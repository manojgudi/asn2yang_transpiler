"""
Thin wrapper around `asn1tools.parse_string` that:

  1. Re-raises parser errors as our own Asn1SyntaxError so the CLI can
     show a uniform "what failed and where and why" message.

  2. Normalises the result into the small AST dict that the rest of the
     transpiler works with.

The shape of the dict (mirrors what `asn1tools` gives us, no surprises):

    {
        "name": str,                     # module name
        "tags": str | None,              # AUTOMATIC / EXPLICIT / IMPLICIT / None
        "types": {
            "TypeName": {
                "type":   "INTEGER" | "BOOLEAN" | "REAL" | "OCTET STRING"
                         | "BIT STRING" | "IA5String" | "OBJECT IDENTIFIER"
                         | "SEQUENCE" | "SET" | "CHOICE" | "ENUMERATED"
                         | "SEQUENCE OF" | "SET OF" | "TypeRef" | ...,
                # type-specific keys appear here; the builder knows which to
                # look at. Examples:
                "restricted-to": [[low, high], ...],   # INTEGER / REAL
                "size":          [[low, high], ...],   # OCTET STRING / etc.
                "members":       [ {"type":..., "name":..., "optional":bool?}, ... ],
                "values":        [ [label, int], ... ],  # ENUMERATED
                "element":       {...},                  # SEQUENCE OF inner type
                ...
            },
            ...
        }
    }
"""

from __future__ import annotations

import asn1tools

from .errors import Asn1SyntaxError


def parse_asn1(text: str) -> dict:
    """Parse an ASN.1 module source string.

    Returns a normalised AST dict keyed by module name
    (one ASN.1 file typically contains exactly one module).

    Raises Asn1SyntaxError with asn1tools' own diagnostic on failure.
    """
    try:
        raw = asn1tools.parse_string(text)
    except Exception as exc:  # asn1tools raises a generic Exception
        raise Asn1SyntaxError(_clean_parser_error(str(exc))) from exc

    if not raw:
        raise Asn1SyntaxError("no module found in input")

    # asn1tools returns {module_name: module_ast}; we keep it as a dict-of-one.
    return raw


def parse_asn1_files(paths: list[str]) -> dict:
    """Parse multiple ASN.1 files and merge their types into a single
    synthetic module named after the LAST file.

    This is the multi-module equivalent of `parse_asn1`.  ASN.1 files
    use `IMPORTS Foo FROM Bar;` to reference types defined in another
    module.  For a single-file transpile we cannot resolve these
    forward references; for a multi-file transpile we flatten every
    module's types into one namespace so that names resolve naturally.

    The LAST file is treated as the "primary" module -- its unreferenced
    SEQUENCE types are the candidates for being emitted as the YANG
    root container.  Every earlier file's types are treated as
    dependencies and are only inlined at use sites.

    Returns: a one-entry AST dict (module_name -> module_ast) where
    `module_ast["types"]` contains the union of every loaded module's
    types, and `module_ast["__multi_module__"]` is True so the
    transpiler knows to skip top-level SEQUENCE / SET / CHOICE / SEQUENCE
    OF (which would otherwise duplicate the inlined copies emitted at
    use sites).  `module_ast["__root_candidates__"]` holds the set of
    type names from the LAST file -- the transpiler emits only those
    as top-level SEQUENCE/SET containers.

    Raises Asn1SyntaxError on the first failing file.
    """
    if not paths:
        raise Asn1SyntaxError("no input files given")

    merged_types: dict = {}
    primary_name: str | None = None
    last_file_types: set[str] = set()

    for path in paths:
        text = _read_file(path)
        per_module = parse_asn1(text)
        per_types: set[str] = set()
        for mod in per_module.values():
            per_types.update((mod.get("types") or {}).keys())
        # The LAST iteration overwrites this -- so the final value is
        # the type names from the last file in `paths`.
        last_file_types = per_types
        if primary_name is None and per_module:
            primary_name = next(iter(per_module))
        for _mod_name, mod in per_module.items():
            for tname, tdef in (mod.get("types") or {}).items():
                merged_types[tname] = tdef

    if primary_name is None or not merged_types:
        raise Asn1SyntaxError("no types found in any input file")

    return {
        primary_name: {
            "types": merged_types,
            "__multi_module__": True,
            "__root_candidates__": last_file_types,
        }
    }


def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError as exc:
        raise Asn1SyntaxError(f"cannot read {path}: {exc}") from exc


def _clean_parser_error(msg: str) -> str:
    """Trim the noisy preamble asn1tools sometimes prepends."""
    # asn1tools messages start with "ASN.1 syntax error at line X, column Y: "
    if msg.startswith("ASN.1 syntax error"):
        return msg
    return f"ASN.1 parse error: {msg}"
