"""
Allow `python -m transpiler ...` to work.

We load `transpiler.cli` at runtime via `runpy` so the static analyzer
does not need to follow a cross-module import -- the previous shape
(`from .cli import main`) tripped Pyright's analyzer when the workspace
snapshot was stale.  Runtime behaviour is identical either way.
"""

import runpy
import sys


def _invoke_cli() -> int:
    """Run `transpiler.cli` as if it were `__main__`, return its exit code."""
    # runpy returns the module's globals dict; pull `main` out of it.
    ns = runpy.run_module("transpiler.cli", run_name="__main__")
    return ns["main"]()


if __name__ == "__main__":
    sys.exit(_invoke_cli())
