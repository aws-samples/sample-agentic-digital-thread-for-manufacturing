<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# pg8000 Lambda layer

`stacks/adt_seed_data_stack.py` builds this first-party CDK file asset during
synthesis. The bundler installs the exact, hash-verified wheels of
`pg8000==1.31.6` and its dependencies (`scramp`, `asn1crypto`,
`python-dateutil`, `six`) for CPython 3.12 into the Lambda layer's required
`python/` directory, and copies `pgdriver.py`, the sample's PostgreSQL client
module, beside them. The functions import `pgdriver`. No external Lambda
layer publisher or AWS account is used.

The lock is generated from `requirements/pg8000-layer.in` with the
`uv pip compile` command in `requirements/README.md`. The build uses `--require-hashes`,
`--only-binary=:all:`, explicit platform/interpreter/ABI flags, disables bytecode,
and normalizes output timestamps and modes before CDK stages the asset.

`pgdriver.py` is one of four identical copies of the module
(`tests/test_pgdriver_copies.py`); change the app's copy,
`agentic_digital_thread/agents/pgdriver.py`, and copy it here.

The packages are consumed unmodified under their own licenses: pg8000
(BSD-3-Clause, <https://codeberg.org/tlocke/pg8000>), scramp (MIT-0,
<https://codeberg.org/tlocke/scramp>), asn1crypto (MIT,
<https://github.com/wbond/asn1crypto>), python-dateutil (Apache-2.0 AND
BSD-3-Clause, <https://github.com/dateutil/dateutil>) and six (MIT,
<https://github.com/benjaminp/six>).
