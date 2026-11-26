<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Re-derivation on correction: lineage-scoped invalidation, pushed to the client

---
Status: accepted
---

When a proposal is accepted, invalidation is scoped by **captured lineage** — only values
whose recorded lineage referenced the changed relationship are stale — backstopped by the
metric layer's concept→metric index to catch derived metrics computed from the changed
concept. Affected metrics are re-resolved and pushed to the dashboard over the existing
SSE stream; affected cards show a visible "updating from correction" state, then the new
value, updated lineage, and an audit note.

## Considered Options

- **Coarse (full-dashboard refresh):** rejected — wasteful and hides cause-and-effect.
- **Pull/TTL-only refresh:** rejected as the primary path — the correction ripple should
  be immediate and visible, which is the core trust moment.

## Consequences

- Re-derivation needs a small job/queue with **debounce/coalesce** so a burst of
  corrections doesn't thrash the resolver.
- A concept→metric reverse index must be maintained alongside the metric layer.
- Depends on resolve-time lineage capture (ADR 0004).

## Refinement (design review, 2026-06-12)

Sharpened against the as-built dashboard SSE (`dashboard.py::dashboard_stream` — a per-connection
generator fanning out via the in-process `mqtt_subscriber` queue, full snapshot every 30s) and the
ADR-0003 lifecycle.

### A. Only resolver-read corrections ripple — the MAPS_TO mapping correction

The Resolver reads the **`MAPS_TO` edge** (concept→column) and the **AuthorityRule**, not the
self-learned `ValueMapping`/`JoinPath`/`IdentityRule` nodes (implementation follow-up). So the rippling
correction is a **MAPS_TO mapping correction** — a new `proposed_type` `concept_mapping` flowing
through `record_user_correction` (one unified lifecycle, ADR 0003): accept repoints the
`MAPS_TO` edge for (system, concept) and bumps `r.version` (the ADR-0003 version path). **Authority
correction** is a sibling that also ripples (writes `AuthorityRule`) — a fast-follow on the same
machinery, not a co-headline. Accepting a self-learned cross-system proposal moves no card and
triggers no re-derivation.

### B. Affected cards by static index, not stored lineage

Lineage is ephemeral (ADR 0017) — captured on the value, never stored — so invalidation is **not**
done by replaying stored lineage. Cards→metrics→concepts are statically declared (CardLayer,
MetricLayer), so the affected set is a deterministic traversal: changed **concept** → the
**concept→metric reverse index** (incl. the derived-formula closure, so OEE is caught when
availability/performance/quality-rate change) → **metric→card** (CardLayer). The reverse index lives
in the MetricLayer. Ephemeral lineage still travels on the pushed value (provenance + consistency);
it is not the invalidation key.

### C. Dedicated in-process `card_events` broker + `card_update` SSE event

A small dedicated pub/sub broker (`subscribe()→Queue` / `unsubscribe()` / `publish(event)`), NOT an
overload of `mqtt_subscriber` (telemetry ≠ corrections). `dashboard_stream` drains it alongside the
mqtt queue and emits a new `card_update` SSE event. **In-process only** (single server process, like
the current fan-out); a multi-worker/multi-instance shared bus (Redis/SNS) is a documented
cloud-first-later deferral.

### D. In-process concept-keyed debounce/coalesce worker

The accept handler does the cheap part synchronously (write `MAPS_TO` + version bump) and enqueues
the **affected concepts**; an in-process worker debounces a short window (~300–500ms), unions the
pending concepts, computes the affected-card set once (B), re-resolves each affected card once, and
publishes one coalesced `card_update` batch. Coalescing is by concept-set union, so three
corrections that all feed OEE re-resolve the OEE card once. Re-resolution (sync DB work) is offloaded
off the event loop. Optional immediate `status:"updating"` emit on enqueue, then the resolved batch.

### E. Visible ripple + audit on the card's lineage expander

The push is additive to `renderCards` / the lineage expander: an affected card shows an
"updating from correction" state, then swaps value + lineage **in place** with a brief highlight (the
trust moment); unaffected cards are untouched (no coarse refresh). The **audit trail is the
proposal log** (the accepted correction with source/version/supersede chain) — no new store; a later change
surfaces a per-card breadcrumb ("updated from correction · mapping vN") read from it. The pushed value
carries its freshly-captured lineage, preserving the shown-number↔lineage consistency invariant.
The lineage-view **"correct this mapping" control lives on the dashboard card's lineage expander** —
the tightest see-wrong-number → fix → watch-it-ripple loop (literally what the acceptance gate describes).

### F. DRY reusable lineage element

A single reusable lineage-node renderer with an optional inline "correct this mapping" affordance
(extending the `_lineageNode` seam), reused wherever lineage renders, plus shared
correction-submit + `card_update`-handling client helpers — no duplicated lineage/correction markup.

### Scope — closes the acceptance gate

**In:** the `concept_mapping` MAPS_TO correction (route + `record_user_correction` path + `MAPS_TO`
repoint + version bump); concept→metric reverse index (MetricLayer) + metric→card (CardLayer);
`card_events` broker + `card_update` SSE; debounce/coalesce re-derivation worker; the dashboard-card
lineage-expander correct-control + visible updating→value ripple + audit breadcrumb; reusable
lineage element. Offline-testable core; live SSE/graph gated.

**Deferred (do not lose):** authority-correction ripple (fast-follow, same machinery);
multi-instance shared event bus (cloud-first-later); `value_mapping_version` population (no
resolve-time read site); field-level authority override (CONTEXT Authority).
