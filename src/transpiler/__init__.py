"""
asn1 -> yang transpiler package.

Implements the transpilation rules from:
    "Preserving Semantics for Transpilation of Data Modelling Languages
     using CafeOBJ" (Section 5).

Public entry points:
    parse_asn1(text)        -> AST dict (or raises Asn1SyntaxError)
    transpile(text, source) -> YANG source string
"""

from .asn1_parser import parse_asn1
from .errors import Asn1SyntaxError, TranspileError, YangValidationError
from .yang_builder import transpile

__all__ = [
    "parse_asn1",
    "transpile",
    "TranspileError",
    "Asn1SyntaxError",
    "YangValidationError",
]
