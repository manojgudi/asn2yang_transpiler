# asn1-to-yang-transpiler

A toy ASN.1 → YANG transpiler that implements the rules from the
research paper:

> *Preserving Semantics for Transpilation of Data Modelling Languages
> using CafeOBJ* — **Section 5** (Integer / Real / String / Record /
> Choice rules).

It parses an ASN.1 module with [`asn1tools`][asn1tools] (industry-standard),
walks the AST, applies one small rule per ASN.1 type, and emits YANG that
is validated by [`pyang`][pyang] (the IETF-recommended YANG validator).

[asn1tools]: https://github.com/eerimoq/asn1tools
[pyang]:     https://github.com/mbj4668/pyang

## Install

```bash
pip install -e .
```

This puts two commands on your `$PATH`:

* `python -m transpiler …`
* `asn1-to-yang …`

You can also just run the project-root shim:

```bash
python asn1_to_yang.py …
```

## Usage

```text
python -m transpiler transpile INPUT.asn
                              [--check-asn1]
                              [--check-yang]
                              [--max-depth N]    # default 10
                              [--output FILE]    # default stdout

python -m transpiler validate-asn1 INPUT.asn
python -m transpiler validate-yang INPUT.yang
```

### Examples

```bash
# Basic leaf types -> typedefs.
python -m transpiler transpile examples/01_basic_types.asn --check-yang

# SEQUENCE with nested anonymous SEQUENCE -> containers.
python -m transpiler transpile examples/02_sequence.asn --check-yang

# CHOICE -> container wrapping a `choice` node with one `case` per alt.
python -m transpiler transpile examples/03_choice.asn --check-yang

# REAL -> decimal64 with the smallest fraction-digits that fits the range.
python -m transpiler transpile examples/04_real_numbers.asn --check-yang

# Each type here should ABORT with a clean "where / why / fix" message.
python -m transpiler transpile examples/05_failure_modes.asn
```

## What it supports (per the paper, §5)

| ASN.1 | YANG | Paper rule |
| --- | --- | --- |
| `INTEGER (l..u)` | smallest `uint`/`int` k ∈ {8,16,32,64} fitting `[l,u]` | §5.1, Eq.(13) |
| `REAL (l..u)` (restricted) | `decimal64` with smallest `fraction-digits` in 1..18 | §5.2, Eq.(16) |
| `BOOLEAN` | `boolean` | §5, Eq.(9) |
| `ENUMERATED` | `enumeration { enum … }` | §5, Eq.(10) |
| `IA5String` | `string` with `length` | §5.3, Eq.(19) |
| `OCTET STRING` | `binary` with `length` | §5.3 |
| `BIT STRING` | `binary` with `length` | §5.3 |
| `SEQUENCE` | `container` with mandatory leaves in declaration order | §5.4 |
| `CHOICE` | `container` wrapping a `choice` node | §5.5 |
| `OBJECT IDENTIFIER` | `object-identifier-128` | (commonly used; not in paper) |

## What it refuses (graceful failure)

Every refusal raises one of the exception types in `transpiler/errors.py`
and prints a uniform three-line `where / why / fix` banner:

```
====================
  TRANSPILER ERROR
====================
  where : module 'X' > type 'Y' > field 'Z'
  why   : OPTIONAL is not covered by the paper's semantic model (§5.4 …).
  fix   : remove OPTIONAL or mark the field with a sentinel value.
```

The refused constructs are exactly the ones the paper calls out as
**outside its scope** or **semantically impossible** to preserve:

| Reason | Example |
| --- | --- |
| `INTEGER` range wider than any `int64` / `uint64` | `INTEGER (-1e20 .. 1e20)` |
| `REAL` without a value-range constraint (carrier set includes ±∞, NaN) | `REAL` |
| `OPTIONAL` SEQUENCE members | `SEQUENCE { a INTEGER, b BOOLEAN OPTIONAL }` |
| `DEFAULT` SEQUENCE members | `SEQUENCE { a INTEGER, b INTEGER DEFAULT 0 }` |
| `SEQUENCE` / `CHOICE` nesting deeper than `--max-depth` | deeply recursive types |
| Out-of-scope constructs (e.g. `EMBEDDED PDV`, `OBJECT CLASS`) | any of these |

## Recursion-depth limit

`SEQUENCE` / `CHOICE` / `SEQUENCE OF` can nest recursively. Per the
paper's Round-trip condition, a recursive type is fine as long as the
bijection holds at every level, but very deep types are almost always a
sign of a design error. The transpiler refuses anything deeper than
`--max-depth` (default `10`) with a `DepthExceeded` exception that names
the offending location.

## Project layout

```
.
├── asn1_to_yang.py          # project-root shim -> transpiler.cli
├── pyproject.toml           # src/ layout, console_scripts entry point
├── requirements.txt
├── examples/
│   ├── 01_basic_types.asn
│   ├── 02_sequence.asn
│   ├── 03_choice.asn
│   ├── 04_real_numbers.asn
│   └── 05_failure_modes.asn
└── src/transpiler/
    ├── __init__.py
    ├── __main__.py          # `python -m transpiler …`
    ├── cli.py               # CLI argument parsing + subprocess to pyang
    ├── asn1_parser.py       # thin wrapper over asn1tools.parse_string
    ├── rules.py             # ONE small function per paper rule
    ├── yang_builder.py      # walks the AST, applies rules, emits YANG
    └── errors.py            # TranspileError + four subclasses
```

The code is intentionally tiny so a human can read every line:

* **`rules.py`** — one function per ASN.1 type; each is ~5–15 lines and
  has a one-line docstring citing the paper section / equation.
* **`yang_builder.py`** — a 100-line walker that dispatches to `rules.py`
  and stitches the pieces into a YANG module.
* **`asn1_parser.py`** — a 60-line adapter that re-raises `asn1tools`
  errors as `Asn1SyntaxError` with the same `where / why / fix` shape.

## License & scope

This is a **toy** transpiler built to study the paper. It is **not**
production-ready. It implements a deliberate subset of ASN.1 — anything
outside that subset (EMBEDDED PDV, OBJECT CLASS, ANY/ANY DEFINED BY,
constraints on OCTET STRING etc. beyond SIZE, value-range constraints
on REALs beyond a single interval, …) raises a clear error.
