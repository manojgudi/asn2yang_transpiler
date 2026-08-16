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
        # No uint fits: fall through to signed. Note: the signed range
        # is narrower than the unsigned one for positives (int64 max =
        # 2^63 - 1 < uint64 max = 2^64 - 1), so this loop will also fail
        # and raise below if neither fits.
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
    """Paper §5.4, extended per optional.txt.

    SEQUENCE -> YANG container with one child per member, in declared
    order.  Per the rephrase in optional.txt:
        - ASN.1 mandatory  -> YANG `mandatory true;`
        - ASN.1 OPTIONAL   -> no `mandatory` (YANG leaves are optional by default)
        - ASN.1 DEFAULT v  -> YANG `default "v";`
    """
    _check_depth(location, depth, max_depth)
    if not type_def.get("members"):
        return "    // empty SEQUENCE\n"

    lines = []
    for member in type_def["members"]:
        mandatory, default = _member_options(member, location)
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
        kind, body, _tail = inner_emit(
            mtype if isinstance(mtype, str) else member["type"],
            member,
            child_loc,
            depth + 1,
            max_depth,
        )
        lines.extend(
            _wrap_member(
                mname,
                kind,
                body,
                mandatory=mandatory,
                default=default,
            )
        )
    return "\n".join(lines) + "\n"


def _wrap_member(
    name: str,
    kind: str,
    body: str,
    *,
    mandatory: bool = True,
    default: str | None = None,
) -> list[str]:
    """Format a leaf-like member.

    `body` is already a complete type clause (terminated with ';' for
    plain types or '}' for constrained ones).

    Per optional.txt:
        mandatory=True, default=None  ->  leaf X { type <body>; mandatory true; }
        mandatory=False, default=None ->  leaf X { type <body>; }   (YANG optional default)
        mandatory=False, default="v"  ->  leaf X { type <body>; default "v"; }
    """
    yang_name = _as_identifier(name)
    if kind != "leaf":
        return [body]  # already a full block

    body = body.strip()

    # Sub-statements that go inside the leaf after `type ...;`
    extras: list[str] = []
    if mandatory:
        extras.append("mandatory true;")
    if default is not None:
        extras.append(f'default "{default}";')

    # Multi-line body (enumeration): always emit multi-line leaf so the
    # indentation is consistent.
    if "\n" in body:
        lines = [
            f"    leaf {yang_name} {{",
            f"      type {body.split(chr(10))[0]}",
        ]
        for ln in body.split("\n")[1:]:
            lines.append("      " + ln)
        for ex in extras:
            lines.append(f"      {ex}")
        lines.append("    }")
        return lines

    # Multi-line leaf only when we have extras (mandatory / default).
    # NB: `body` is already a complete type clause ('uint8;' or
    # 'uint8 { range "..."; }'), so we drop it in verbatim -- no extra ';'.
    if extras:
        lines = [
            f"    leaf {yang_name} {{",
            f"      type {body}",
        ]
        for ex in extras:
            lines.append(f"      {ex}")
        lines.append("    }")
        return lines

    # Simple single-line case (no extras).
    return [f"    leaf {yang_name} {{ type {body} }}"]


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
    """Paper §5.5, extended per optional.txt.

    CHOICE -> YANG `choice { ... }` with one `case` per alternative.

    Per optional.txt: ASN.1 CHOICE alternatives are non-optional (and
    asn1tools enforces this), so we emit `mandatory true;` ONCE on the
    `choice` line itself -- not on individual `case` lines, because
    RFC 7950 §7.9.2 forbids `mandatory` as a sub-statement of `case`.
    """
    _check_depth(location, depth, max_depth)
    members = type_def.get("members") or []
    if not members:
        raise _impassable(location, "CHOICE has no alternatives.")

    lines = [
        f"    choice {_as_identifier(choice_name)} {{",
        "      mandatory true;",
    ]
    for alt in members:
        atype = alt["type"]
        aname = alt["name"]
        alt_loc = f"{location} > alternative '{aname}'"
        if isinstance(atype, str):
            kind, body, _tail = inner_emit(atype, alt, alt_loc, depth + 1, max_depth)
            case_inner = _wrap_member(aname, kind, body)
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

    # Composite element type -- the carrier-set bijection for SEQUENCE OF
    # only carries over cleanly when T is a leaf-like type with a fixed
    # built-in mapping. Composite T would need an inline dispatch path
    # we don't yet implement; raise so it's not silently wrong.
    raise _impassable(
        elt_loc,
        f"SEQUENCE OF composite element type {etype!r} is not supported.",
        "use a SEQUENCE OF <leaf-like-type> or extend sequence_of().",
    )


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


def _member_options(member: dict, location: str) -> tuple[bool, str | None]:
    """Return (mandatory, default_yang_value) for a SEQUENCE member.

    ASN.1 semantics:
        <no keyword>          -> mandatory=True,  default=None
        OPTIONAL              -> mandatory=False, default=None
        DEFAULT v  (= OPTIONAL with a default) -> mandatory=False, default=YANG-formatted v

    The mapping follows the rephrase in `optional.txt`:
        - ASN.1 mandatory  -> YANG `mandatory true;`
        - ASN.1 OPTIONAL   -> no `mandatory` (YANG leaves are optional by default)
        - ASN.1 DEFAULT v  -> YANG `default "v";`  (plus no `mandatory true;`)
    """
    if "default" in member:
        # DEFAULT implies OPTIONAL in ASN.1.
        return False, _format_default_value(member["default"])
    if member.get("optional"):
        return False, None
    return True, None


def _format_default_value(v) -> str:
    """Render an ASN.1 default value as a YANG `default "<v>";` literal.

    YANG's `default` substatement always takes a quoted string, regardless
    of the underlying type.  Python booleans need a special case because
    `str(True)` is 'True' (capitalised) but YANG wants 'true'/'false'.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


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
