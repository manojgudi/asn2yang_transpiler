# asn1-to-yang-transpiler

A prototype ASN.1 → YANG transpiler that implements the rules from the
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

### Prerequisites

* Python ≥ 3.10
* `asn1tools`, `pyang` (Python), `libyang3` / `yanglint` (system)

### Steps

```bash
# 1. Clone and enter the repo
git clone <repo-url> asn_yang_transpiler
cd asn_yang_transpiler

# 2. System packages (Debian / Ubuntu)
sudo apt install -y python3-pip libyang2-tools

# 3. Python dependencies
pip install -r requirements.txt
# or, for editable install (also adds the `asn1-to-yang` console script):
pip install -e .
```

This puts three ways to invoke the transpiler on your `$PATH`:

* `python -m transpiler ..`
* `asn1-to-yang …`
* `python asn1_to_yang.py ..`

### Sanity check

```bash
# Should print a YANG module header and a list of typedefs.
python -m transpiler transpile examples/01_basic_types.asn

# Should print "Y Y Y Y" for each of the 5 working examples.
python3 validate_data.py
```

If `python3 validate_data.py` does not produce all `Y` in the four
columns, see **Troubleshooting** below.

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

## Testing

The repo ships two complementary test harnesses. Both run **without
modifying the transpiler source** — they exercise the transpiler as an
external artefact.

### 1. Test YANG generation

The transpiler reads an ASN.1 module and writes YANG. To verify the
output is well-formed and semantically correct:

```bash
# Transpile a single example, with pyang checking the output.
python -m transpiler transpile examples/02_sequence.asn --check-yang

# Same, but pipe the YANG to stdout for inspection:
python -m transpiler transpile examples/02_sequence.asn

# Transpile all examples in one go:
for f in examples/0*.asn; do
    echo "=== $f ==="
    python -m transpiler transpile "$f" 2>&1 | head -25
done
```

What you should see for `01_basic_types.asn`:

```yang
module BasicTypes {
  yang-version 1.1;
  ...
  typedef Temperature { type int16 { range '-50..150'; } }
  typedef TinyCount { type uint8 { range '0..255'; } }
  typedef Greeting { type string { length "1..40"; } }
  ...
}
```

Programmatic equivalent (used by `validate_data.py`):

```python
from transpiler.asn1_parser import parse_asn1
from transpiler import transpile

ast = parse_asn1(open("examples/01_basic_types.asn").read())
yang_text = transpile(ast)
```

### 2. Test JSON instances against both ASN.1 and YANG models

For each working example, `examples/data/` ships a pair of JSON files:

* `data_NN.json`     — an instance expected to satisfy **both** the
  ASN.1 model and the transpiled YANG.
* `data_NN_bad.json` — an instance that violates exactly one constraint
  (range, enumeration membership, size, choice alternative), expected to
  be rejected by **both** validators.

The harness `validate_data.py` runs four checks per example:

| Column | What it tests |
| --- | --- |
| `ASN.1 cons valid` | independent ASN.1 constraint check accepts `data_NN.json` |
| `ASN.1 cons bad` | independent ASN.1 constraint check rejects `data_NN_bad.json` |
| `YANG valid acc` | libyang's `yanglint` accepts `data_NN.json` |
| `YANG bad rej` | libyang's `yanglint` rejects `data_NN_bad.json` |

Run it:

```bash
# All 5 working examples:
python3 validate_data.py

# A single example:
python3 validate_data.py 02

# Several examples:
python3 validate_data.py 01 02 03
```

Expected output (all `Y` in every column):

```
example                      ASN.1 cons valid   ASN.1 cons bad  YANG valid acc  YANG bad rej
--------------------------------------------------------------------------------------------
01_basic_types                              Y                Y               Y             Y
02_sequence                                 Y                Y               Y             Y
03_choice                                   Y                Y               Y             Y
04_real_numbers                             Y                Y               Y             Y
06_optional_and_default                     Y                Y               Y             Y
```

These are **consistency sanity checks**: a valid instance is accepted by
both validators and a bad instance is rejected by both. They are not a
proof of semantic preservation -- the proof of the institution morphism
in the paper rests on the CafeOBJ proof scores in §5, not on these
JSON tests.

#### Sanity-checking the harness

The harness is not just rubber-stamping — try breaking it on purpose:

```bash
# Make data_02_bad.json valid (change id=0 back to id=12345):
sed -i 's/"id": 0/"id": 12345/' examples/data/data_02_bad.json
python3 validate_data.py 02
# Expected: 'YANG bad rej' becomes N — the bad instance is now accepted.

# Make data_01.json violate ENUMERATED membership:
sed -i 's/"Color": "green"/"Color": "purple"/' examples/data/data_01.json
python3 validate_data.py 01
# Expected: 'ASN.1 cons valid' becomes N with the violation message.

# Restore the fixtures (git) before committing:
git checkout -- examples/data/
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
  why   : INTEGER range [-1e20, 1e20] does not fit any YANG integer …
  fix   : split the range into multiple fields or constrain the source range.
```

The refused constructs are exactly the ones the paper calls out as
**outside its scope** or **semantically impossible** to preserve:

| Reason | Example |
| --- | --- |
| `INTEGER` range wider than any `int64` / `uint64` | `INTEGER (-1e20 .. 1e20)` |
| `REAL` without a value-range constraint (carrier set includes ±∞, NaN) | `REAL` |
| `SEQUENCE` / `CHOICE` nesting deeper than `--max-depth` | deeply recursive types |
| Out-of-scope constructs (e.g. `EMBEDDED PDV`, `OBJECT CLASS`) | any of these |

## `OPTIONAL` and `DEFAULT` support (extension per `optional.txt`)

The paper's §5.4 restricts SEQUENCE members to mandatory-only. We
extend this per the rephrase in [`optional.txt`](optional.txt):

| ASN.1 | YANG sub-statement |
| --- | --- |
| mandatory field | `mandatory true;` |
| `OPTIONAL` field | *(nothing — YANG leaves are optional by default)* |
| `DEFAULT v` field | `default "v";` |
| CHOICE (non-optional alternatives, per §5.5) | `mandatory true;` on the `choice` line, *not* on individual `case` lines |

See `examples/06_optional_and_default.asn` for a runnable demonstration.

## Recursion-depth limit

`SEQUENCE` / `CHOICE` / `SEQUENCE OF` can nest recursively. Per the
paper's Round-trip condition, a recursive type is fine as long as the
bijection holds at every level, but very deep types are almost always a
sign of a design error. The transpiler refuses anything deeper than
`--max-depth` (default `10`) with a `DepthExceeded` exception that names
the offending location.


## License & scope

This is a **prototype** transpiler built to study the paper. The code was implemented with assistance from a large language model.
Distributed under MIT LICENSE.
