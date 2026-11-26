// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Core app-chrome types (branding / version / demo gate / systems). */

/** GET /api/branding — agents/branding.py get_branding(). Public route. */
export interface Branding {
  company: string;
  tagline: string;
  plants?: unknown;
  systems?: unknown;
}

/** GET /api/version */
export interface VersionInfo {
  version?: string;
  [k: string]: unknown;
}

/** GET /api/demo/status — gates demo-only UI (fail-safe: hide on unknown). */
export interface DemoStatus {
  enabled?: boolean;
  /** Workshop mode; false means the workshop story routes answer 404. */
  workshop?: boolean;
  [k: string]: unknown;
}

/** GET /api/systems — connected-systems chips (shape refined in Wave 2). */
export interface ConnectedSystem {
  id?: string;
  name?: string;
  [k: string]: unknown;
}
