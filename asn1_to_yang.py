#!/usr/bin/env python3
"""
Thin shim at the project root so users can also run:

    python asn1_to_yang.py transpile INPUT.asn [options]

The real implementation lives in `src/transpiler/cli.py`.  This file
just forwards to it via `runpy` so it works whether the user has
installed the package (`pip install -e .`) or not.

Recommended invocations after `pip install -e .`:

    python -m transpiler ...
    asn1-to-yang ...           # installed entry point
"""

import runpy
import sys


def _invoke_cli() -> int:
    """Run `transpiler.cli` as if it were `__main__`, return its exit code."""
    ns = runpy.run_module("transpiler.cli", run_name="__main__")
    return ns["main"]()


if __name__ == "__main__":
    sys.exit(_invoke_cli())
