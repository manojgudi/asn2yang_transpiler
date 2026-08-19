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
    """Extract the (lo, hi) of a restricted INTEGER, or None if unbounded.

    ASN.1 `INTEGER (a..b, c..d, ...)` becomes a `restricted-to` list that
    may contain `None` entries (the `...` extension marker) and/or
    multiple disjoint ranges.  For the carrier-set bijection in paper
    §5.1, Eq.(13), we widen to the bounding range `[min(lo), max(hi)]`
    so that all current values fit (lossy on the gap, bijection-
    preserving for the present carrier set).  See issues.txt §1.
    """
    ranges = type_def.get("restricted-to")
    if not ranges:
        return None
    real_ranges = [r for r in ranges if r is not None]
    if not real_ranges:
        return None
    lo = min(r[0] for r in real_ranges)
    hi = max(r[1] for r in real_ranges)
    return (lo, hi)


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

    ASN.1 extension marker `...` in an ENUMERATED list becomes a `None`
    entry in the asn1tools AST.  We silently drop it (see issues.txt §1).
    """
    lines = ["enumeration {"]
    for entry in type_def["values"]:
        if entry is None:
            continue
        label, value = entry
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
    """BIT STRING -> YANG binary (the paper notes this is straightforward).

    ASN.1 BIT STRING SIZE(N) is in *bits*; YANG `binary` length is in
    *octets*.  Convert via `ceil(N / 8)` so `SIZE(7)` becomes 1 octet
    (7 bits padded to one byte, per JER), which is what `base64`
    encodes to.  Without this conversion, libyang sees a 1-byte
    base64 value against a `length "7"` constraint and rejects it.
    """
    raw_length = _string_length(type_def)
    if not raw_length:
        return "binary", ""
    try:
        # `_string_length` returns either "N" or "lo..hi".
        if ".." in raw_length:
            lo_s, hi_s = raw_length.split("..")
            lo, hi = int(lo_s), int(hi_s)
            octet_clause = f' length "{(lo + 7) // 8}..{(hi + 7) // 8}"'
        else:
            n = int(raw_length)
            octet_clause = f' length "{(n + 7) // 8}"'
    except ValueError:
        octet_clause = f' length "{raw_length}"'
    return "binary", octet_clause


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
    type_def: dict,
    location: str,
    depth: int,
    max_depth: int,
    inner_emit,
    ast: dict | None = None,
) -> str:
    """Paper §5.4, extended per optional.txt.

    SEQUENCE -> YANG container with one child per member, in declared
    order.  Per the rephrase in optional.txt:
        - ASN.1 mandatory  -> YANG `mandatory true;`
        - ASN.1 OPTIONAL   -> no `mandatory` (YANG leaves are optional by default)
        - ASN.1 DEFAULT v  -> YANG `default "v";`

    If `ast` is provided, a member whose type-name resolves to a
    SEQUENCE / SET / SEQUENCE OF / SET OF typedef is inlined as a
    nested container / list rather than emitted as `leaf foo { type Foo; }`.
    This handles ASN.1 IMPORTS (e.g. `heading Heading` where
    `Heading ::= SEQUENCE {...}` in another module) and SEQUENCE OF
    members (e.g. `pathHistory PathHistory` where
    `PathHistory ::= SEQUENCE OF PathPoint`).
    """
    _check_depth(location, depth, max_depth)
    if not type_def.get("members"):
        return "    // empty SEQUENCE\n"

    all_types = _flatten_types(ast) if ast is not None else {}

    lines = []
    for member in type_def["members"]:
        # ASN.1 extension marker `...` becomes a `None` entry in the
        # asn1tools AST.  Per paper §6 these are future-extension
        # markers; for a present-tense transpile we silently skip them
        # (see issues.txt #2).
        if member is None:
            continue
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
                    mname, member, child_loc, depth + 1, max_depth, inner_emit, ast
                )
            )
            continue

        # Named SEQUENCE reference -> inline its fields as a container.
        if (
            ast is not None
            and isinstance(mtype, str)
            and mtype in all_types
            and all_types[mtype].get("type") in ("SEQUENCE", "SET")
        ):
            child_loc = f"{location} > field '{mname}'"
            inline_member = {
                "type": mtype,
                "name": mname,
                "members": all_types[mtype].get("members") or [],
                "optional": bool(member.get("optional")),
            }
            lines.append(
                _emit_inline_block(
                    mname,
                    inline_member,
                    child_loc,
                    depth + 1,
                    max_depth,
                    inner_emit,
                    ast,
                    presence=bool(member.get("optional")),
                )
            )
            continue

        # Named SEQUENCE OF / SET OF reference -> emit as a list.
        if (
            ast is not None
            and isinstance(mtype, str)
            and mtype in all_types
            and all_types[mtype].get("type") in ("SEQUENCE OF", "SET OF")
        ):
            child_loc = f"{location} > field '{mname}'"
            list_tdef = {
                "type": all_types[mtype]["type"],
                "size": all_types[mtype].get("size"),
                "element": all_types[mtype].get("element") or {"type": "OCTET STRING"},
            }
            list_body = sequence_of(
                list_tdef, child_loc, depth + 1, max_depth, inner_emit, ast, mname
            )
            if member.get("optional"):
                yang_name = _as_identifier(mname)
                indented = "\n".join(
                    "    " + ln if ln else ln
                    for ln in list_body.rstrip("\n").split("\n")
                )
                # YANG lists don't support `presence`; wrap the list
                # in an OPTIONAL container with `presence` instead.
                lines.append(
                    f"    container {yang_name} {{\n"
                    f'      presence "{yang_name.replace("-", " ")} is present";\n'
                    f"{indented}\n    }}"
                )
            else:
                lines.append(list_body)
            continue

        # Named CHOICE reference -> emit as a container wrapping a choice.
        if (
            ast is not None
            and isinstance(mtype, str)
            and mtype in all_types
            and all_types[mtype].get("type") == "CHOICE"
        ):
            child_loc = f"{location} > field '{mname}'"
            choice_body = choice(
                all_types[mtype],
                child_loc,
                depth + 1,
                max_depth,
                inner_emit,
                choice_name=mname,
                ast=ast,
            )
            yang_name = _as_identifier(mname)
            inner = "\n".join(
                "    " + ln if ln else ln for ln in choice_body.rstrip("\n").split("\n")
            )
            if member.get("optional"):
                lines.append(
                    f"    container {yang_name} {{\n"
                    f'      presence "{yang_name.replace("-", " ")} is present";\n'
                    f"{inner}\n    }}"
                )
            else:
                lines.append(f"    container {yang_name} {{\n{inner}\n    }}")
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
    name: str,
    member: dict,
    location: str,
    depth: int,
    max_depth: int,
    inner_emit,
    ast: dict | None = None,
    presence: bool = False,
) -> str:
    """Inline composite member: dispatch via the regular _emit_type path.

    Indents the inner body one extra level so the resulting YANG is
    human-readable, not just well-formed.

    When `ast` is provided, we always recurse through `sequence()` so
    that nested named SEQUENCE references in the inlined body also get
    inlined (handles imported typedefs like
    `Heading { headingValue, headingConfidence }` regardless of
    whether `member["type"]` says "SEQUENCE" or a named typedef like
    "Heading").

    When `presence=True`, the inlined container is emitted as a YANG
    `presence` container, which makes the whole sub-tree optional
    rather than mandatory.  Used for ASN.1 OPTIONAL SEQUENCE members.
    """
    yang_name = _as_identifier(name)
    if ast is not None:
        # Direct call to sequence() so the ast is threaded through.
        body = sequence(member, location, depth, max_depth, inner_emit, ast)
    else:
        asn_type = member["type"]
        kind, body, _ = inner_emit(asn_type, member, location, depth, max_depth)
    inner = _indent_block(body.rstrip("\n").split("\n"))
    if presence:
        return _wrap_with_presence(yang_name, inner)
    return f"    container {yang_name} {{\n{inner}\n    }}"


def _indent_block(lines: list[str]) -> str:
    """Indent a list of body lines by 4 spaces, preserving blank lines."""
    return "\n".join("    " + ln if ln else ln for ln in lines)


def _wrap_with_presence(name: str, content: str) -> str:
    """Wrap `content` in a `container name { presence ...; content }`.

    Used for OPTIONAL inlined SEQUENCE / SET / CHOICE members: ASN.1
    OPTIONAL maps cleanly to a YANG `presence` container.  SEQUENCE OF
    cannot have `presence` directly (lists don't support it), so the
    caller wraps the list in a fresh container and applies this helper.
    """
    return (
        f"    container {name} {{\n"
        f'      presence "{name.replace("-", " ")} is present";\n'
        f"{content}\n    }}"
    )


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
    ast: dict | None = None,
) -> str:
    """Paper §5.5, extended per optional.txt.

    CHOICE -> YANG `choice { ... }` with one `case` per alternative.

    Per optional.txt: ASN.1 CHOICE alternatives are non-optional (and
    asn1tools enforces this), so we emit `mandatory true;` ONCE on the
    `choice` line itself -- not on individual `case` lines, because
    RFC 7950 §7.9.2 forbids `mandatory` as a sub-statement of `case`.

    When `ast` is provided, an alternative whose type-name resolves to
    a SEQUENCE / SET / CHOICE / SEQUENCE OF is inlined as a container
    / list rather than emitted as `case X { leaf X { type X; } }`.
    """
    _check_depth(location, depth, max_depth)
    members = type_def.get("members") or []
    if not members:
        raise _impassable(location, "CHOICE has no alternatives.")

    all_types = _flatten_types(ast) if ast is not None else {}

    lines = [
        f"    choice {_as_identifier(choice_name)} {{",
        "      mandatory true;",
    ]
    for alt in members:
        # ASN.1 extension marker `...` (see issues.txt #2).
        if alt is None:
            continue
        atype = alt["type"]
        aname = alt["name"]
        alt_loc = f"{location} > alternative '{aname}'"

        # Named SEQUENCE / SET alternative -> inline as a container inside the case.
        if (
            ast is not None
            and isinstance(atype, str)
            and atype in all_types
            and all_types[atype].get("type") in ("SEQUENCE", "SET")
        ):
            inline_member = {
                "type": atype,
                "name": aname,
                "members": all_types[atype].get("members") or [],
            }
            case_body = _emit_inline_block(
                aname, inline_member, alt_loc, depth + 1, max_depth, inner_emit, ast
            )
            # Re-indent one level deeper so the container body sits at 8 spaces.
            case_inner = "\n".join(
                "  " + ln if ln else ln for ln in case_body.rstrip("\n").split("\n")
            )
            lines.append(f"      case {_as_identifier(aname)} {{")
            for ln in case_inner.split("\n"):
                lines.append(ln)
            lines.append("      }")
            continue

        # Named SEQUENCE OF / SET OF alternative -> inline as a list inside the case.
        if (
            ast is not None
            and isinstance(atype, str)
            and atype in all_types
            and all_types[atype].get("type") in ("SEQUENCE OF", "SET OF")
        ):
            list_tdef = {
                "type": all_types[atype]["type"],
                "size": all_types[atype].get("size"),
                "element": all_types[atype].get("element") or {"type": "OCTET STRING"},
            }
            list_body = sequence_of(
                list_tdef, alt_loc, depth + 1, max_depth, inner_emit, ast, aname
            )
            case_inner = "\n".join(
                "  " + ln if ln else ln for ln in list_body.rstrip("\n").split("\n")
            )
            lines.append(f"      case {_as_identifier(aname)} {{")
            for ln in case_inner.split("\n"):
                lines.append(ln)
            lines.append("      }")
            continue

        # Named CHOICE alternative -> inline as a container wrapping a choice.
        if (
            ast is not None
            and isinstance(atype, str)
            and atype in all_types
            and all_types[atype].get("type") == "CHOICE"
        ):
            choice_body = choice(
                all_types[atype],
                alt_loc,
                depth + 1,
                max_depth,
                inner_emit,
                choice_name=aname,
                ast=ast,
            )
            inner = "\n".join(
                "  " + ln if ln else ln for ln in choice_body.rstrip("\n").split("\n")
            )
            lines.append(f"      case {_as_identifier(aname)} {{")
            for ln in inner.split("\n"):
                lines.append(ln)
            lines.append("      }")
            continue

        # Leaf-like / inline alternative -- existing path.
        if isinstance(atype, str):
            kind, body, _tail = inner_emit(atype, alt, alt_loc, depth + 1, max_depth)
            case_inner = _wrap_member(aname, kind, body)
        else:
            case_inner = [
                _emit_inline_block(
                    aname, alt, alt_loc, depth + 1, max_depth, inner_emit, ast
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
    type_def: dict,
    location: str,
    depth: int,
    max_depth: int,
    inner_emit,
    ast: dict | None = None,
    list_name: str = "entry",
) -> str:
    """SEQUENCE OF T -> YANG 'list' with one entry typed by T.

    When `ast` is provided and the element is itself a SEQUENCE / SET
    (named typedef or inline), its fields are inlined as the list's
    entry body.  This handles `SEQUENCE OF PathPoint` etc. from
    ITS-Container-style imports.
    """
    _check_depth(location, depth, max_depth)
    size = type_def.get("size")
    if not size:
        size_clause = ""
    else:
        # Filter out the ASN.1 `...` extension marker (becomes None in
        # the asn1tools AST).  See issues.txt §1.
        real_size = [s for s in size if s is not None]
        if not real_size:
            size_clause = ""
        # Same shape as for strings -- see _string_length.
        elif len(real_size) == 1 and isinstance(real_size[0], int):
            n = real_size[0]
            size_clause = f"      min-elements {n};\n      max-elements {n};\n"
        elif (
            len(real_size) == 1
            and isinstance(real_size[0], (list, tuple))
            and len(real_size[0]) == 2
        ):
            lo, hi = real_size[0]
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
    all_types = _flatten_types(ast) if ast is not None else {}

    # Composite element type (named or inline SEQUENCE / SET) -> inline
    # its fields as the list's entry body.
    is_composite = (
        ast is not None
        and isinstance(etype, str)
        and etype in all_types
        and all_types[etype].get("type") in ("SEQUENCE", "SET")
    )
    inline_composite = (
        ast is not None
        and isinstance(etype, str)
        and etype in ("SEQUENCE", "SET")
        and "members" in element
    )
    if is_composite or inline_composite:
        if is_composite:
            elem_tdef = {
                "type": etype,
                "name": "entry",
                "members": all_types[etype].get("members") or [],
            }
        else:
            elem_tdef = element
        elem_body = sequence(elem_tdef, elt_loc, depth + 1, max_depth, inner_emit, ast)
        # YANG lists must have a key.  Pick the first non-None leaf-like
        # member if there is one; otherwise fall back to a synthetic
        # `id` leaf so the schema at least loads.
        key_member = _pick_list_key(elem_tdef.get("members") or [], all_types)
        if key_member is not None:
            key_name = _as_identifier(key_member["name"])
            key_line = f'      key "{key_name}";\n'
        else:
            key_line = '      key "id";\n      leaf id { type uint32; }\n'
        return (
            f"    list {_as_identifier(list_name)} {{\n"
            f"{key_line}{size_clause}{elem_body.rstrip()}\n    }}"
        )

    # Leaf-like element type.
    if isinstance(etype, str):
        kind, body, tail = inner_emit(etype, element, elt_loc, depth + 1, max_depth)
        body = body.strip()
        # `body` is already a complete `type ... ;` or `type ... { ... }`
        # clause per the `_emit_type` contract -- do NOT add another `;`.
        entry = f'    list {_as_identifier(list_name)} {{\n      key "value";\n      leaf value {{ type {body} }}\n{size_clause}    }}'
        return entry

    # Composite element type (anonymous, no ast) -- raise as before.
    raise _impassable(
        elt_loc,
        f"SEQUENCE OF composite element type {etype!r} is not supported.",
        "use a SEQUENCE OF <leaf-like-type> or extend sequence_of().",
    )


def _flatten_types(ast: dict) -> dict:
    """Return a flat {type_name: tdef} dict from a multi-module AST.

    The transpiler's AST is `{module_name: {"types": {...}}}`.  When
    the user supplies multiple ASN.1 files via `parse_asn1_files`, all
    modules' types are merged into one synthetic module, so this is a
    single-level unwrap.  Kept as a separate function in case the AST
    shape grows later.
    """
    flat: dict = {}
    for _mod_name, mod in ast.items():
        flat.update(mod.get("types") or {})
    return flat


def _pick_list_key(members: list, all_types: dict) -> dict | None:
    """Find a good YANG list-key candidate among SEQUENCE OF element members.

    YANG RFC 7950 §7.8.2 requires a list to have exactly one key.  We
    pick the first non-`...` member whose type is a leaf-like ASN.1
    sort (INTEGER / BOOLEAN / ENUMERATED, or a typedef that resolves
    to one of these).
    """
    LEAF_LIKE = {"INTEGER", "BOOLEAN", "ENUMERATED"}
    for m in members:
        if m is None:
            continue
        tname = m.get("type")
        if not isinstance(tname, str):
            continue
        if tname in LEAF_LIKE:
            return m
        resolved = all_types.get(tname, {})
        if resolved.get("type") in LEAF_LIKE:
            return m
    return None


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
