"""
Custom exceptions used throughout the transpiler.

Every error a user can hit is one of these so the CLI can produce a
single, consistent, "what failed and where and why" message.
"""

from __future__ import annotations


class TranspileError(Exception):
    """Base class for every error the transpiler raises.

    Carries enough context to tell the user exactly what went wrong:

        location  - human-readable path inside the ASN.1 model
                    (e.g. "module 'CAM' > SEQUENCE 'ItsPduHeader' > field 'messageID'")
        reason    - one-sentence description of why it failed
        hint      - optional suggestion on how to fix it
    """

    def __init__(self, location: str, reason: str, hint: str | None = None):
        self.location = location
        self.reason = reason
        self.hint = hint
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [
            f"  where : {self.location}",
            f"  why   : {self.reason}",
        ]
        if self.hint:
            lines.append(f"  fix   : {self.hint}")
        return "\n".join(lines)


class Asn1SyntaxError(TranspileError):
    """Raised when the ASN.1 input itself does not parse."""

    def __init__(self, message: str):
        # location is implicit (the whole file); we keep the base interface.
        super().__init__(location="<ASN.1 file>", reason=message)


class UnsupportedConstruct(TranspileError):
    """An ASN.1 construct that has no semantics-preserving YANG mapping.

    Per the paper, Section 5 scopes the framework to a subset of ASN.1
    types. Constructs outside that subset (e.g. EMBEDDED PDV) raise this.
    """


class SemanticPreservationImpossible(TranspileError):
    """The construct is in scope, but its ASN.1 carrier set cannot be mapped
    bijectively onto any YANG built-in (e.g. an INTEGER range that no
    signed/unsigned 8/16/32/64 type can represent)."""


class DepthExceeded(TranspileError):
    """A recursive type nests deeper than the configured limit."""


class YangValidationError(TranspileError):
    """The YANG we generated is rejected by pyang."""
