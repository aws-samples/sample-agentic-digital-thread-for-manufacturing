<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0094: A defined metric whose authority is held per site is answered per site

## Status

**Proposed: 2026-10-02.** Amends [ADR-0078](0078-defined-metric-terms-answered-by-the-metric-layer.md)
D9 (no site fan-out of the notice) and
[ADR-0045](0045-multi-site-metric-fan-out.md) Decision 3 (fan-out only inside
`_build_cards`).

## Context

Onboarding mints one `AuthorityRule` per system for every concept its class
owns (`onboard._dp_authority_rules`), and retires a competing claim only at the
SAME plant (ADR-0045). A deployment with one MES per plant therefore carries
one `quality-metric`, `oee-score`, `availability`, `throughput`, ... rule per
plant. Measured on a four-site live deployment (2026-10-02): 26
concepts have 2+ active rules; 14 of them have exactly one rule per plant's
MES and no other system.

`GraphAuthorityResolver` ignores plant, so the scalar resolve of every metric
over those concepts fails loud ("ambiguous authority ... 4 active
AuthorityRules name different systems"). ADR-0078 classifies that as a
`withheld / unavailable` block and the prompt rule forbids any substitute. The
result: "Compare quality pass rates across Milwaukee, Detroit, and Cedar
Rapids" (a built-in suggested prompt) is answered with a refusal on every
multi-site account, although each plant's value is well defined. It also
failed one golden run and is the one strict-smoke miss on the live account.

The dashboard does not have this problem: its cards fan out per site with the
authority pinned (`metric_multisite.resolve_metric_multi`, ADR-0045). Only the
per-turn notice withholds.

## Decision

### D1 — The ambiguity is typed

`GraphAuthorityResolver` raises `AmbiguousAuthorityError`, a subclass of
`MetricResolverError` carrying `concept` and the sorted `system_ids`. Every
existing handler sees the same class and message; the scalar resolve still
fails loud. A derived metric wraps it (`from cause`); callers find it through
the cause chain.

### D2 — Only a per-site partition fans out

`metric_turn_sites.site_partition` accepts the named systems only when ALL
hold:

- each is an onboarded catalog entry;
- each is SITED (`metric_multisite.site_of` non-empty: not `all`, not empty);
- the sites are pairwise different (case-insensitive);
- there are 2 to `MAX_SITES` (6, the notice's list cap) of them.

Anything else (an unsited or unknown system, two systems at one site) is a
genuine conflict and keeps today's `withheld / unavailable` block, reason
verbatim. A fan-out fault of any kind also keeps it.

### D3 — Each site is resolved and classified exactly like a scalar

For a partition, the notice resolves the metric once per site with
`PinnedAuthority(system_id)` (the `metric_multisite` pin) inside the same
`:metric-turn` execution trace, and classifies each site with the notice's own
`_classify` / `_classify_error` / derived-window withhold. A site is
`computed`, `withheld` (status and detail verbatim) or `result_unavailable`
(exception class only). The result is the outcome `computed_by_site` with the
concept, the sites sorted by site, the query count and the systems.

No cross-site value is combined. The block says so and forbids the model from
totalling or averaging the sites (the dashboard's simple-mean aggregate is a
presentation choice, not a definition, and a combined rate needs per-site
weights the metric layer does not carry).

### D4 — The runtime re-sanitizes and renders it

`computed_by_site` joins `OUTCOMES`. Each site is re-bounded through
`sanitize_result`, plus a non-empty `site` and `system_id`; a malformed,
nested, empty or over-cap site list fails CLOSED to the invalid marker. The
block (`render_sites_block`) states the shared authority, window and lineage
once when every computed site agrees (lineage with each site's own system id
stripped), one line per site, the per-site rule line, and the PERMITTED /
NOT AVAILABLE line of its shape. The closing scope sentence counts each
computed site's window. `REQUIRED_RESPONSE` and `MAX_NOTICE_CHARS` are
unchanged; a large block drops from the tail like any other.

### D5 — ADR-0045 Decision 3, amended

The per-site fan-out may also run inside the ADR-0078 notice, which is
TTL-cached per metric (180 s, the same generation-guarded cache), timeboxed
(12 s) and runs on its own 2-thread pool: cost is bounded per metric per TTL,
never per viewer or per SSE tick. It still never runs on a plain request path.

## Consequences

- Cross-plant metric questions on multi-site accounts get each plant's value
  from the metric layer instead of a refusal. Single-authority deployments are
  byte-identical (no ambiguity, no fan-out).
- A partition costs up to `MAX_SITES` resolves per metric per TTL; a slow
  source can hit the 12 s timebox, which yields the existing forbidding
  `result_unavailable (timebox)` block and caches the late result.
- A genuine authority conflict is still withheld; the steward fix is
  unchanged (deactivate the wrong rule or `set_authority`).

## Verification

`tests/unit/test_metric_turn_sites.py`: the typed raise; the cause-chain
lookup; every partition rule; per-site classification incl. no-data, a
resolver error and a non-resolver error; a conflict and a fan-out fault keep
the verbatim withhold; the runtime round trip and fail-closed sanitation; the
notice bound with three maximal blocks. Live: the cross-plant smoke prompt and
the golden lane on a four-site deployment.

## Amendment 2026-10-02: each site needs ONE binding

The first live run on a deployed workshop partitioned correctly,
then every site withheld as `ambiguous_binding`: onboarding had mapped
`quality_checks.pass_rate`, `pass_count`, `fail_count` and the work order
`good_quantity` all to `quality-metric`, so no site had one column for the
quality metrics. The map prompt caused it: `catalog.MAPPING_DISAMBIGUATION`
sent the 0-100 pass rate to `quality-metric`, and that concept's definition
listed the rate columns too, while the metric and card layers read the rate
from `quality-yield-rate` (`oee-quality` prefer_stored, the `quality-by-line`
card). The hint and the definition now send a 0-100 pass/yield rate to
`quality-yield-rate` and keep `quality-metric` for counts and scores
(`tests/unit/test_catalog_isa95_synonyms.py`). An account onboarded before
this needs its MES systems re-onboarded. Live check: the cross-plant golden
and smoke prompt after re-onboarding the four MES systems.

## Amendment 2026-10-04: the dashboard's grouped panels and the fan-out aggregate

The Context above said the dashboard did not have this problem. Its scalar
cards do not; its two other paths did, both seen on a fresh four-plant
deployment:

- **Grouped panels** (`POST /api/metrics/resolve-by-dimension`, "Quality by
  line" and the per-line throughput chart) resolved through the unpinned
  `GraphAuthorityResolver` and rendered "ambiguous authority ... 4 active
  AuthorityRules" with a Retry button, although each plant's MES answered three
  lines when pinned. The route now applies D2: an unpinned resolve that fails
  on a per-site partition is resolved once per site with `PinnedAuthority`, and
  the envelope carries one bar per `"<site> · <dimension value>"`, the per-row
  lineage of each plant, `system_id: null` and a `sites` breakdown (`ok` with
  its row count, `uncovered` with the missing concept, or `error`). Every site
  uncovered is the uncovered envelope; no site answering keeps the original
  error. A genuine conflict (two systems at one plant) keeps the error verbatim,
  and a pinned request never fans out (`_grouped_per_site`,
  `tests/unit/test_resolve_by_dimension_route.py`).
- **The scalar fan-out** (`metric_multisite.resolve_metric_multi`) counted a
  site whose value was WITHHELD (`ambiguous-binding`, `unresolved-predicate`)
  as "nothing read". When every plant withheld (`machine-speed` binds to two
  columns on each MES) it raised, `dashboard._build_card_metric_multi` fell
  back to the single-authority path, and that path died on the partition and
  showed the authority message instead of the binding one. A withheld site is
  now a READ site; the aggregate adopts the withheld register (invariant first,
  then the two steward choices) and its `detail` names every withholding system
  and the shared reason, so the card reads "Withheld — the concept binds to
  several columns; a steward must choose one: ... on mes-a, mes-b" with no
  Retry (`tests/unit/test_multisite_honest_no_data.py`). Per-site entries carry
  the resolver's `detail` when withheld.

## Amendment 2026-11-02: a site the metric layer cannot read is withheld by name

A live check onboarded a Tulip workspace as a fifth site. Its OEE Summary
table maps `oee-score`, `availability`, `performance` and the quality
concepts, so the OEE partition included it, and the notice rendered that site
as `UNAVAILABLE (ValueError)`. The resolver had composed SQL for a system on a
connection type it has no reader for (`connection_types.structured_read_supported`
is false: the REST vendor registry gives Tulip and generic OpenAPI no
`structured_reader`). Such a system's catalog schema is empty and its table
names are vendor labels, so `db.validate_identifier` raised a bare
`ValueError` outside the resolver's error contract: a derived metric's
`prefer_stored` read did not fall through, and the dashboard's grouped route
let it escape `_grouped_per_site`. With plain names the SQL went to
`query_router.query_system`, which cannot run it (generic OpenAPI sends it as
a GET path).

The resolver now refuses such a system before composing any query
(`metric_resolver.require_metric_reader`, called by the scalar, grouped and
heatmap paths) with `UnreadableSystemError`, a `StructuralMetricError` naming
the system and its connection type. Each caller's existing register applies:
the per-site notice shows that site as `WITHHELD — status 'unavailable'` with
the reason, the grouped route answers the readable sites and marks that one
`error` with a `detail` naming the system and its connection type (when no
site can be read, the envelope's structural error names every such system
instead of the authority split), and nothing is sent to the source. Readable connection types are
unchanged. The dashboard fan-out already left these systems out
(`metric_multisite.systems_covering`, `test_dashboard_skips_unreadable_systems.py`).
The metric layer still computes no value for such a site;
reading it would need a vendor reader in the registry
(`tests/unit/test_metric_layer_reads_no_unreadable_system.py`).
