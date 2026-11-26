<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Configurable layers ship as code defaults; admin-editability is a deferred seam

---
Status: accepted
---

The metric layer, concept↔system-type affinity, coverage thresholds, and similar tunable
layers ship with **code defaults** for v1. Making them editable from the admin pages
(`admin_routes.py` / `admin.html`) is designed-for but **not built now** — each such layer
is kept behind a clear interface so an admin-backed store can replace the default source
later without touching consumers.

## Context

A recurring instinct in design is to make everything runtime-configurable up front. That
expands product surface (persistence, validation, auth, UX) and competes with the core
digital-thread narrative. Deferring it as a seam keeps the abstraction benefit without the
build cost.

## Consequences

- Consumers read these layers through an interface, not a hardcoded literal, so the source
  can swap from default → admin store later.
- "Configurable from admin" is an explicit non-goal for v1, recorded so it isn't mistaken
  for missing work.
