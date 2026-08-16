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
    except Exception as exc:                       # asn1tools raises a generic Exception
        raise Asn1SyntaxError(_clean_parser_error(str(exc))) from exc

    if not raw:
        raise Asn1SyntaxError("no module found in input")

    # asn1tools returns {module_name: module_ast}; we keep it as a dict-of-one.
    return raw


def _clean_parser_error(msg: str) -> str:
    """Trim the noisy preamble asn1tools sometimes prepends."""
    # asn1tools messages start with "ASN.1 syntax error at line X, column Y: "
    if msg.startswith("ASN.1 syntax error"):
        return msg
    return f"ASN.1 parse error: {msg}"
