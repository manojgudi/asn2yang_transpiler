#!/usr/bin/env python3
"""
validate_data.py -- validate JSON data files against ASN.1 model and
the corresponding transpiled YANG model.

Inputs:
    examples/data/data_NN.json        -- instance expected to satisfy ASN.1
    examples/data/data_NN_bad.json    -- instance expected to be rejected

For each pair (valid, bad) the script:
    1. Transpiles examples/NN.asn to YANG using `transpiler.transpile`.
       (We do NOT modify the transpiler source -- this is purely empirical
       validation of the *output* of the transpiler.)
    2. Validates the JSON against the ASN.1 model using an *independent*
       constraint checker (range, enumeration membership, size, choice
       alternative).
    3. Validates the JSON against the transpiled YANG using `yanglint`
       (libyang).
    4. Reports ASN.1 cons valid_ok / bad_caught and YANG valid_acc / bad_rej.

These four checks are consistency sanity tests: a valid instance must be
accepted by both validators and a bad instance must be rejected by both.
They are not a proof of semantic preservation.

Usage:
    python3 validate_data.py                 # all examples
    python3 validate_data.py 01              # just example 01
    python3 validate_data.py 01 02 03        # specific examples
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from transpiler import transpile as do_transpile
from transpiler.asn1_parser import parse_asn1

# Examples with no single SEQUENCE/CHOICE root -- their JSON is a flat
# dict mapping type names to values.  We synthesise a wrapping container
# in the YANG so libyang has something to anchor on.
WRAP_NEEDED = {"01_basic_types", "04_real_numbers"}


# ---------------------------------------------------------------------------
# Independent ASN.1 constraint checker. Returns list[str] of human-readable
# violations (empty list == value satisfies all ASN.1 constraints).
# ---------------------------------------------------------------------------
def asn1_check_constraints(ast: dict, type_name: str, value: Any) -> list[str]:
    mod = list(ast.values())[0]
    tdef = mod["types"].get(type_name, {})
    errors: list[str] = []

    def _check(tdef, val, path):
        kind = tdef.get("type")
        if kind == "INTEGER":
            for lo, hi in tdef.get("restricted-to") or []:
                if lo is None and hi is None:
                    continue
                if not (lo <= val <= hi):
                    errors.append(f"{path}: INTEGER {val} outside [{lo},{hi}]")
        elif kind == "ENUMERATED":
            labels = [lbl for lbl, _ in tdef.get("values", [])]
            if val not in labels:
                errors.append(f"{path}: ENUMERATED value {val!r} not in {labels}")
        elif kind == "SEQUENCE":
            if not isinstance(val, dict):
                errors.append(
                    f"{path}: expected SEQUENCE dict, got {type(val).__name__}"
                )
                return
            members = tdef.get("members") or []
            known = {m["name"] for m in members if m is not None}
            extra = set(val.keys()) - known
            if extra:
                errors.append(f"{path}: unexpected keys {sorted(extra)}")
            for member in members:
                if member is None:
                    continue
                mname = member["name"]
                # OPTIONAL fields may be absent -- skip rather than error.
                if mname not in val:
                    if member.get("optional"):
                        continue
                    errors.append(f"{path}: missing member '{mname}'")
                    continue
                # Inline types carry their full definition inside the
                # member dict (members/values/restricted-to/size). Named
                # typedefs are looked up by name.  Precedence: inline
                # definition wins.
                inline_keys = ("members", "values", "restricted-to", "size")
                if any(k in member for k in inline_keys):
                    member_tdef = {
                        k: member[k]
                        for k in ("type", "members", "values", "restricted-to", "size")
                        if k in member
                    }
                else:
                    mtype = member["type"]
                    member_tdef = mod["types"].get(mtype, {"type": mtype})
                _check(member_tdef, val[mname], f"{path}.{mname}")
        elif kind == "CHOICE":
            if not isinstance(val, dict):
                errors.append(f"{path}: expected CHOICE dict, got {type(val).__name__}")
                return
            members = tdef.get("members") or []
            known = {m["name"] for m in members if m is not None}
            if not any(k in val for k in known):
                errors.append(f"{path}: no CHOICE alternative matched in {val!r}")
                return
            for member in members:
                if member is None:
                    continue
                mname = member["name"]
                if mname in val:
                    inline_keys = ("members", "values", "restricted-to", "size")
                    if any(k in member for k in inline_keys):
                        member_tdef = {
                            k: member[k]
                            for k in (
                                "type",
                                "members",
                                "values",
                                "restricted-to",
                                "size",
                            )
                            if k in member
                        }
                    else:
                        mtype = member["type"]
                        member_tdef = mod["types"].get(mtype, {"type": mtype})
                    _check(member_tdef, val[mname], f"{path}.{mname}")
                    return
        elif kind in ("IA5String", "OCTET STRING", "BIT STRING"):
            size = tdef.get("size") or []
            for entry in size:
                if entry is None:
                    continue
                if isinstance(entry, int):
                    lo, hi = entry, entry
                elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                    lo, hi = entry[0], entry[1]
                else:
                    continue
                if kind == "OCTET STRING" and isinstance(val, str):
                    try:
                        raw = bytes.fromhex(val)
                    except ValueError:
                        errors.append(f"{path}: OCTET STRING not valid hex ({val!r})")
                        continue
                    length = len(raw)
                elif isinstance(val, (bytes, str)):
                    length = len(val)
                else:
                    errors.append(f"{path}: {kind} value not string/bytes ({val!r})")
                    continue
                if not (lo <= length <= hi):
                    errors.append(f"{path}: {kind} length {length} outside [{lo},{hi}]")
        elif kind == "REAL":
            for lo, hi in tdef.get("restricted-to") or []:
                if lo is None:
                    continue
                try:
                    in_range = float(lo) <= float(val) <= float(hi)
                except (TypeError, ValueError):
                    errors.append(f"{path}: REAL {val!r} not numeric vs [{lo},{hi}]")
                    continue
                if not in_range:
                    errors.append(f"{path}: REAL {val} outside [{lo},{hi}]")
        elif kind == "BOOLEAN":
            if not isinstance(val, bool):
                errors.append(f"{path}: expected bool, got {type(val).__name__}")

    _check(tdef, value, type_name)
    return errors


# ---------------------------------------------------------------------------
# Helper: convert OCTET STRING from hex string -> bytes (and back).
# We store OCTET STRINGs as hex in JSON for readability; libyang expects
# base64 in RFC 7951 JSON encoding.
# ---------------------------------------------------------------------------
def _json_to_yang(d: Any) -> Any:
    """Recursively normalise Python values for libyang's RFC 7951 JSON:
    * OCTET STRING stored as hex string -> base64 (binary).
    * REAL / decimal64 stored as Python float -> JSON string with
      the same textual representation, because RFC 7951 §6.6 requires
      decimal64 to be encoded as a string.
    """
    if isinstance(d, dict):
        return {k: _json_to_yang(v) for k, v in d.items()}
    if isinstance(d, float):
        return repr(d)
    if (
        isinstance(d, str)
        and len(d) > 0
        and len(d) % 2 == 0
        and all(c in "0123456789abcdefABCDEF" for c in d)
    ):
        try:
            return base64.b64encode(bytes.fromhex(d)).decode()
        except Exception:
            return d
    return d


# ---------------------------------------------------------------------------
# YANG validation via yanglint (libyang CLI).
# ---------------------------------------------------------------------------
def yanglint_validate(
    yang_text: str,
    module_name: str,
    instance: dict,
    wrap: bool,
    wrap_spec: dict[str, str],
) -> tuple[bool, str]:
    """Returns (accepted, message).  accepted=True iff libyang reports no
    validation errors."""
    if shutil.which("yanglint") is None:
        return False, "yanglint CLI not installed (apt install libyang2-tools)"

    with tempfile.TemporaryDirectory() as tmpdir:
        yang_path = os.path.join(tmpdir, f"{module_name}.yang")

        # libyang requires a `revision` statement.
        if 'revision "' not in yang_text:
            body_start = -1
            for keyword in (
                "typedef ",
                "container ",
                "list ",
                "leaf ",
                "leaf-list ",
                "choice ",
                "grouping ",
                "notification ",
                "rpc ",
                "augment ",
            ):
                idx = yang_text.find("\n  " + keyword)
                if idx >= 0 and (body_start < 0 or idx < body_start):
                    body_start = idx
            if body_start >= 0:
                yang_text = (
                    yang_text[:body_start]
                    + '\n  revision "2024-01-01" { description "transpiler test"; }'
                    + "\n"
                    + yang_text[body_start:]
                )

        # Wrap typedef-only modules in a synthetic container.
        if wrap:
            extra = "\n  container test-instance {\n    config true;\n"
            for leaf, ref in wrap_spec.items():
                extra += f"    leaf {leaf} {{ type {ref}; }}\n"
            extra += "  }\n"
            yang_text = yang_text.rstrip()
            if not yang_text.endswith("}"):
                return False, f"unexpected YANG tail: {yang_text[-50:]!r}"
            yang_text = yang_text[:-1] + extra + "}\n"

        Path(yang_path).write_text(yang_text)

        json_path = os.path.join(tmpdir, "data.json")
        Path(json_path).write_text(json.dumps(_json_to_yang(instance)))

        proc = subprocess.run(
            ["yanglint", "-f", "json", yang_path, json_path],
            capture_output=True,
            text=True,
        )
        combined = (proc.stdout + proc.stderr).strip()
        if "YANGLINT[E]" in combined or "libyang err" in combined:
            return False, combined[:300]
        if proc.returncode != 0:
            return False, combined[:300] or f"yanglint rc={proc.returncode}"
        return True, ""


# ---------------------------------------------------------------------------
# Per-example driver
# ---------------------------------------------------------------------------
def _root_type(ast: dict) -> str | None:
    mod = list(ast.values())[0]
    for tname, tdef in mod["types"].items():
        if tdef["type"] in ("SEQUENCE", "CHOICE"):
            return tname
    return None


def run_example(name: str) -> dict:
    asn1_path = Path(f"examples/{name}.asn")
    data_dir = Path("examples/data")
    valid_path = data_dir / f"data_{name[:2]}.json"
    bad_path = data_dir / f"data_{name[:2]}_bad.json"
    if not (valid_path.exists() and bad_path.exists()):
        return {"name": name, "error": f"missing {valid_path} or {bad_path}"}

    asn1_text = asn1_path.read_text()
    ast = parse_asn1(asn1_text)
    module_name = list(ast.keys())[0]
    try:
        valid_json: dict = json.loads(valid_path.read_text())
        bad_json: dict = json.loads(bad_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {"name": name, "error": f"JSON read failed: {exc}"}
    # Drop any _comment keys used for documentation
    valid_json = {k: v for k, v in valid_json.items() if not k.startswith("_")}
    bad_json = {k: v for k, v in bad_json.items() if not k.startswith("_")}

    # ASN.1 transpile (no source-code changes, just using the existing
    # transpiler as-is).
    try:
        yang_text = do_transpile(ast)
    except Exception as exc:
        return {"name": name, "error": f"transpile failed: {exc}"}

    # ASN.1 constraint check (the paper's pT)
    if name in WRAP_NEEDED:
        cons_valid_errs: list[str] = []
        cons_bad_errs: list[str] = []
        for tname, val in valid_json.items():
            cons_valid_errs.extend(asn1_check_constraints(ast, tname, val))
        for tname, val in bad_json.items():
            cons_bad_errs.extend(asn1_check_constraints(ast, tname, val))
        wrap = True
        wrap_spec = {k.lower().replace(" ", "-"): k for k in valid_json.keys()}
    else:
        root = _root_type(ast)
        if root is None:
            return {"name": name, "error": "no SEQUENCE/CHOICE root type"}
        cons_valid_errs = asn1_check_constraints(ast, root, valid_json)
        cons_bad_errs = asn1_check_constraints(ast, root, bad_json)
        wrap = False
        wrap_spec = {}

    # Build the JSON instance in RFC 7951 form: every key prefixed with
    # the module NAME (not the module prefix keyword).
    def yangify(d: dict, rename_map: dict[str, str] | None = None) -> dict:
        if rename_map is None:
            rename_map = {}
        out: dict = {}
        for k, v in d.items():
            new_k = rename_map.get(k, k)
            if isinstance(v, dict):
                out[f"{module_name}:{new_k}"] = yangify(v, rename_map)
            else:
                out[f"{module_name}:{new_k}"] = v
        return out

    if wrap:
        type_to_leaf = {v: k for k, v in wrap_spec.items()}
        yang_inst_valid = {
            f"{module_name}:test-instance": yangify(valid_json, type_to_leaf)
        }
        yang_inst_bad = {
            f"{module_name}:test-instance": yangify(bad_json, type_to_leaf)
        }
    else:
        root = _root_type(ast)
        assert root is not None
        yang_inst_valid = {f"{module_name}:{root}": yangify(valid_json)}
        yang_inst_bad = {f"{module_name}:{root}": yangify(bad_json)}

    yang_ok_valid, yang_msg_valid = yanglint_validate(
        yang_text, module_name, yang_inst_valid, wrap, wrap_spec
    )
    yang_ok_bad, yang_msg_bad = yanglint_validate(
        yang_text, module_name, yang_inst_bad, wrap, wrap_spec
    )

    return {
        "name": name,
        "module": module_name,
        "asn1_cons_valid_ok": len(cons_valid_errs) == 0,
        "asn1_cons_bad_caught": len(cons_bad_errs) > 0,
        "cons_valid_errs": cons_valid_errs,
        "cons_bad_errs": cons_bad_errs,
        "yang_valid_accepted": yang_ok_valid,
        "yang_bad_rejected": not yang_ok_bad,
        "yang_valid_msg": yang_msg_valid,
        "yang_bad_msg": yang_msg_bad,
    }


def main(argv: list[str]) -> int:
    all_examples = [
        "01_basic_types",
        "02_sequence",
        "03_choice",
        "04_real_numbers",
        "06_optional_and_default",
    ]
    names = argv[1:] if len(argv) > 1 else all_examples
    names = [n for n in names if n in all_examples]

    header = (
        f"{'example':25s} {'ASN.1 cons valid':>19s} "
        f"{'ASN.1 cons bad':>16s} {'YANG valid acc':>15s} "
        f"{'YANG bad rej':>13s}"
    )
    print(header)
    print("-" * len(header))

    overall = True
    for name in names:
        r = run_example(name)
        if "error" in r:
            print(f"{name:25s} ERROR: {r['error']}")
            overall = False
            continue
        cells = [
            "Y" if r["asn1_cons_valid_ok"] else "N",
            "Y" if r["asn1_cons_bad_caught"] else "N",
            "Y" if r["yang_valid_accepted"] else "N",
            "Y" if r["yang_bad_rejected"] else "N",
        ]
        print(
            f"{name:25s} {cells[0]:>19s} {cells[1]:>16s} "
            f"{cells[2]:>15s} {cells[3]:>13s}"
        )
        if "N" in cells:
            overall = False
            if r["cons_valid_errs"]:
                print(f"    cons-valid errors: {r['cons_valid_errs'][:2]}")
            if r["cons_bad_errs"]:
                print(f"    cons-bad    errors: {r['cons_bad_errs'][:2]}")
            if r["yang_valid_msg"]:
                print(f"    YANG-valid  : {r['yang_valid_msg'][:200]}")
            if r["yang_bad_msg"]:
                print(f"    YANG-bad    : {r['yang_bad_msg'][:200]}")

    print()
    print("Legend:")
    print(
        "  ASN.1 cons valid : independent ASN.1 constraint check accepts data_NN.json"
    )
    print(
        "  ASN.1 cons bad   : independent ASN.1 constraint check rejects data_NN_bad.json"
    )
    print("  YANG valid acc   : libyang (yanglint) accepts data_NN.json")
    print("  YANG bad rej     : libyang (yanglint) rejects data_NN_bad.json")
    print()
    print("These are consistency sanity checks between the ASN.1 model and the")
    print("transpiled YANG model -- not a proof of semantic preservation.")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
