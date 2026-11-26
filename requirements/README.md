<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Python deployment locks

The human-maintained dependency inputs are `infra.in`, `runtime.in`,
`pg8000-layer.in`, `lambda-db-init.in`,
`../erp_simulation/requirements-simulation.txt`,
`../erp_simulation/requirements-erp-streamlit.txt`, and
`../agentic_digital_thread/pyproject.toml`. The generated deployment locks are:

| Context | Lock | Target |
|---|---|---|
| CDK synth/packaging | `../requirements.txt` | CPython 3.12, Linux x86_64 |
| ADT application | `../agentic_digital_thread/requirements/app-py312-linux-arm64.lock` | CPython 3.12, Linux ARM64 |
| AgentCore runtime | `../agentic_digital_thread/requirements/runtime-py312-linux-arm64.lock` | CPython 3.12, Linux ARM64 |
| Seed Lambda pg8000 layer (also the 42Q and Tulip mocks') | `../lambda-layers/pg8000/requirements-py312-linux-x86_64.lock` | CPython 3.12, Linux x86_64 (pure-Python wheels, also installed for ARM64) |
| ERP database init Lambda | `../lambda-db-init/requirements-py311-linux-arm64.lock` | CPython 3.11, AL2 Linux ARM64 (manylinux2014) |
| ERP simulation Lambdas | `erp-simulation-py311-linux-arm64.lock` | CPython 3.11, Linux ARM64 |
| ERP Streamlit dashboard | `erp-dashboard-py311-linux-arm64.lock` | CPython 3.11, Linux ARM64 |

Every lock ships pinned to exact versions with `--hash` entries, and every
build installs it with `--require-hashes`, so you don't need to regenerate a
lock to build or deploy.

To refresh a lock, install the resolver `uv==0.9.15` and run its command from
the repository root. Unset `UV_INDEX*`, `PIP_INDEX_URL` and
`PIP_EXTRA_INDEX_URL` first, so a private or credential-bearing index can't
leak into a lock:

```bash
UV_LOCK_FLAGS="--default-index https://pypi.org/simple --exclude-newer 2026-11-16T23:59:59Z --generate-hashes --no-build --no-header --no-annotate --no-config --upgrade"
uv pip compile requirements/infra.in --output-file requirements.txt \
  --python-version 3.12 --python-platform x86_64-manylinux2014 $UV_LOCK_FLAGS
uv pip compile agentic_digital_thread/pyproject.toml \
  --output-file agentic_digital_thread/requirements/app-py312-linux-arm64.lock \
  --python-version 3.12 --python-platform aarch64-manylinux_2_28 $UV_LOCK_FLAGS
uv pip compile agentic_digital_thread/pyproject.toml requirements/runtime.in \
  --output-file agentic_digital_thread/requirements/runtime-py312-linux-arm64.lock \
  --python-version 3.12 --python-platform aarch64-manylinux_2_28 $UV_LOCK_FLAGS \
  --exclude-newer-package aws-opentelemetry-distro=2026-11-18T23:59:59Z
uv pip compile requirements/pg8000-layer.in \
  --output-file lambda-layers/pg8000/requirements-py312-linux-x86_64.lock \
  --python-version 3.12 --python-platform x86_64-manylinux2014 $UV_LOCK_FLAGS
uv pip compile requirements/lambda-db-init.in \
  --output-file lambda-db-init/requirements-py311-linux-arm64.lock \
  --python-version 3.11 --python-platform aarch64-manylinux2014 $UV_LOCK_FLAGS
uv pip compile erp_simulation/requirements-simulation.txt \
  --output-file requirements/erp-simulation-py311-linux-arm64.lock \
  --python-version 3.11 --python-platform aarch64-manylinux_2_28 $UV_LOCK_FLAGS
uv pip compile erp_simulation/requirements-erp-streamlit.txt \
  --output-file requirements/erp-dashboard-py311-linux-arm64.lock \
  --python-version 3.11 --python-platform aarch64-manylinux_2_28 $UV_LOCK_FLAGS
```

To check that the locks still match their inputs, run the commands, then
`git diff --exit-code` on the lock files: no diff means no drift.

The commands use public PyPI only, resolve wheels only (`--no-build`),
generate hashes, and use the fixed `2026-11-16T23:59:59Z` upload cutoff
(`--exclude-newer`). Pinning plus `--require-hashes` is the
real supply-chain defense; the cutoff only fixes the resolution date so locks
are byte-reproducible. It is not "never update" — advance it on a reviewed
refresh and regenerate, so dependency updates land deliberately instead of
silently on every CI run.

**Refresh cadence.** Move the cutoff forward and regenerate on a regular
schedule (and whenever a security scan flags a locked package), then re-run
`pip-audit` against both locks to confirm the advisory count stays near zero.
The 2026-11-16 refresh advanced the cutoff from the original `2026-08-25` pin,
which had drifted ~83 days stale and was carrying advisories in `pyjwt`,
`python-jose`, `virtualenv`, `strands-agents` and others. That refresh also
retired the former per-package exceptions (ADOT `2026-10-30`, duckdb
`2026-10-21`): both resolved under the global cutoff. One exception has been
added since: the runtime lock takes `aws-opentelemetry-distro` 0.23.0
(uploaded 2026-11-18) for the `opentelemetry-api` 1.45.2 fix, with
`--exclude-newer-package aws-opentelemetry-distro=2026-11-18T23:59:59Z` on
that lock's command only; every package it pins predates the cutoff.

**Exceptions.** If a required release postdates the cutoff, let only that
package past it with `--exclude-newer-package <name>=<date>` on the one lock
that needs it. Never use a wildcard or an open bump for unrelated packages.

**Security floors.** The same test file's `SECURITY_FLOORS` names, for each
selected package, the lowest release that clears its security advisories. A lock may
carry a newer release, never an older one. The former per-package
`SECURITY_FIX_CUTOFFS` are gone: every entry fell before the 2026-11-16 cutoff.

Validate target wheels without installing:

```bash
python3.12 -m pip download --require-hashes --only-binary=:all: \
  --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 \
  --platform manylinux_2_17_aarch64 \
  --implementation cp --python-version 3.12 \
  --abi cp312 -r agentic_digital_thread/requirements/app-py312-linux-arm64.lock \
  -d /tmp/adt-wheels
```

`strands-agents==1.48.0` is load-bearing and must remain exact in the app and
runtime locks; see `agentic_digital_thread/pyproject.toml`.

## Offline dependency-license gate

Normal verification consumes only checked-in locks, policy, inventory, and
attribution text; it invokes no package manager and makes no network request:

```bash
python3 scripts/check_dependency_licenses.py --check
```

`config/dependency-license-policy.json` declares all seven Python locks, all
five npm locks, reviewed SPDX identifiers, exact metadata evidence overrides,
and narrow package/version exceptions (none at present).
`config/dependency-licenses.json` is the canonical exact coordinate inventory.

Only an intentional dependency refresh may contact public registries. Use a
credential-blind environment, regenerate locks, refresh authoritative published
wheel Core Metadata/npm lock facts, review the complete policy/inventory/NOTICE delta, and
then run audits separately:

Regenerate the Python locks with the `uv pip compile` commands above, then:

```bash
: >/tmp/adt-empty-user.npmrc && : >/tmp/adt-empty-global.npmrc
(cd highbyte/components/opc-server && \
  npm_config_registry=https://registry.npmjs.org/ \
  npm_config_userconfig=/tmp/adt-empty-user.npmrc \
  npm_config_globalconfig=/tmp/adt-empty-global.npmrc \
  npm install --package-lock-only --ignore-scripts)
python3 scripts/check_dependency_licenses.py --refresh   # NETWORKED
python3 scripts/check_dependency_licenses.py --check     # OFFLINE
(cd highbyte/components/opc-server && \
  npm_config_registry=https://registry.npmjs.org/ \
  npm_config_userconfig=/tmp/adt-empty-user.npmrc \
  npm_config_globalconfig=/tmp/adt-empty-global.npmrc \
  npm ci --omit=dev && npm audit --omit=dev --audit-level=high) # NETWORKED
```

The refresh fails closed when authoritative metadata is absent or ambiguous.
Resolve such a package with exact wheel/published-tarball evidence in a
coordinate-specific `metadata_overrides` entry; never add an unknown-license or
family-wide exception.
