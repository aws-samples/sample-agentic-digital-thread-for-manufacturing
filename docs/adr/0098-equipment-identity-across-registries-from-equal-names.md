<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0098: Equipment identity across registries from equal machine names

## Status

**Proposed: 2026-10-18.** Verified offline and on a local PostgreSQL 16 server
against the seeded workshop data. Not yet deployed to an AWS account.

## Context

The provision post-pass writes equipment ValueMappings from an equipment
registry (a system with a Field on the `equipment-id` Concept) to a historian's
telemetry Assets (`_persist_equipment_value_mappings`). Its evidence lives in
the registry row's tag: a designation token such as `XY-123` shared with the
Asset name. A registry whose tags carry no designation (a line-position id
such as `L2-M1`) never matches anything, and nothing paired two registries
with each other.

So two systems that both keep an equipment registry had no graph fact saying
which of their rows is the same machine, even when both rows carry the same
machine name. Join discovery does not produce one either: its overlap floor is
five shared values, and the identity-affinity exact pass needs one dominant
value shape across a column, which free-text names do not have. The
orchestrator was left to whatever text a registration supplied.

## Decision

1. **A second evidence kind in the same post-pass.** After the per-registry
   telemetry pass, `_generate_equipment_value_mappings` pairs every two
   registries of different systems (`_cross_registry_vms_or_none`). A row of
   one and a row of the other give one ValueMapping between their tags when:
   - their names are equal after casefolding and collapsing whitespace;
   - the name is on exactly one tag in each registry, and that tag carries no
     other name;
   - the name carries at least one equipment designation
     (`_equipment_designations`, the token rule the telemetry matcher uses);
   - both rows are known to be at the same site (decision 7).
2. **What is written.** `method='equipment_name_match'`, `confidence=0.95`,
   `concept='equipment'`, `to_element_id=""`, `to_path=""` (no Asset, so no
   `MAPS_TO_ASSET` edge), and a computed `provenance` that names the two
   name columns, where each side's site came from and the row counts
   compared, never a value. The lower system
   id is the `from` side, so the content-derived id (`_equipment_vm_id`) is the
   same whichever system provisioned last. The existing writer MERGEs it,
   de-duplicates it and retires older generated rows with the same identity.
   Only these mappings carry `provenance`: an empty string on the others would
   still be NOT NULL to the route seed's ordering.
3. **No model call.** The match is deterministic. The LLM vocabulary bridge
   of the telemetry pass is unchanged and never sees these rows.
4. **The name column is found, not mapped.** The registry is found by its
   tag Field's Concept; the name column comes from the table's Field list by
   name (`_registry_companion_fields`), so only the tag column has to be on
   `equipment-id`. A registry whose only name candidate is its tag takes no
   part.
5. **Written at provision, like the other equipment mappings.** The human
   step is upstream: a registry only exists once its tag column is on
   `equipment-id`, which a person confirms at the mapping review.
   The tag must be identifier shaped (`_registry_tag_eligible`): a field
   whose name says it is a date, time, name, description, maker, model or
   serial is never a registry's tag, for the telemetry pass and this one
   alike. The mapper often puts such columns on `equipment-id` beside the
   real tag (measured 2026-10-03: six `assets` columns). Before this rule,
   with the tag column moved off `equipment-id` at the review, a serial
   number column won the pick, and the name match would have tied a
   machine to its serial number before anyone corrected the tag. Now a
   system whose only `equipment-id` fields describe the asset is no
   registry, and nothing is written for it until the tag is back.
   Among the eligible fields the pick settles the table first: one whose
   candidate field is its identity key (`IDENTIFIED_BY`), then the
   equipment reference table (`_registry_reference_shaped`: a name column of its own,
   one row per tag value, proven over the whole table). Then the field:
   designation density, the identity key, the name rank, and last the field
   and table names. A tag column is also a column of the fact tables that
   reference it (runs, readings, work orders), with the same name and
   values; before this, the pick kept whichever the graph returned first,
   and a fact table has no name column, so the system dropped out of this
   match. Reference shape is read from rows, so it never overrides a key the
   schema states. Density is read per column, so it never moves the tag
   from the reference table to a fact table's column that holds other equipment's
   ids carrying a designation (a gauge), where the reference table's own tags carry
   none: the frozen MES map put `quality_checks.measurement_equipment` on
   `equipment-id` in all five replays.
6. **The provision result lists them.** `_dp_result` reads the system's
   ValueMappings before the post-pass runs; when the post-pass wrote any, the
   result's `value_mappings` list is read again, so the onboarding page shows
   them.
7. **Same site, or no match.** Plants built from the same templates name
   their machines the same way, and a designation can repeat across plants,
   so equal names alone do not say two rows are one machine. Each row gets a
   site (`_registry_row_site`):
   - a registry whose System serves one plant: that plant
     (`catalog.normalize_plant`, then `metric_multisite.site_of`, compared
     after `_equip_norm`). Its own site column is not read, since it may hold
     a site code rather than the plant;
   - a registry whose System serves no one plant (no plant, or an All Sites
     wildcard, such as a corporate CMMS): the row's own site column value
     (`_registry_companion_fields`), through the same rule as a System's
     plant (`_registry_site_key`), so a row that says `All Sites`, `All`,
     `All Plants`, `Custom` (any case or separator) or nothing is at no
     known site; unknown also when the table has no site column or the
     tag's rows disagree.

   Two rows match only when both sites are known and equal. So two plants'
   registries never match; a plant registry matches an All Sites registry
   only on rows whose site column names that plant; an All Sites registry
   without a site column matches nothing; and a name repeated inside a
   multi-site registry is not one-to-one, so it matches nothing either.
8. **What a complete pass no longer produces is retired.** The writer
   MERGEs this pass's mappings and retires an older generated row with the
   same identity; a generated row whose identity the pass does not produce
   at all (the tag pick moved to a field with other values, a name went
   away) used to stay, and the route seed reads generated rows as fact.
   After the writes, `_sweep_stale_equipment_vms` deletes the generated
   (`vm-equip-`) rows the pass did not write, in the scope it read in full
   (`_EquipmentPass`, from `_equipment_pass`), then reads back that none is
   left:
   - a telemetry row (`value_format_match`, `hierarchy_position`) whose
     `from_system` is a registry the pass discovered and generated in full;
   - a name match (`equipment_name_match`) whose two systems both are, and
     only when the name match itself did not fail.

   A whole discovery also proves which systems are no registry now. The
   `equipment-id` traversal reads every System in the graph, not just the
   one being provisioned, and every read in discovery (the traversal, a tag
   pick's samples and reference checks) is recorded, so when none of them was
   incomplete the registries it returned are all there are. A system it did
   not return (its last `equipment-id` field was corrected to another
   concept, only fields `_registry_tag_eligible` rejects are left, or its
   schema or concept edge is gone) cannot produce a generated row, so the
   sweep also deletes, in the same statement (`$regs`: every registry the
   discovery returned, read in full or not):
   - a telemetry row whose `from_system` is not among them;
   - a name match with either end not among them, whether or not the name
     match ran whole.

   A registry discovery returned but read only in part keeps every row
   whose systems were all returned. A whole discovery that returns no
   registry at all retires every generated deterministic equipment row; that
   holds only because a traversal that degrades to no rows is recorded (and
   one that raises fails the pass), so neither reads as "no registry". A
   Neptune reply with no `results` list is recorded the same way
   (`db._neptune_execute`). A traversal row that comes back without its
   system id is recorded too, so that discovery is not whole; a row with the
   system but without its schema, table or field makes that system unread:
   it counts as returned (in `$regs`) and is never read in full, so none of
   its rows goes until a later pass reads it whole. A system id, schema,
   table or field that is not a non-empty string without surrounding space
   counts as missing. A system whose rows disagree (two schemas or plants,
   two System nodes under one id; one field both with and without the
   identity key) is unread the same way: the pick never chooses between
   them by the order the graph returned them. An exact duplicate row is
   read once. A system whose System node names a connection type outside
   SQL (`NON_SQL_CONNECTION_TYPES`: SiteWise, MCP, REST, Athena) is no
   registry of the pass: `pg` cannot read it, so it is never read, and a
   whole discovery counts it as none. Its reads used to fail on every
   provision (the workshop's SiteWise historian maps `asset_hierarchy`
   fields to `equipment-id`), so no workshop discovery was ever whole and
   the sweep never ran. Two System nodes under one id with two connection
   types disagree and are unread. A tag pick whose sample or reference read was incomplete makes
   its system unread too: the pick that is left is a guess. A registry
   that was not read whole writes nothing this pass, not just retires
   nothing: its telemetry mappings are dropped, and so is every name match
   with a registry whose name read was incomplete (a read cut at its limit
   can hide a second machine with the same name, which would make a name
   look one to one when it is not). A traversal that was not read whole (a
   failed read, or a row naming no system, which may be a hidden candidate
   of any system) writes nothing at all that provision, for any system.
   The cost: a transient read failure on a system's only provision leaves
   its equipment mappings unwritten until it is provisioned again (module
   7's value-mapping check shows the gap, and the warning names the cause).

   A registry is in full when no read of its own passes was incomplete: a
   graph read `db.neptune_query` degraded to no rows, a `pg` result marked
   `unavailable` or `truncated`, or a failure the pass swallowed
   (`db.recording_incomplete_reads`). A pass with an incomplete read in
   discovery itself (the `equipment-id` traversal, a tag pick's sample or
   reference check), or any mapping not written or its duplicate not retired,
   sweeps nothing. A count is read first, so a pass over unchanged data
   reads once and deletes nothing. Steward and learned rows
   (`subj-`, `vm-learned-`, `guidance-` ids, `user_confirmed` and
   `crosswalk_confirmed` methods) are never in scope. LLM-bridged rows
   (`semantic_similarity`) are not swept either: the model can pick another
   candidate or none on unchanged data, so a sweep would flap them between
   provisions, and the bridge's failure is swallowed without making the
   pass incomplete; they keep the identity-only lifecycle.

## Consequences

- Two registries naming one machine the same way are one traversal apart in
  the graph, with the method and provenance saying why.
- Equal names without a designation (`Hydraulic Press 402`, `Rotary Filler
  Station A`) give no mapping, and neither do equal names at two sites
  (decision 7). Registries whose names are descriptive only stay out of it.
- A registry is read once more per provision when there are two or more
  registries (the rows of its tag and name columns).
- A system with more than one eligible field on `equipment-id` costs, per
  candidate, one Field-list read and one count query over at most 1,000 of
  its rows (the reference check), beside the density sample it already had.
- Every provision whose discovery was whole and whose every mapping was
  written whole costs one more graph read (the stale count), and a delete
  and a readback when something is stale.
- Two provisions running the pass at once: one pass's discovery can run
  while the other system's mapper is rewriting its fields, see that system
  as no registry and retire its generated rows. The next provision of that
  system writes them again. Not guarded.
- Out of scope: registries that are not SQL (a REST CMMS, a SiteWise or MCP
  source). Discovery leaves them out (decision 8); a REST registry reader
  would have to change that. An LLM-bridged row keeps the identity-only
  lifecycle (decision 8).
