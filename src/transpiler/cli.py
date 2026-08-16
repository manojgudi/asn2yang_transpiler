#!/usr/bin/env python3
"""
Command-line entry point for the ASN.1 -> YANG transpiler.

Usage
-----

  python -m transpiler transpile INPUT.asn [options]
  python asn1_to_yang.py    transpile INPUT.asn [options]   # shim at project root
  asn1-to-yang              transpile INPUT.asn [options]   # installed entry point

Options for `transpile`:
  --check-asn1   validate the ASN.1 input first
  --check-yang   validate the generated YANG with pyang
  --max-depth N  max nesting for SEQUENCE/CHOICE (default 10)
  --output FILE  write YANG to FILE instead of stdout

Standalone checks:
  validate-asn1 INPUT.asn
  validate-yang INPUT.yang
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# In-package imports (this file lives inside the `transpiler` package).
from .asn1_parser import parse_asn1
from .errors import Asn1SyntaxError, TranspileError
from .yang_builder import transpile


# ===========================================================================
# CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="asn1_to_yang",
        description=(
            "Toy ASN.1 -> YANG transpiler implementing the rules from "
            "'Preserving Semantics for Transpilation of Data Modelling "
            "Languages using CafeOBJ' (Section 5)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    # -- transpile ----------------------------------------------------------
    t = sub.add_parser("transpile", help="Convert ASN.1 to YANG (default).")
    t.add_argument("input", help="ASN.1 source file (- for stdin).")
    t.add_argument(
        "--check-asn1", action="store_true", help="Validate the ASN.1 input first."
    )
    t.add_argument(
        "--check-yang",
        action="store_true",
        help="Validate the generated YANG with pyang.",
    )
    t.add_argument(
        "--max-depth",
        type=int,
        default=10,
        help="Max nesting depth for SEQUENCE/CHOICE (default: 10).",
    )
    t.add_argument(
        "--output", "-o", default="-", help="Write YANG to FILE instead of stdout."
    )

    # -- validate-asn1 ------------------------------------------------------
    v1 = sub.add_parser("validate-asn1", help="Only check that the ASN.1 input parses.")
    v1.add_argument("input", help="ASN.1 source file (- for stdin).")

    # -- validate-yang ------------------------------------------------------
    v2 = sub.add_parser("validate-yang", help="Validate a YANG file with pyang.")
    v2.add_argument("input", help="YANG source file.")

    return p


# ===========================================================================
# Actions
# ===========================================================================
def cmd_transpile(args: argparse.Namespace) -> int:
    text = _read_input(args.input)

    # The transpiler itself parses internally; --check-asn1 is a quick
    # way to just *check* without generating YANG.  But because parsing
    # is part of transpilation, the check is essentially free -- we still
    # try the transpile so we can chain --check-yang.
    try:
        ast = parse_asn1(text)
        yang = transpile(ast, max_depth=args.max_depth)
    except Asn1SyntaxError as exc:
        print(_banner("ASN.1 SYNTAX ERROR") + "\n" + str(exc), file=sys.stderr)
        return 2
    except TranspileError as exc:
        print(_banner("TRANSPILER ERROR") + "\n" + str(exc), file=sys.stderr)
        return 3

    if args.check_asn1:
        print("[ok] ASN.1 syntax is valid.", file=sys.stderr)

    if args.output == "-":
        sys.stdout.write(yang)
    else:
        Path(args.output).write_text(yang)
        print(f"[ok] wrote YANG to {args.output}", file=sys.stderr)

    if args.check_yang:
        rc, msg = _pyang_validate(yang, source_name=args.input)
        if rc == 0:
            print("[ok] YANG is valid (pyang).", file=sys.stderr)
        else:
            print(_banner("YANG VALIDATION FAILED"), file=sys.stderr)
            print(msg, file=sys.stderr)
            return 4
    return 0


def cmd_validate_asn1(args: argparse.Namespace) -> int:
    text = _read_input(args.input)
    try:
        parse_asn1(text)
    except Asn1SyntaxError as exc:
        print(_banner("ASN.1 SYNTAX ERROR") + "\n" + str(exc), file=sys.stderr)
        return 2
    print("[ok] ASN.1 syntax is valid.", file=sys.stderr)
    return 0


def cmd_validate_yang(args: argparse.Namespace) -> int:
    text = Path(args.input).read_text()
    rc, msg = _pyang_validate(text, source_name=args.input)
    if rc == 0:
        print("[ok] YANG is valid (pyang).", file=sys.stderr)
        return 0
    print(_banner("YANG VALIDATION FAILED"), file=sys.stderr)
    print(msg, file=sys.stderr)
    return 4


# ===========================================================================
# Helpers
# ===========================================================================
def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text()


def _pyang_validate(
    yang_text: str, source_name: str = "<generated>"
) -> tuple[int, str]:
    """Run `pyang` on the given YANG text.

    Returns (returncode, combined stdout+stderr).
    """
    if shutil.which("pyang") is None:
        return 127, "pyang CLI not found on PATH. Install with: pip install pyang"
    # Write to a temp file so pyang can read it (it doesn't read from stdin).
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yang", delete=False) as f:
        f.write(yang_text)
        tmp = f.name
    try:
        proc = subprocess.run(
            ["pyang", tmp],
            capture_output=True,
            text=True,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _banner(title: str) -> str:
    bar = "=" * max(2, len(title) + 4)
    return f"{bar}\n  {title}\n{bar}"


# ===========================================================================
# main
# ===========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # No subcommand? Default to 'transpile'.
    if args.cmd is None:
        args = parser.parse_args(["transpile", *sys.argv[1:]])

    if args.cmd == "transpile":
        return cmd_transpile(args)
    if args.cmd == "validate-asn1":
        return cmd_validate_asn1(args)
    if args.cmd == "validate-yang":
        return cmd_validate_yang(args)
    parser.error(f"unknown command: {args.cmd}")
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
