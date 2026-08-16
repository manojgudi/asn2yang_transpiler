"""
The transpilation rules from Section 5 of:

    "Preserving Semantics for Transpilation of Data Modelling Languages
     using CafeOBJ"

Each public function implements ONE rule from the paper, has a one-line
docstring citing the paper section, and returns either:

  * ("leaf", "<yang-type>", "<constraints-or-empty>")
        for primitive ASN.1 types whose YANG mapping is a single `type` clause
        inside a `leaf` or `uses`.

  * ("block", "<indented-yang-body>")
        for composite ASN.1 types whose mapping is a multi-line YANG block
        (SEQUENCE / CHOICE / SEQUENCE OF / ENUMERATED).

A rule may also raise one of the errors in errors.py when semantic
preservation is impossible for the given ASN.1 input.
"""

from __future__ import annotations

from .errors import DepthExceeded, SemanticPreservationImpossible

# ---------------------------------------------------------------------------
# Bounds for the 8 concrete YANG integer types (Eq. 12 of the paper).
# Order matters: smallest k first, as per Eq. 13.
# ---------------------------------------------------------------------------
_UINT = [(8, 0, 2**8 - 1), (16, 0, 2**16 - 1), (32, 0, 2**32 - 1), (64, 0, 2**64 - 1)]

_INT = [
    (8, -(2**7), 2**7 - 1),
    (16, -(2**15), 2**15 - 1),
    (32, -(2**31), 2**31 - 1),
    (64, -(2**63), 2**63 - 1),
]

# decimal64 carries i * 10^-n with -2^63 <= i <= 2^63 - 1, n in 1..18.
_DEC64_INT_MIN = -(2**63)
_DEC64_INT_MAX = 2**63 - 1


# ===========================================================================
# 5.1  INTEGER  (Eq. 13)
# ===========================================================================
def integer(type_def: dict, location: str) -> tuple[str, str]:
    """Paper §5.1, Eq.(13). Returns (yang_type, range_clause_or_empty)."""
    bounds = _integer_bounds(type_def, location)

    # Unbounded INTEGER -> default to int64 (paper's only fallback mention).
    if bounds is None:
        return "int64", ""

    lo, hi = bounds
    if lo >= 0:
        for k, b_lo, b_hi in _UINT:
            if lo >= b_lo and hi <= b_hi:
                return f"uint{k}", f" range '{lo}..{hi}'"
        # Out of unsigned range but non-negative -> use the smallest signed type
        # big enough (semantics-preserving because every uint value fits in
        # the corresponding signed range? No, it doesn't. So we need signed
        # only if lo < 0 OR if no uint fits. Both cases covered below.)
    for k, b_lo, b_hi in _INT:
        if lo >= b_lo and hi <= b_hi:
            return f"int{k}", f" range '{lo}..{hi}'"

    # No YANG built-in integer covers [lo, hi].
    raise _impassable(
        location,
        f"INTEGER range [{lo}, {hi}] does not fit any YANG integer "
        f"(int8/16/32/64 or uint8/16/32/64).",
        "split the range into multiple fields or constrain the source range.",
    )


def _integer_bounds(type_def: dict, location: str):
    """Extract the (lo, hi) of a restricted INTEGER, or None if unbounded."""
    ranges = type_def.get("restricted-to")
    if not ranges:
        return None
    if len(ranges) > 1:
        raise _impassable(
            location,
            "multiple value-range constraints (a..b, c..d, ...) are not "
            "covered by the paper's Eq.(13).",
            "collapse to a single contiguous range [a, b].",
        )
    return tuple(ranges[0])


# ===========================================================================
# 5.2  REAL  (Eq. 16)
# ===========================================================================
def real(type_def: dict, location: str) -> tuple[str, str]:
    """Paper §5.2, Eq.(16). Returns (decimal64, fraction-digits/range)."""
    # ASN.1 REAL's carrier set always includes +/-infinity and NaN, which
    # decimal64 cannot represent. The paper only covers *restricted* REALs.
    if "restricted-to" not in type_def:
        raise _impassable(
            location,
            "ASN.1 REAL is unbounded (carrier set = R union {+inf, -inf, NaN}); "
            "YANG decimal64 cannot represent +/-infinity or NaN, so a "
            "bijection is impossible.",
            "add a value-range constraint, e.g. 'REAL (0.0 .. 100.0)'.",
        )

    lo, hi = type_def["restricted-to"][0]
    # asn1tools emits REAL bounds as strings; convert for arithmetic.
    try:
        lo_f, hi_f = float(lo), float(hi)
    except ValueError:
        raise _impassable(
            location,
            f"REAL range bound is not a valid number: {lo!r} .. {hi!r}.",
            "use decimal literals like '0.0 .. 100.0'.",
        )
    # decimal64 with n fraction-digits carries i * 10^-n with i in int64.
    # Find the smallest n that makes both lo and hi exactly representable.
    n = _smallest_fraction_digits(lo_f, hi_f, location)
    return "decimal64", f' fraction-digits {n}; range "{lo}..{hi}"'


def _smallest_fraction_digits(lo: float, hi: float, location: str) -> int:
    """Return the smallest n in 1..18 that lets decimal64 carry [lo, hi]."""
    for n in range(1, 19):
        raw_lo = round(lo * (10**n))
        raw_hi = round(hi * (10**n))
        if raw_lo >= _DEC64_INT_MIN and raw_hi <= _DEC64_INT_MAX:
            return n
    raise _impassable(
        location,
        f"REAL range [{lo}, {hi}] cannot be represented by any "
        f"decimal64 (no fraction-digits in 1..18 keeps the integer part "
        f"within int64).",
        "tighten the REAL range or split into multiple fields.",
    )


# ===========================================================================
# BOOLEAN  (§5, identical semantics to YANG boolean)
# ===========================================================================
def boolean() -> tuple[str, str]:
    """Paper §5, Eq.(9)."""
    return "boolean", ""


# ===========================================================================
# ENUMERATED  (§5, identical semantics, just different syntax)
# ===========================================================================
def enumerated(type_def: dict) -> str:
    """Paper §5, Eq.(10). Returns the `enumeration { enum ... }` block.

    Left-justified: the builder applies uniform indentation when it wraps
    the result inside `type { ... }`.
    """
    lines = ["enumeration {"]
    for label, value in type_def["values"]:
        lines.append(f'  enum "{_as_identifier(label)}" {{ value {value}; }}')
    lines.append("}")
    return "\n".join(lines)


# ===========================================================================
# 5.3  Strings  (IA5String / OCTET STRING / BIT STRING)
# ===========================================================================
def ia5string(type_def: dict) -> tuple[str, str]:
    """Paper §5.3, Eq.(18)-(19). IA5String -> string with length constraint.

    The valid character set (Eq. 19) is a *subset* of YANG's `string`, so
    we emit `string` plus a `length` constraint that the carrier set
    intersection fits.
    """
    length = _string_length(type_def)
    return "string", f' length "{length}"' if length else ""


def octet_string(type_def: dict) -> tuple[str, str]:
    """OCTET STRING -> YANG binary (the paper notes this is straightforward)."""
    length = _string_length(type_def)
    return "binary", f' length "{length}"' if length else ""


def bit_string(type_def: dict) -> tuple[str, str]:
    """BIT STRING -> YANG binary (the paper notes this is straightforward)."""
    length = _string_length(type_def)
    return "binary", f' length "{length}"' if length else ""


def _string_length(type_def: dict) -> str:
    """Read the optional SIZE() constraint and turn it into a YANG length.

    asn1tools emits two shapes for 'size':
        SIZE(8)             ->  [8]              (fixed length)
        SIZE(0..100)        ->  [[0, 100]]       (range)
        SIZE(0..100, ...)   ->  [[0,100], ...]   (multiple ranges; not supported)

    YANG has two equivalent syntaxes for a fixed length:
        length "8"          (single value)
        length "8..8"        (range with min == max)
    pyang rejects the latter, so we use the former.
    """
    size = type_def.get("size")
    if not size:
        return ""
    if len(size) == 1 and isinstance(size[0], int):
        return str(size[0])
    if len(size) == 1 and isinstance(size[0], (list, tuple)) and len(size[0]) == 2:
        lo, hi = size[0]
        return f"{lo}..{hi}"
    raise _impassable(
        "<string type>",
        f"unsupported size constraint: {size!r}",
        "use a single contiguous SIZE (lo..hi) or a fixed SIZE (n).",
    )


# ===========================================================================
# OBJECT IDENTIFIER  (commonly used but not covered by the paper)
# ===========================================================================
def object_identifier() -> tuple[str, str]:
    """OBJECT IDENTIFIER -> YANG 'object-identifier-128'. (No paper mapping.)"""
    return "object-identifier-128", ""


# ===========================================================================
# 5.4  SEQUENCE  -- returns a YANG *block*, not a leaf clause.
# ===========================================================================
def sequence(
    type_def: dict, location: str, depth: int, max_depth: int, inner_emit
) -> str:
    """Paper §5.4. SEQUENCE -> YANG container with one child per member,
    in declared order, all mandatory.

    Per the paper, OPTIONAL fields are out of scope for the semantic model.
    """
    _check_depth(location, depth, max_depth)
    if not type_def.get("members"):
        return "    // empty SEQUENCE\n"

    lines = []
    for member in type_def["members"]:
        _assert_mandatory(member, location)
        mtype = member["type"]
        mname = member["name"]

        # Inline composite members (anonymous SEQUENCE / CHOICE inside
        # another SEQUENCE) appear as a string type name with a
        # "members" key inside the member dict. Wrap them in their own
        # container so their fields don't leak into the parent.
        if (
            isinstance(mtype, str)
            and mtype in ("SEQUENCE", "SET")
            and "members" in member
        ):
            child_loc = f"{location} > field '{mname}'"
            lines.append(
                _emit_inline_block(
                    mname, member, child_loc, depth + 1, max_depth, inner_emit
                )
            )
            continue

        child_loc = f"{location} > field '{mname}'"
        kind, body, tail = inner_emit(
            mtype if isinstance(mtype, str) else member["type"],
            member,
            child_loc,
            depth + 1,
            max_depth,
        )
        lines.extend(_wrap_member(mname, kind, body, tail))
    return "\n".join(lines) + "\n"


def _wrap_member(name: str, kind: str, body: str, tail: str) -> list[str]:
    """Format a leaf-like member. `body` is already a complete type clause
    (terminated with ';' for plain types or '}' for constrained ones) --
    so we just wrap it in `leaf X { type <body> }`."""
    yang_name = _as_identifier(name)
    if kind == "leaf":
        body = body.strip()
        if "\n" in body:
            # Multi-line body (enumeration). Indent each line.
            lines = [f"    leaf {yang_name} {{", f"      type {body.split(chr(10))[0]}"]
            for ln in body.split("\n")[1:]:
                lines.append("      " + ln)
            lines.append("    }")
            return lines
        return [f"    leaf {yang_name} {{ type {body} }}"]
    return [body]  # already a full block


def _emit_inline_block(
    name: str, member: dict, location: str, depth: int, max_depth: int, inner_emit
) -> str:
    """Inline composite member: dispatch via the regular _emit_type path.

    Indents the inner body one extra level so the resulting YANG is
    human-readable, not just well-formed.
    """
    yang_name = _as_identifier(name)
    asn_type = member["type"]
    kind, body, _ = inner_emit(asn_type, member, location, depth, max_depth)
    inner = "\n".join("    " + ln if ln else ln for ln in body.rstrip("\n").split("\n"))
    return f"    container {yang_name} {{\n{inner}\n    }}"


# ===========================================================================
# 5.5  CHOICE  -- returns a YANG block.
# ===========================================================================
def choice(
    type_def: dict,
    location: str,
    depth: int,
    max_depth: int,
    inner_emit,
    choice_name: str = "choice",
) -> str:
    """Paper §5.5. CHOICE -> YANG 'choice' with one 'case' per alternative.

    `choice_name` is the YANG identifier for the choice node; the caller
    passes it in because RFC 7950 requires every `choice` to have a name
    that is unique within its enclosing container/list/grouping.
    """
    _check_depth(location, depth, max_depth)
    members = type_def.get("members") or []
    if not members:
        raise _impassable(location, "CHOICE has no alternatives.")

    lines = [f"    choice {_as_identifier(choice_name)} {{"]
    for alt in members:
        atype = alt["type"]
        aname = alt["name"]
        alt_loc = f"{location} > alternative '{aname}'"
        if isinstance(atype, str):
            kind, body, tail = inner_emit(atype, alt, alt_loc, depth + 1, max_depth)
            case_inner = _wrap_member(aname, kind, body, tail)
        else:
            case_inner = [
                _emit_inline_block(
                    aname, alt, alt_loc, depth + 1, max_depth, inner_emit
                )
            ]
        lines.append(f"      case {_as_identifier(aname)} {{")
        # Re-indent case_inner one level deeper so the leaves inside
        # the case sit at 8 spaces (matching RFC 7950 examples).
        for ln in case_inner:
            lines.append("  " + ln if ln else ln)
        lines.append("      }")
    lines.append("    }")
    return "\n".join(lines) + "\n"


# ===========================================================================
# SEQUENCE OF  -> YANG list  (paper does not cover this directly but the
#                              SEQUENCE OF/SET OF carrier-set is identical,
#                              so the bijection carries over.)
# ===========================================================================
def sequence_of(
    type_def: dict, location: str, depth: int, max_depth: int, inner_emit
) -> str:
    """SEQUENCE OF T -> YANG 'list' with one entry typed by T."""
    _check_depth(location, depth, max_depth)
    size = type_def.get("size")
    if not size:
        size_clause = ""
    else:
        # Same shape as for strings -- see _string_length.
        if len(size) == 1 and isinstance(size[0], int):
            n = size[0]
            size_clause = f"      min-elements {n};\n      max-elements {n};\n"
        elif (
            len(size) == 1 and isinstance(size[0], (list, tuple)) and len(size[0]) == 2
        ):
            lo, hi = size[0]
            # YANG 'list' uses min-elements / max-elements, not range syntax.
            size_clause = (f"      min-elements {lo};\n" if lo > 0 else "") + (
                f"      max-elements {hi};\n" if hi < 2**32 - 1 else ""
            )
        else:
            raise _impassable(
                location,
                f"unsupported SEQUENCE OF size constraint: {size!r}",
                "use a single contiguous SIZE (lo..hi) or a fixed SIZE (n).",
            )

    element = type_def["element"]
    etype = element["type"]
    elt_loc = f"{location} > element"

    # We need a name for the list's key. Convention: "entry" with a 'value'
    # leaf (or 'container' if T is composite).
    if isinstance(etype, str):
        kind, body, tail = inner_emit(etype, element, elt_loc, depth + 1, max_depth)
        body = body.strip()
        if kind == "leaf":
            entry = f'    list entry {{\n      key "value";\n      leaf value {{ type {body};{tail} }}\n{size_clause}    }}'
            return entry
        # kind == block (enum); embed inside list
        entry = f'    list entry {{\n      key "value";\n      leaf value {{\n        type {body};{tail}\n      }}\n{size_clause}    }}'
        return entry

    # Composite element type -- use a 'container' inside the list entry.
    _, body, _ = inner_emit(
        "__INLINE__", {"__inner__": element}, elt_loc, depth, max_depth
    )
    return f'    list entry {{\n      key "id";\n      leaf id {{ type uint32; }}\n      container value {{\n{body}      }}\n{size_clause}    }}'


# ===========================================================================
# Type reference (alias)  -- e.g. 'MyAlias ::= MyInt'
# ===========================================================================
def type_reference(type_def: dict) -> tuple[str, str]:
    """Alias for another type. Forwarded as-is; YANG allows type by name."""
    return type_def["type"], ""


# ===========================================================================
# Helpers
# ===========================================================================
def _as_identifier(name: str) -> str:
    """Map an ASN.1 identifier to a YANG-friendly one (YANG allows '-' in
    identifiers; we mirror that style). asn1tools already gives valid
    identifiers so this is mostly a safety net."""
    return name.replace(" ", "-")


def _assert_mandatory(member: dict, location: str) -> None:
    if member.get("optional"):
        raise _impassable(
            f"{location} > field '{member['name']}'",
            "OPTIONAL is not covered by the paper's semantic model "
            "(§5.4 explicitly restricts to mandatory fields).",
            "remove OPTIONAL or mark the field with a sentinel value.",
        )
    if "default" in member:
        raise _impassable(
            f"{location} > field '{member['name']}'",
            "DEFAULT values are not covered by the paper's semantic model "
            "(§5.4 explicitly restricts to mandatory fields).",
            "remove DEFAULT or mark the field with a sentinel value.",
        )


def _check_depth(location: str, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise _depth_exceeded(location, max_depth)


def _impassable(location: str, reason: str, hint: str | None = None):
    return SemanticPreservationImpossible(location, reason, hint)


def _depth_exceeded(location: str, max_depth: int):
    return DepthExceeded(
        location,
        f"recursive nesting depth exceeded the configured limit of {max_depth}.",
        "flatten the type or raise the limit in asn1_to_yang.py.",
    )
