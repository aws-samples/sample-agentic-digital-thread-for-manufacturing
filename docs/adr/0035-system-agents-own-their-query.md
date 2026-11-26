<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# System agents own their query composition (agent-owned query mode)

---
Status: accepted
---

> Implementation status: **implemented.** Both query modes are in the code and
> `agent` is the default `SYSTEM_QUERY_MODE` (`agents/orchestrator.py`,
> `deploy.sh`); `orchestrator` stays selectable for A/B runs. This ADR records
> the design decided in the 2026-07-02 design review; its acceptance criteria
> and scope boundary are below.

## Context

`system_agent.create_mcp_server` dispatches by `connection_type`, and the pattern
is **not uniform**:

- **PostgreSQL** and **MCP Server** already wrap `create_system_agent` (a **Nova
  LLM agent**) and expose a coarse **`query_<id>(question)`** NL tool on the
  gateway. PostgreSQL *also* exposes a raw `execute_sql_<id>(sql)` "fast path"
  that bypasses the LLM.
- **Athena, SiteWise (Direct), SiteWise (i3X), REST API** take an **early-return
  raw-tool branch** — they register structured tools (`athena_run_query(sql)`,
  `sitewise_*`, i3X tools) directly on the MCP server with **no LLM agent**.

Because the raw-tool connectors expose only low-level SQL/structured tools, the
**orchestrator** (Claude Sonnet) must compose every query for them. To make that
work it accumulated prescriptive query guidance — call `get_system_schema` first,
use exact columns, `CAST` from reported types, filter partitions first, use real
table names. That guidance bloated the orchestrator, inflated tool-call counts
(~43 on a multi-hop question — per-asset SiteWise describe loops + Athena
re-querying), and let quantitative questions be answered from **graph metadata
instead of a live query** (the Q1/Q6 grounding gap). The root problem is the
**mix**: some systems are agent-owned, some are orchestrator-owned.

Critically, `create_system_agent` is **already polymorphic** across every
connection type — it builds the correct internal toolset (`make_athena_tools`,
`_make_sitewise_direct_tool`, `_make_i3x_tools`, `MCPClient`, the SQL tool) and
wraps it in a Nova agent. The capability exists; only the `create_mcp_server`
dispatch withholds it from four connectors.

## Decision

Adopt a **single coherent query model**, selectable by a provision-time toggle
`SYSTEM_QUERY_MODE` (`agent` | `orchestrator`), **default `agent`**, and **never
a mix**.

### `agent` mode (Mode A — the target)

- **Every data system is a per-system LLM agent.** Remove the early-return
  raw-tool branches in `create_mcp_server` so Athena / SiteWise (Direct) /
  SiteWise (i3X) / REST flow through the same `create_system_agent →
  query_<system>(question)` path PostgreSQL/MCP already use. No new agent code —
  `create_system_agent` is already polymorphic.
- **Expose only `query_<system>(question)`** to the orchestrator. Demote the raw
  tools (`athena_run_query`, `sitewise_*`, i3X, and `execute_sql_<id>`) to the
  agent's **internal** toolset. **Remove the `execute_sql` bypass entirely** — the
  orchestrator must never compose system SQL.
- **Per-system configurable model** (default Nova Lite at the time of this ADR;
  since superseded by the model catalog's AGENT default — Claude Haiku 4.5, see
  `agents/model_catalog.py` — via `SYSTEM_AGENT_MODEL`,
  overridable per system), because Athena SQL (partitions, `CAST`, nested
  structs) is harder than PostgreSQL. **In-agent self-recovery** on
  `COLUMN_NOT_FOUND` / `TYPE_MISMATCH` (re-introspect own schema + retry) is a
  **hard requirement** — the recovery mechanism lives where the schema knowledge
  is.
- **Knowledge is scoped:** the system agent owns/consumes its **system-local**
  quirks/learnings; the graph owns **cross-system** relationships (identity /
  value mappings, correlations) consumed by the orchestrator. The orchestrator
  translates identifiers via the graph and delegates an **intent-level,
  pre-translated** NL sub-question — no column names, casts, or SQL hints.
- **System-local guidance delivery:** stored per `system_id` in a **mutable fast
  store** carrying a monotonic `version`. The agent consults the store on the
  **per-question path** via a **cheap backend version check**, reloading cached
  guidance only when the persisted version changes — so a writer bump in one
  runtime is picked up by a **separate reader runtime without a container roll**
  (the version is authoritative in the backend, not in-process). Guidance is
  injected as a **labeled non-authoritative** prompt section that cannot override
  the grounding/self-recovery rules. Learning updates cause **no runtime roll and
  no downtime**.
- **Orchestrator's reduced role:** decompose → route via `explore_graph` →
  translate identifiers → delegate NL to `query_<system>` → synthesize + ground.
  **Delete all SQL-composition guidance and remove `get_system_schema` (physical
  columns) from the orchestrator's toolset**; keep `explore_graph` (concept-level
  routing). Hard rule: **quantitative / aggregate / current-state facts MUST come
  from a `query_<system>` result — never from graph metadata alone.**

### `orchestrator` mode (Mode B — retained for A/B)

No per-system LLM; the orchestrator uses a small set of **generic,
connection-type-agnostic** tools (`describe_schema`, `run_query`, `connect`) and
owns querying itself. Kept **internally coherent** (not mixed) so it can be
compared against Mode A on the scorecard.

## Considered options

- **Mode A (system-agent-owned) — chosen as default/target.** Distributes query
  knowledge to where the schema lives, grounds by construction (delegation = a
  live query), keeps the orchestrator small/generic, and scales as customers
  connect many divergent systems. Costs: a nested-LLM hop per sub-question
  (latency/cost; sub-questions parallelizable) and Nova-Lite SQL-quality risk
  (mitigated by per-system model choice + in-agent self-recovery).
- **Mode B (orchestrator-owned, generic tools) — retained behind the toggle.**
  One LLM (cheaper, faster, stronger SQL), but centralizes all system knowledge in
  one brain that does not scale to many heterogeneous systems, and grounds only by
  discipline.
- **Status quo (accidental mix) — rejected.** It is the source of the guidance
  bloat, the ~43-call multi-hop, and the grounding gap.
- **Decide by measurement.** `SYSTEM_QUERY_MODE` lets us A/B the two modes on the
  7-question scorecard (tool-call count, first-attempt error rate, grounding,
  latency, cost) rather than argue theoretically.

## Consequences

- `create_mcp_server` loses the raw-tool early returns; the four connectors gain
  an LLM agent with no new agent-factory code.
- The orchestrator prompt shrinks materially and `get_system_schema` leaves its
  toolset; **routing must be fully served by `explore_graph`'s concept-level
  view** — verify no gap before deleting physical schema access.
- Adds a nested-LLM hop per delegated sub-question.
- Preserves the prior deployed fixes — `CONNECTION_SECRET_ARN` resolution on the
  runtime, gateway target sync (SYNCHRONIZE_UNSUCCESSFUL), unconditional
  VPC/Neptune connectivity, cross-account role assumption. The runtime instantiates
  an agent instead of a tool server; the gateway contract changes from many raw
  tools to one `query_<system>` tool per system.
- Learning delivery pairs with [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md);
  the mutable per-system store + version check is shared.
- **Acceptance criteria** (from the design review):
  - Orchestrator query-guidance for columns/casts/partitions/schema-first is **removed** (lives in system agents).
  - A system agent answers a per-system sub-question by running a **live** query and **self-recovers** its own column/type errors.
  - Quantitative/aggregate/"current" questions ALWAYS hit a live source.
  - Tool-call count per cross-system question materially reduced vs. the baseline.
  - Learning is post-turn and (if retained) human-elicited; a closed-loop test proves learn→improve
    (see [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md)).
  - No dataset-specific literals; base-mode purity preserved.
  - The onboarding agent never fabricates systems: an empty catalog yields "none connected yet," never invented vendors.
- **Scope boundary — consume, don't reimplement.** ADT is the intelligence/query plane over a
  source's data plane. It connects to and queries source *output surfaces* generically via
  `connection_config` (`role_arn`/`external_id`/`region` for AWS sources; `url`/`token`/`auth_scheme`
  for MCP) and must **not** reimplement a source's ingestion/modeling/UNS pipelines.

## Amendment 2026-09-26 — confirmed JoinPath endpoints may cross the delegation boundary

**Context.** A steward-confirmed JoinPath (`mes-mil work_orders.work_order_number →
erp lot_mes_tracking.batch_id`) was in the route seed, but the Mode-A orchestrator
first sent MES *lot* tokens to ERP, got an `IDENTIFIER-MISS`, and only then recovered
through the work-order join (golden `g-wo-lot-supplier-trace`: 1 of 5 runs passed before
table-qualified endpoints, and the detour still appeared afterwards). With only a bare field
name, ERP could not tell which of its tables the carried values belonged to.

**Decision.**
- The route seed's `join_paths` / `identity_rules` summaries carry `method`, and JoinPath
  endpoints ride table-qualified (`table.field`, the rule's OWN stored endpoints — ADR-0011).
- The ROUTE mini-block gains an optional `join_rule:` line: the confirmed JoinPath's id and
  `system.table.field → system.table.field` endpoints, copied verbatim from the seed. This
  is the ONLY physical name allowed in a delegation. It is steward-confirmed routing
  metadata, not an orchestrator-composed query, and it tells the target which of its OWN
  fields holds the carried values. The target still owns composition and self-recovery.
- Mode A prefers a `user_confirmed` / `crosswalk_confirmed` JoinPath over identifier
  guessing: fetch the join's from_field values from the source, then delegate
  sequentially with them.

**Measurement.** `eval_answers` rows carry the COMPUTED `metrics.identifier_miss_calls`
(fenced IDENTIFIER-MISS blocks in public tool_result summaries; absent when unmeasured).
It is not part of the pinned 13-key A/B diff.

**Unchanged.** No other column names, casts or query fragments; Mode B is unaffected
(the rule lives only in `_MODE_A_DELEGATE_BLOCK`).

**Note 2026-10-05: measured exact joins count as confirmed.** Since ADR-0087 the
seed ships no curated join; the MES -> ERP hops are derived by the exact-agreement
pass and carry `value_exact_match` (identical values measured in both systems,
reviewed at the Identity Rules checkpoint). The Mode-A text named only
`user_confirmed` / `crosswalk_confirmed`, so the hop the workshop's module 2 follows
was outside the letter of the policy even though the model used it. `join_rule:` and
"confirmed joins beat identifier guesses" now name all three methods. Other
methods (`onboarding-llm`, `value_core_match`, `foreign_key_match`) stay excluded.

## Amendment 2026-10-08 — route-seed budget 10,000

**Evidence.** An offline run of the production `build_route_seed` over the
workshop's own registration manifest (the systems a fresh workshop account has
at module 6 and module 8, with and without HighByte; inventory from
`agents.demo.systems` and `agents.demo.workshop`, rules shaped like the captured
live seeds) found the 8,000-character seed already over budget on main at module
6. The shrink pass cuts the longest prose field first, so the plant MES's
registration guidance (570 fenced characters) was cut, then dropped entirely at
module 8, and the shared PostgreSQL default went to nothing while five or six
systems still pointed at it. With HighByte as a ninth or tenth system, SiteWise's
base guidance ("The ONLY sensor source …") went too. Every tier-1 rule survived
in every scenario: prose yields before evidence-backed rules by design (R3). The
replay reproduced, per system, the seed captured on a deployed workshop on 2026-10-07.

Smallest budget (100-character steps) at which every registration base and the
module 6 SiteWise note arrive whole:

| workshop state | systems | budget needed |
|---|---|---|
| module 6 today | 8 | 8,500 |
| module 8 today | 9 | 8,700 |
| module 6 + HighByte, one exact join | 9 | 9,700 |
| module 8 + HighByte, one exact join | 10 | 9,900 |
| module 8 + HighByte, three exact joins | 10 | 10,900 |

**Decision.** `orchestrator._ROUTE_SEED_SERIALIZED_BUDGET` is 10,000 characters
(was 8,000). It stays the one home of the number: every size check, the owners
block's room and the tests read that name. The code levers that recover room
without a raise (one fence policy line per payload, omitting empty rule fields)
total about 1,150 characters, which covers today's modules but not HighByte, so
they are not a substitute. 10,000 covers HighByte with up to one exact join; three
exact joins would need about 11,000, which is not measurable offline (HighByte's
real onboarding output is unknown) and is left until a live onboarding shows it.

**Cost.** A seed that fit under 8,000 is byte-identical for every question
without an owned identifier (with one, see the owners room below). A seed that
was over budget grows
by up to about 2,000 characters (about 500 tokens) per orchestrator turn; every
8+-system workshop seed was over budget.

**Room for the IDENTIFIER OWNERS block.** The owners block (ADR-0087
amendments of 2026-10-06/07) rides after the seed in whatever room the budget
leaves. Tier-2 trimming stops within one row of the budget, so that room was 8
to 246 characters in every modeled workshop state (77 on the seed captured from
a deployed workshop), while the block for a run-number question is about 930 characters whole
and, in its smallest form, the header plus one compact line naming every
candidate owner system (see "Owner order is total" below). It
was never delivered, and raising the budget alone does not change that: tier 2
refills any budget. So `build_route_seed` now asks the fit to keep room for it
(`orchestrator._fit_route_seed`). The room reserved counts the translations
block and the wildcard note, which the owners block's room is already charged
with. When the plain fit leaves less than the whole block, the fit is
continued to the budget minus the whole block, and kept only if that costs no
more prose than the plain fit (tier-2 detail and lossless sharing pay for it).
Otherwise it is continued to the budget minus the smallest form, which may
shrink prose. Neither may cost a tier-0/1 rule (R3) or a system; when both
would, the plain fit stands and the block keeps what room is left, as before.
The fit reserves room for the WHOLE identifier
tail — the deterministic translations, the owners block and the wildcard note —
not only the owners block, so a question that needs a pattern translation but
has no exact owner also keeps room for its translated value under pressure
(`orchestrator._tail_room_reserves`, at most two reserve values so the fit never
exceeds three full fits). Only questions carrying an identifier (an owner, a
translation, or a wildcard-bearing graph) take this path; every other seed is
fitted exactly as before. Only the log records of the fit(s) that produced
the kept seed are emitted.

**The hard budget is enforced on the final composed return, and the tail drops
in a correctness-first priority.** The
fit above reserves room for the identifier tail, but when the tier-0/1 floor
makes every reserve unaffordable the plain fit stands and leaves only what room
is left. The optional post-JSON tail — IDENTIFIER TRANSLATIONS, the IDENTIFIER
OWNERS block and the one wildcard note — used to be appended to that fit
unchanged, with no size check on the result, so a rule-heavy graph with a
wildcard rule could return a seed over the budget while the owners block
vanished. Making the final length safe by dropping the WHOLE
translations block, or the wildcard note, into the accidental remainder would be
its own correctness loss: a dropped deterministic translation sends the
original identifier to the target — the exact failure `identifier_translation`
was built to prevent — and the wildcard note is the instruction not to invent
`{*}` and which record to select, not generic repetition. The tail is now
composed within the room the fit leaves (`orchestrator._compose_identifier_tail`)
and dropped in an explicit priority order when that room is short, most valuable
kept first: (1) the translations (the exact deterministic translated value) are
the correctness floor (`orchestrator._fit_translations`): the whole block; else
the header plus as many leading full lines as fit; else, as the last step before
dropping, a COMPACT form (`identifier_translation.render_compact`: source value
and field, translated value, target system/field, and a short candidate-set
instruction — never fill `{*}`, get every match, use the one in force at the
event date, else the current one — with no method or confidence prose), line by
line. If not even one compact line fits, step (b) runs
(`orchestrator._yield_rules_for_translation`). PROTECTED — never
removed by this step: every tier-0 rule; the IdentityRule(s) that produced the
translations (their ids are carried on the structured items,
`orchestrator._translation_items` → `rule_ids`); and every rule the question
needs (`orchestrator._question_relevant`): its fields or concept share a
question term (`_question_terms`), or the question names one of its endpoint
systems as a whole token (`_names_system`: the full id, case- and
hyphen/underscore-insensitive, so the generic word "system" never matches
`system-c`). Every other tier-1 rule is eligible, INCLUDING unrelated tier-1
summaries on the translation's own systems; eligible rules on systems the
translation does not touch are removed first, then lowest priority first
(`_rule_sort_key`), only until the compact first translation fits. Tier-2 rules
are not in the pool (the fit already trimmed them first). The step re-encodes the
already-fitted payload — no extra full fit — and logs
`ROUTE_SEED_TRANSLATION_YIELD`. If the eligible rules cannot free enough room,
NOTHING is removed and the translation is dropped instead, logged as
`ROUTE_SEED_TRANSLATION_DROPPED`. What the code guarantees: the first matched
translation is delivered whenever its compact line fits in the room left after
the fit plus what the eligible rules can free; otherwise it is dropped and every
remaining rule is kept. It is therefore dropped only when the remaining tier-1
rules are all producing or question-relevant (or tier-0). On a
two-system 29-exact-rule wall the compact line (274 characters) fits the 281 the
fit leaves; with 4 more characters per translation field, or 24-character system
ids, it does not, and step (b) frees room from that wall's own unrelated exact
rules. The compact line writes each endpoint as `system: field`, which the
route-seed trace and `parse_rendered` split exactly; for the full line, whose
endpoint is `system.field`, the trace splits at the longest known system id, and
an endpoint whose system the seed inventory omitted stays one unsplit label;
(2) the wildcard note, ranked ABOVE the advisory owners
whenever a wildcard rule applies (it is only present then), with its leading
`"\n"` separator counted in both the reserve and the composer when nothing
precedes it; (3) the owners block (advisory
routing) is trimmed to its smallest form, then omitted, last. Room is allocated
in that priority order and the blocks are emitted in reading order
(translations, owners, note). `build_route_seed` then checks the FINAL composed
string against the budget (`orchestrator._fit_and_compose_route_seed`) and falls
back to the emergency bounded seed if it is still over — so the single-budget
statement above is now true of every size check, including the composed tail,
not only of the fitted seed.

**Owner order is total, directional duplicates fold canonically, and the
smallest form names every candidate system past the line cap.** `find_owners` sorted only by confidence and token,
and the route-seed Cypher and `_rule_sort_key` had no stable tie-break after
confidence, so equal-ranked exact rules inherited database collection order;
under budget trimming that order decided which named owners survived. The order
is now total — tier, then (Python side) overlap with the question's own words,
then confidence, then the stable rule id and endpoint ids — in BOTH the Cypher
`ORDER BY` (IdentityRule, JoinPath, ValueMapping) and the Python sorts
(`identifier_translation._owner_sort_key`, `orchestrator._rule_sort_key`), with
no plant/site heuristic added (domain-neutrality ratchet). In the Cypher the id
and endpoint ids are ONE list-valued sort key, in `_rule_identity_key`'s field
order: Neptune Analytics rejects an `ORDER BY` with more than five sort keys
("ORDER BY with more than 5 sort keys is not supported in this release") and
orders lists element by element, so the list key gives the same order within
the limit. Listed as separate keys (seven for JoinPath and IdentityRule), the
query was rejected on a deployed graph, `neptune_query` degraded the rejection
to `[]`, and every seed lost its join paths, value mappings and identity rules;
the unit lane, which runs the Cypher against fakes, could not see it.
`tests/unit/test_cypher_sort_key_limit.py` now bounds the sort keys of every
`ORDER BY` in the package's string literals, and the lenient `neptune_query`
logs the engine's masked message so a degraded query names its cause. The two directional
rules of one exact join (each side's resolve writes one, source and target
swapped) are now canonicalized to the sorted unordered endpoint pair BEFORE
de-duplication, and the best rule for that pair is chosen by a total key — tier,
then confidence, then the canonical endpoint ids, then the rule's own source
endpoint as a final direction key (so equal-confidence directional
copies also render one direction) — rather than by whichever
direction the graph returned first (`find_owners`/`_owner_rank`); the winner's
own direction is rendered, so a single rule's line is unchanged. The per-line
owners list is still capped at `TRANSLATION_CAP`, but the smallest owners form
is no longer "first owner plus N omitted" — which implied a preference and could
drop the question's own-plant MES behind unrelated equal-ranked owners — it is
ONE compact line naming every candidate owner system (sorted). That set is built
from EVERY shape match before the per-line cap and carried on the returned list
(`find_owners`/`_OwnerList.candidate_systems`), so a candidate is never dropped
behind the eight named lines either (`identifier_translation.render_owners` /
`_compact_owner_line`).

**An emptied shared section leaves no reference.** When the shrink pass cuts a
shared connection-type section (`shared_guidance[<type>]`) to nothing, each
system that delivered its base through it used to keep its `see shared
guidance: <type>` line, pointing the orchestrator at an empty section (at the
8,000 budget, in every modeled HighByte state with the work-order hop). The
references are now dropped with the section (`orchestrator._drop_emptied_shared_references`) and
stay dropped when the system's own fields are re-fitted; the cut stays reported
on each of those systems. This frees about 30 characters per referencing system
(six on the workshop at module 8), which the shrink pass then leaves to the
systems' own guidance.

**Effect (same offline replay, after the three changes).** With the module 6
note and the work-order hop on a fresh account's graph:

| workshop state | registration guidance | shared PostgreSQL default | owners block (run-number question) |
|---|---|---|---|
| module 6 / module 8 today | whole (was cut, then dropped at module 8) | whole (was dropped) | whole (was never delivered) |
| + HighByte, no exact join | whole | whole | smallest form |
| module 8 + HighByte, one exact join | whole | whole; cut to nothing on a run-number question | smallest form |
| module 6 + HighByte, one exact join | whole; on a run-number question the plant MES (570 → 376) and SiteWise (546 → 482) are cut to make room | whole; cut to 417 on a run-number question | smallest form |
| + HighByte, three exact joins | module 6 whole, module 8 cut; on a run-number question cut or dropped (all whole needs 10,600 to 10,900) | cut to nothing | smallest form |

No system points at an empty shared section in any state. On a run-number
question the smallest owners form costs prose only where the plain seed had
no tier-2 detail left to give; making a generic default yield before a
registration base (the one lever not taken here) would move that cost onto
the default. Verified by `tests/unit/test_route_seed_workshop_budget.py`
(workshop-shaped fixture `_route_seed_workshop_fixture.py`) and
`test_route_seed_routing_guidance.py` (the 2026-10-04 lever pins, kept at the
8,000 they were measured under).

**Asset coverage rides on top of the fit.** A system's `asset_coverage` line
(the per-site asset counts the role pass records on the System node,
[graph shape contract](../graph-shape-contract.md)) is not part of this
budget. `orchestrator._fit_and_compose_route_seed` takes it out before the fit
runs and puts it back after the identifier tail is composed
(`orchestrator._with_asset_coverage`), capped at 1,024 characters across all
systems. So the plain fit, every reserve, a step (b) yield and the tail's room
are all decided as if no system had coverage: with its coverage keys removed,
the seed is byte-identical to one built without coverage. The final check is
the budget plus the coverage actually added. Verified by
`tests/unit/test_route_seed_coverage_with_reserves.py` (every workshop state,
with and without a run-number question, and the yield path).

## Amendment 2026-10-17: route-seed budget 16,000

**Context.** The 2026-10-08 amendment set 10,000 and left a raise for HighByte
with three or more exact joins "until a live onboarding shows it". A deployed
workshop at module 8b showed it. The exact-agreement pass had written
12 exact joins (24 IdentityRule and JoinPath summaries): each of the four plant
MES systems' run number and work order number to the ERP, three of those MES's
consumed lot number to the ERP material lot, and the PLM item. With
HighByte, the module 6 sensor note and a REST CMMS connected in module 8b, the
seed was over 10,000 on every question. Prose yields before evidence-backed
rules (R3), so all 24 rules arrived and every system's registration text was
cut to nothing: the plant MES's guidance (the only seed text that says how its
machine names read), both sensor systems' guidance (which one serves trends,
which the current value) and each plant system's capabilities line. The
module 2 page's claim that the assistant reads each system's guidance before
routing was false on that build. The module 8b question then had no way to tie
the MES's machine name to the maintenance system's asset, and the answer
reported the gap in one of six asks.

**Evidence.** `tests/unit/_route_seed_live_fixture.py` rebuilds that account's
shape from the workshop's own manifests and the account's own 12 joins (module 8
with HighByte and the corporate CMMS, and module 8b with that REST CMMS in place
of the plant CMMS). `tests/unit/fixtures/workshop_chat_prompts.json` holds every
chat prompt on the workshop pages, captured from the workshop repository.
Lowest budget, in 100-character steps, from which every larger budget passes,
over every one of those prompts (the reserve selection is not monotonic, so a
few lower budgets also pass on their own: 12,600 at module 8 and 12,400 at
module 8b for the first row):

| what arrives whole | module 8 | module 8b |
|---|---|---|
| every tier-1 rule, registration base, capabilities line and the module 6 sensor note | 13,100 | 13,500 |
| the above, the shared PostgreSQL default and the REST CMMS default at its field cap | 13,100 | 14,000 |
| the same with the 13th lot join (the one the account lacked) | | 14,500 |
| the whole IDENTIFIER OWNERS block on every prompt that carries one | 13,500 | 14,400 (14,900 with the 13th lot join) |

The hardest prompt in the first three rows is module 2's lot follow-up, whose
identifier tail is the longest. The last row covers the 6 distinct page prompts
that carry an identifier with an owners block (module 2's lot follow-up, the
module 3, 4, 5 and 8b maintenance question, module 5's root cause, module 6's
containment list, and module 7b's recall picture and quality-escape report). Below its threshold the block falls back to its
smallest form, one line naming the candidate systems; at 14,000 every one of
them gets only that form at module 8b.

The REST CMMS was registered with its connection type's default guidance (1,412
characters), which is longer than the per-field guidance cap
(`guidance_scope.DELIVERY_FIELD_CAP`, 1,024). Once a seed is over budget every
guidance field is cut to that cap first, so this default arrives cut to it; the
budget passes cut nothing further at 14,000 and above. Raising the field cap is
a separate decision this amendment does not take.

Decision-point replays of the logged module 8b turn (Bedrock, same model and
system prompt, eight runs each, only the seed changed between 10,000 and
16,000): at the first maintenance call, the 16,000 seed's call named the
machine's designation `CMX-441` in 6 of 8 runs (the other 2 checked the graph
for an equipment mapping first); the 10,000 seed never named it (0 of 8). After
the logged miss, the 16,000 seed re-delegated with `1001-CMX-441` in 8 of 8
runs, the 10,000 seed in 1 of 8.

**Decision.** `orchestrator._ROUTE_SEED_SERIALIZED_BUDGET` is 16,000 (was
10,000): every row of the table in every state with about 1,100 characters to
spare, and the seed the replays above measured. It is not sized for notes an
attendee or the learning loop adds later; those are not measured here. A
seed that fit 10,000 is byte-identical when its rows arrive in the traversal's
own order, as every production read does (see shared concepts below). A seed
that was over budget grows by up to 6,000 characters (about 1,500 tokens) per
orchestrator turn, against a standing orchestrator prompt of about 62,000.
Tests that exercise the trimming passes rather than the number run under the
old 10,000 (`tests/unit/conftest.py`, `route_seed_pressure_budget`), as the
lever tests already run under 8,000.

**Considered and rejected: tier-1 rules yield to registration text.** A
post-fit step that put registration text back by dropping tier-1 rules the
question does not need also recovered the 8b turn in the replay (8 of 8 after
the miss). It was
rejected because it overturns R3 (evidence-backed rules outrank prose) and
dropped six to eight exact-join summaries on every workshop prompt, often ones
the attendee's next follow-up needs (the lot-to-material-lot joins behind
module 2's follow-ups). The budget raise keeps every rule.

**Found with the raise: shared concepts are ordered in the builder.** With the
pressure gone, shared-concept rows survive the fit, and their order came from
the order rows arrived in. The traversal orders them by concept then peer, so a
production seed was already stable; the builder now applies the same order
(`orchestrator._merge_route_seed_rules`), so the seed never depends on row
order (`tests/unit/test_route_seed_owner_order.py`,
`tests/unit/test_route_seed_registration_text.py`). Rows that arrive in the
traversal's order are unchanged by it.

**Open.** The MES machine name reaches the maintenance tag through registration
prose. A confirmed value mapping in the graph would make that link
deterministic; nothing in the MES data lets onboarding propose one today.

## Amendment 2026-10-18: the seed carries the stored value mappings for identifiers the question names

**Context.** The route seed hands the orchestrator rule-grain ValueMapping
summaries and never their values. An identifier whose only link to another
system is a row-level mapping (one machine's id in the plant MES, its tag in
the plant CMMS, its asset name in SiteWise) therefore reached the
orchestrator with no value to translate to. Its two other ways to the value
both depend on the model: Step 3 tells it to look the identifier up through
`explore_graph` before delegating, and the IDENTIFIER-MISS recovery (ADR-0048)
starts only when a system agent returns the fenced marker, so an empty answer
to the untranslated value starts nothing, and a recovery costs a wasted first
delegation. On the workshop the gap was covered by registration prose that
states the machine's identity in words (the registration-text audit of
2026-10-17); this amendment is the graph-backed path that lets that prose go.

The block hands stored values over as the translation, so it carries only
what the graph can stand behind, as data, and when in doubt it says nothing
and the existing recovery path runs.

**Decision.**
- Which rows. Only two kinds of ValueMapping may reach the block
  (`identifier_translation.value_mapping_trusted`): a deterministic method
  (`value_format_match`, `equipment_name_match`) under the deterministic
  equipment writer's ids (`vm-equip-`), or a steward-confirmed method
  (`user_confirmed`, `crosswalk_confirmed`) under the steward accept's ids
  (`subj-`, and `vm-learned-` for its replay) that a person accepted one row
  at a time. The method alone is not enough: the resolve step's model picks
  the method of its rows (`vm-xsys-`), so it can claim `value_format_match`.
  A model-proposed method (`semantic_similarity`, `hierarchy_position`) never
  appears. The resolve writer (`onboard._dp_write_cross_vm`) now stores a
  model's claim of `user_confirmed` as `semantic_similarity`; no other
  model-output writer accepted it.
- One row at a time. Every accept writes the same `user_confirmed` node under
  the same `subj-` id, including the ones nobody reviews row by row:
  accept-all, an explicit batch, the high-confidence sweep (its route, the
  periodic pass and the post-ask hook) and the opt-in fresh-log auto-accept.
  So a steward-confirmed row counts only when its node carries
  `confirmation_mode = 'single_row'` and a non-empty `accepted_by`
  (`identifier_translation.single_row_confirmed`). Only the route a person
  uses to accept ONE proposal (`server._accept_proposed_mapping_sync`, with a
  signed-in actor) and a steward's direct correction (`user-define`, through
  `self_learning.record_user_correction`) write that stamp; the ledger row
  keeps it, so a replay (`onboard._dp_replay_learned_vm`) copies it. Every
  other accept writes both properties empty, also over an earlier stamp on
  the same node. Rows accepted before the stamp existed have none and are
  left out (no migration).
- The read. `build_route_seed(question=...)` makes ONE more read when the
  question holds an identifier token, and one more for the hop. Both open with
  the trust predicate (method, id prefix and, for a steward-confirmed method,
  the one-row stamp, all parameterized), then leave out a mapping naming a
  system whose removal has started, then match the stored `from_value` or
  `to_value`, lower-cased, equal to a token or ending with it right after a
  separator (`-`, `_`, `/`, `.`, space). `LIMIT 32`, ordered like the
  rule-grain read; a read that returns 32 rows omits the block, because a
  conflicting row may be past the cap.
- Which tokens. An identifier has a letter and a digit: dates, times, plain
  numbers, ordinals, clock times, durations and hyphenated words are not
  tokens, and give no read. A token with two or more segments (`CMX-441`)
  matches exactly or by WHOLE TRAILING SEGMENTS (`1001-CMX-441`, never
  `ACMX-441`); a compact token with no separator (`M1`, `LINE3`) matches
  exactly only. A token holding `#` or a letter or digit outside ASCII
  (`ASSET#1`, `MÜ-1`) matches exactly only too: the question is read in NFC,
  the read looks for both its composed and decomposed spellings, each lowered
  the Unicode way and the ASCII way (Memgraph 3.12's `toLower` lowers ASCII
  letters only: `toLower('MÜ-1')` is `'mÜ-1'`), and also every other spelling
  whose lower case is the token's, character by character (`ẞ1` for `ß1`,
  whose upper case is `SS1`; the Kelvin sign for `k`), at most 32 per form,
  else the token's own spellings only and a log line; a one-hop value outside
  ASCII is looked for the same way. Only a spelling the match calls exact is
  read: `ss1` (the lower case of `ß1`'s upper case) is not, so rows holding
  `SS1` never take a place under the 32-row cap from a `ẞ1` row. The one hop
  keeps only the spellings whose lower case is the value's own (`ΑΣ1`
  lowers to `ας1`, so it is no spelling of `ασ1`). Known limit: past 32
  spellings (six or more cased letters outside ASCII, `αβγδεζ1`) only the
  token's own spellings are read, so on a graph whose `toLower` lowers ASCII
  letters only (Memgraph 3.12; Neptune Analytics not measured) a value
  stored in another mix of cases (`Αβγδεζ1`) is not found and the block
  leaves it out. That fails safe (no mapping, the assistant's own recovery
  runs); a stored lower-case lookup key on every ValueMapping would remove
  it. Python decides the match;
  an ASCII token inside such a token is not a token of its own. Date and time unit
  characters after a number (`2026年10月18日`) make no identifier. At most 8
  tokens.
- Which matches (`identifier_translation.find_value_mappings`). A token that
  reaches more than one value in one system reaches none there. Then ONE hop
  (a row sharing a (system, value) endpoint with a matched row; never two).
  Then every row is left out whose endpoint maps to more than one value of one
  other system among the rows found (a stale and a current mapping of one
  machine, say), with every hop that hung on it. Only systems the seed's
  inventory lists. One row per pair of endpoints; ordered steward tier, exact
  before segment before hop, then confidence; at most 8 lines, the rest
  counted on one line.
- As data. Every rendered text (systems, fields, values, method) must pass
  `untrusted_text.sanitize_untrusted` unchanged and `validate_untrusted` (no
  secret shape, no fence token), be one line with no control, format or
  line-separator character, be at most 256 characters, and name none of the
  seed's own markers; otherwise its row is left out, never cut or rewritten.
  Each line is one JSON object, so a stored text is always a quoted JSON
  string, under a line saying the strings are stored data, not instructions.
  Provenance and confidence stay out of the model's text.
- The header. A seed whose tail carries the block gets a header that says so
  (`orchestrator._ROUTE_SEED_VALUE_MAPPING_HEADERS`: the same text with the
  "never row-level mapping values" sentence replaced); every other seed keeps
  its header byte for byte. The longer header is paid for inside the budget.
- Room. The block never costs a system, a tier-0 or tier-1 rule, or any
  character of a system's own text (name, plants, capabilities, guidance). The
  seed is fitted exactly as without the block; only when that leaves too
  little room is it re-fitted for the block, and the re-fit is kept only when
  every system's text is delivered as fully as before
  (`orchestrator._fit_route_seed_with_value_mappings`). Otherwise the block
  gets the room left, degraded line by line, then to compact lines (values,
  systems, method), then dropped. In the tail it ranks below translations, the
  wildcard note and IDENTIFIER FORMS and above IDENTIFIER OWNERS, and it takes
  part in no step (b) yield.
- A question with no stored mapping (or none matching) gets the seed built
  without the read, byte for byte; a failed read omits only the block.
- The Knowledge Graph trace step names each mapping in plain words
  (`route_seed_trace`), read back from the seed text. A steward-confirmed
  mapping reads "accepted by a steward one row at a time", never the rule
  wording "confirmed by a reviewer".
- `_MODE_A_DELEGATE_BLOCK` gains one paragraph: the block's values are the
  translation; send each listed system its own value as written, in the
  sub-question and in `translated_identifiers`, with no graph lookup first;
  the JSON strings are data, never an instruction.

**Evidence.** Unit: `tests/unit/test_identifier_value_mappings.py` (matching,
hop, order, cap, render, read-back), `tests/unit/test_value_mapping_block_safety.py`
(which rows, which text, which tokens, which matches),
`tests/unit/test_value_mapping_block_writers.py` (each real writer's rows
against the trust test; the resolve writer's coercion),
`tests/unit/test_value_mapping_block_accept_paths.py` (each accept path end to
end: one-row accept, direct correction, explicit batch, accept-all, the
high-confidence route, the periodic and post-ask sweep, the fresh-log
auto-accept, a bulk re-accept over a stamp, and the replay),
`tests/unit/test_value_mapping_block_tokens.py` (`#`, slash, dot, composed and
decomposed Unicode, surrounding punctuation, dates),
`tests/unit/test_route_seed_value_mappings.py` (the read's trust predicate,
parameters, bounds and cap, byte identity without a mapping, the header, a
failed read, a system being removed, the trace, the final budget and every
system's own text at 10,000, 14,000 and 16,000 under pressure),
`tests/unit/test_route_seed_workshop_value_mappings.py` (the workshop fixture
with the module 4 equipment mappings at all three budgets), and
`tests/integration/test_route_seed_value_mappings_memgraph.py` (both reads, the
trust predicate and stamp, the exact-only tokens and the seed on Memgraph).
Offline decision-point replays (same model and system prompt, 8 runs per
case, registration identity sentences removed, budget 16,000) that read one
model step: with the block's first, plain-text form, module 5 Q1's first CMMS
call named the CMMS tag in 7 of 8 runs (8 of 8 after the MES step); without it
none did (3 of 8 sent the MES machine id unchanged, 5 asked the MES first).
With the JSON form, 7 of 8 with the block and 1 of 8 without (8 of 8 and 0 of
8 after the MES step). One step cannot judge module 6: in 6 of 8 JSON-form
runs with the block the model called a tool that does not exist (two listed
tool names mixed into one), which the agent loop answers with an unknown-tool
error before the model takes another step; the same made-up name also came
up with no block (4 of 8 runs on one module 6 seed, 1 of 8 on the byte-identical
seed of the other arm). Every valid MES call with the block named the MES
machine id; without it none did. A replay that answers each call and reads
up to three steps is built and has not run yet.
Measured offline on the workshop fixture and a live-shaped module 6 account
(the joins a deployed workshop wrote on 2026-10-17): no case changes any system's own text; the
block displaces only tier-2 summaries and, at 10,000, the IDENTIFIER OWNERS
block; at 10,000 the live-shaped module 6 question gets no block (the systems'
text leaves no room), at 14,000 and 16,000 every case gets the whole block.

**Open.** The trust predicate bounds which rows the block can use, not the
work: the reads have no index on these properties, so each visits every
ValueMapping node. Measured on a deployed workshop: 1 ValueMapping
after module 7 of a fresh install (2026-10-12), 78 on a long-lived install
(2026-10-11), where the 20 rows a module 7 query listed were all
`hierarchy_position` or `semantic_similarity` (none the block may carry).
Indexed lookup keys (normalized exact and suffix values, and the trust stamp)
are the follow-up before this runs on accounts with thousands of mappings.
The reads ran on Memgraph only, not yet on Neptune Analytics. Mode B builds
no route seed and is unchanged.
