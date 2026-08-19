"""
Walk the AST returned by `parse_asn1` and emit YANG source text.

The builder is intentionally tiny: every actual rule lives in `rules.py`.
This file only knows how to:

  * turn a single (module, type) into a YANG `typedef` block,
  * turn every typedef together into a YANG `module` header,
  * dispatch to the right rule in `rules.py` based on the ASN.1 type
    string found in the AST.
"""

from __future__ import annotations

from . import rules
from .errors import UnsupportedConstruct

# Default safety limit for recursive types (SEQUENCE/CHOICE/SEQUENCE OF).
DEFAULT_MAX_DEPTH = 10


def transpile(ast: dict, max_depth: int = DEFAULT_MAX_DEPTH) -> str:
    """Top-level entry: ASN.1 AST dict -> YANG source string.

    `ast` is the {module_name: module_dict} dict from `parse_asn1`.
    A multi-module AST (from `parse_asn1_files`) is accepted: every
    type from every loaded module is emitted into one YANG module
    named after the primary (first) module.
    """
    if not ast:
        raise ValueError("empty AST: no modules to transpile")

    primary_name = next(iter(ast))
    types: dict = {}
    for mod in ast.values():
        types.update(mod.get("types") or {})

    # 1.  module header (YANG requires namespace + prefix at minimum).
    lines = [_module_header(primary_name)]

    # 2.  one typedef per ASN.1 type.  SEQUENCE / CHOICE top-level
    #     become a 'container' (YANG requires `choice` to live inside
    #     a container, list, case, or grouping -- never directly under
    #     a typedef).  SEQUENCE OF / SET OF at the top level cannot be
    #     typedef'd in YANG (a `list` requires an identifier and
    #     typedefs don't allow lists), so we skip them -- every use
    #     site inlines the list via `sequence()`'s named-type lookup.
    #
    #     In multi-module mode (ast carries the `__multi_module__`
    #     flag set by `parse_asn1_files`), every SEQUENCE / SET /
    #     CHOICE / SEQUENCE OF that is *referenced* as a member type
    #     in another SEQUENCE is omitted from the top level -- those
    #     are inlined at the use site.  An unreferenced top-level
    #     type (i.e. the "root" type that nothing else references)
    #     is still emitted as a container so the JSON has something
    #     to anchor on.
    is_multi_module = any(mod.get("__multi_module__") for mod in ast.values())
    referenced: set[str] = set()
    if is_multi_module:
        for tdef in types.values():
            # Direct member references from SEQUENCE / SET / CHOICE.
            for member in tdef.get("members") or []:
                if member is None:
                    continue
                mtype = member.get("type")
                if isinstance(mtype, str):
                    referenced.add(mtype)
            # Element types of SEQUENCE OF / SET OF (inlined at use sites).
            if tdef.get("type") in ("SEQUENCE OF", "SET OF"):
                elem = tdef.get("element") or {}
                etype = elem.get("type")
                if isinstance(etype, str):
                    referenced.add(etype)
    for tname, tdef in types.items():
        location = f"module '{primary_name}' > type '{tname}'"
        if tdef["type"] in ("SEQUENCE", "SET"):
            if is_multi_module and tname in referenced:
                continue  # inlined at use sites
            if is_multi_module and tname not in referenced:
                # Unreferenced top-level SEQUENCE -- only emit it as a
                # root container if it came from the LAST (primary)
                # file.  Orphan types from imported modules are
                # skipped, otherwise libyang complains about their
                # mandatory fields being missing from the JSON.
                root_candidates: set[str] = set()
                for mod in ast.values():
                    # __root_candidates__ contains ALL types from the
                    # last file; subtract referenced ones to get the
                    # actual root candidates.
                    rc = mod.get("__root_candidates__", set())
                    root_candidates |= rc - referenced
                if tname not in root_candidates:
                    continue
                # The PRIMARY root SEQUENCE is emitted WITHOUT a
                # wrapping container: its members appear directly at
                # module top level.  This matches ASN.1 JER-style
                # JSON, where the root SEQUENCE's fields are at the
                # top of the encoded message rather than wrapped in a
                # named object.  Mirrors the structure used by the
                # hand-coded `cam-payload` reference YANG, which has
                # `generationDeltaTime` etc. directly under the
                # payload grouping.
                body = rules.sequence(
                    tdef,
                    location,
                    depth=0,
                    max_depth=max_depth,
                    inner_emit=_emit_type,
                    ast=ast,
                )
                # sequence() emits at 4-space depth for top-level
                # containers (depth=1).  At depth=0 we want 2 spaces.
                for ln in body.rstrip("\n").split("\n"):
                    if ln.startswith("    "):
                        lines.append("  " + ln[4:])
                    else:
                        lines.append(ln)
                continue
            lines.append(_sequence_container(tname, tdef, location, max_depth, ast))
        elif tdef["type"] == "CHOICE":
            if is_multi_module and tname in referenced:
                continue  # inlined at use sites
            lines.append(_choice_container(tname, tdef, location, max_depth, ast))
        elif tdef["type"] in ("SEQUENCE OF", "SET OF"):
            # Skip -- inlined at use sites.
            continue
        else:
            lines.append(_typedef(tname, tdef, location, max_depth, ast))

    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# YANG module header
# ---------------------------------------------------------------------------
def _module_header(name: str) -> str:
    prefix = name.lower()
    return (
        f"module {name} {{\n"
        f"  yang-version 1.1;\n"
        f'  namespace "urn:example:asn1-transpile:{name}";\n'
        f"  prefix {prefix};\n"
        f'  organization "transpiled from ASN.1";\n'
        f'  contact "transpiler";\n'
        f'  description "Generated by asn1_to_yang transpiler.";\n'
    )


# ---------------------------------------------------------------------------
# typedef wrapper (for non-SEQUENCE/SET top-level types)
# ---------------------------------------------------------------------------
def _typedef(name: str, tdef: dict, location: str, max_depth: int, ast: dict) -> str:
    kind, body, tail = _emit_type(tdef["type"], tdef, location, 0, max_depth)
    body = body.strip()
    if "\n" in body:
        # Multi-line body (only `enumeration` produces one). The body has
        # its own internal indentation; we just prefix `type ` to the
        # first line and let the rest flow as-is.
        lines = [f"  typedef {name} {{"]
        body_lines = body.split("\n")
        lines.append("    type " + body_lines[0])
        for ln in body_lines[1:]:
            lines.append("    " + ln)
        lines.append("  }")
        return "\n".join(lines) + "\n"
    # Single-line body -- the rule already produced a complete `type ... { ... }`.
    return f"  typedef {name} {{ type {body} }}\n"


def _sequence_container(
    name: str, tdef: dict, location: str, max_depth: int, ast: dict
) -> str:
    body = rules.sequence(
        tdef, location, depth=1, max_depth=max_depth, inner_emit=_emit_type, ast=ast
    )
    return f"  container {name} {{\n{body}  }}\n"


def _choice_container(
    name: str, tdef: dict, location: str, max_depth: int, ast: dict
) -> str:
    """Wrap a top-level CHOICE in a container.

    YANG forbids `typedef` from having a `choice` sub-statement, so we
    put the choice inside a container named after the ASN.1 type.
    """
    body = rules.choice(
        tdef,
        location,
        depth=1,
        max_depth=max_depth,
        inner_emit=_emit_type,
        choice_name=name,
        ast=ast,
    )
    return f"  container {name} {{\n{body}  }}\n"


# ---------------------------------------------------------------------------
# Dispatch: ASN.1 type string -> rules.py function
# ---------------------------------------------------------------------------
# Functions in rules.py that return a (kind, body, tail) triple.
# `kind` is "leaf" or "block".
# `body` is the YANG type clause (for kind=="leaf") or the indented
# block body (for kind=="block").
# `tail` is any closing text needed for multi-line types (e.g. '}').
def _emit_type(asn_type, tdef, location, depth, max_depth, ast=None):
    """Dispatch one ASN.1 type to its rule. Returns (kind, body, tail).

    `asn_type` is the ASN.1 type name as a string.  For primitive types
    like INTEGER / BOOLEAN we call a `rules.<name>` function that returns
    a (yang_type, constraint) pair.  For composite types like SEQUENCE /
    CHOICE we call a `rules.<name>` function that returns a full YANG
    body block.

    `ast` is threaded through to SEQUENCE / SEQUENCE OF / CHOICE rules
    so that named type references in member lists can be inlined.
    """
    if asn_type == "INTEGER":
        yang, clause = rules.integer(tdef, location)
        return "leaf", _combine(yang, clause), ""

    if asn_type == "REAL":
        yang, clause = rules.real(tdef, location)
        return "leaf", _combine(yang, clause), ""

    if asn_type == "BOOLEAN":
        yang, clause = rules.boolean()
        return "leaf", _combine(yang, clause), ""

    if asn_type == "ENUMERATED":
        body = rules.enumerated(tdef)
        return "leaf", body, ""

    if asn_type == "IA5String":
        yang, clause = rules.ia5string(tdef)
        return "leaf", _combine(yang, clause), ""

    if asn_type == "OCTET STRING":
        yang, clause = rules.octet_string(tdef)
        return "leaf", _combine(yang, clause), ""

    if asn_type == "BIT STRING":
        yang, clause = rules.bit_string(tdef)
        return "leaf", _combine(yang, clause), ""

    # All other ASN.1 string types (UTF8String, NumericString,
    # PrintableString, TeletexString, VisibleString, GeneralString,
    # GraphicString, ObjectDescriptor, etc.) map to YANG's `string`.
    # Per paper §5.3 the carrier-set intersection is a strict subset
    # of YANG string, so a plain YANG `string` is a sound supertype.
    if asn_type in (
        "UTF8String",
        "NumericString",
        "PrintableString",
        "TeletexString",
        "TeletexString (SIZE (...))",
        "VideotexString",
        "VisibleString",
        "GeneralString",
        "GraphicString",
        "ObjectDescriptor",
        "UniversalString",
        "BMPString",
    ):
        # Reuse the IA5String rule -- both end up as `string` with
        # optional length constraint; the carrier set is a subset of
        # YANG's UTF-8 string.
        yang, clause = rules.ia5string(tdef)
        return "leaf", _combine(yang, clause), ""

    if asn_type == "OBJECT IDENTIFIER":
        yang, clause = rules.object_identifier()
        return "leaf", _combine(yang, clause), ""

    # SEQUENCE / SET (also catches inline anonymous SEQUENCE inside
    # another SEQUENCE member).
    if asn_type in ("SEQUENCE", "SET"):
        body = rules.sequence(
            tdef, location, depth, max_depth, inner_emit=_emit_type, ast=ast
        )
        return "block", body, ""

    if asn_type == "SEQUENCE OF":
        body = rules.sequence_of(
            tdef, location, depth, max_depth, inner_emit=_emit_type, ast=ast
        )
        return "block", body, ""

    if asn_type == "SET OF":
        # Identical to SEQUENCE OF as far as the paper's framework goes.
        body = rules.sequence_of(
            tdef, location, depth, max_depth, inner_emit=_emit_type, ast=ast
        )
        return "block", body, ""

    if asn_type == "CHOICE":
        body = rules.choice(
            tdef, location, depth, max_depth, inner_emit=_emit_type, ast=ast
        )
        return "block", body, ""

    # Unknown ASN.1 type -> most likely a reference to another named type
    # or an out-of-scope construct.
    if isinstance(asn_type, str) and asn_type not in _KNOWN_TYPES:
        # Heuristic: if it has no members/enum/etc. treat it as a type
        # reference.  Otherwise complain.
        if (
            "members" not in tdef
            and "values" not in tdef
            and "restricted-to" not in tdef
            and "size" not in tdef
            and "element" not in tdef
        ):
            yang, clause = rules.type_reference(tdef)
            return "leaf", _combine(yang, clause), ""
        raise UnsupportedConstruct(
            location,
            f"ASN.1 construct '{asn_type}' is outside the scope of the paper (see §5).",
            "see README.md for the list of supported constructs.",
        )

    raise UnsupportedConstruct(
        location,
        f"ASN.1 construct '{asn_type}' is not handled by this transpiler.",
        "file a bug or extend transpiler/rules.py.",
    )


_KNOWN_TYPES = {
    "INTEGER",
    "REAL",
    "BOOLEAN",
    "ENUMERATED",
    "IA5String",
    "OCTET STRING",
    "BIT STRING",
    "OBJECT IDENTIFIER",
    "SEQUENCE",
    "SET",
    "SEQUENCE OF",
    "SET OF",
    "CHOICE",
    "UTF8String",
    "NumericString",
    "PrintableString",
    "TeletexString",
    "VideotexString",
    "VisibleString",
    "GeneralString",
    "GraphicString",
    "ObjectDescriptor",
    "UniversalString",
    "BMPString",
    # ASN.1 SEQUENCE is intercepted upstream into _sequence_container; the
    # rules module also handles it when used inline. Keep here for completeness.
}


# ---------------------------------------------------------------------------
# Pretty glue
# ---------------------------------------------------------------------------
def _combine(yang: str, clause: str) -> str:
    """Join a YANG base type with its constraints into one statement.

    Examples:
        'uint8'                       ->  'uint8;'
        'int16' + ' range "-50..150"' ->  'int16 { range "-50..150"; }'

    Either way, the returned string is a COMPLETE `type ... ;` or
    `type ... { ... }` clause ready to be dropped after `type `. The
    caller must NOT append another ';' or '}'.
    """
    clause = clause.strip()
    if not clause:
        return f"{yang};"
    return f"{yang} {{ {clause}; }}"
