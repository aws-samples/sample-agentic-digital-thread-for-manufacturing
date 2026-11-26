// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Neptune Graph — Sustainability & Safety Pack
// source: 'pack:sustainability'
// Load after core.cypher. Adds environmental/safety vocabulary beyond core.
// Note: 'energy-consumption' is in core (backed by SiteWise CNC.PowerConsumption).
// The concepts here are mostly vocabulary for future ESG/safety integrations.

// ============================================================
// ENVIRONMENTAL MONITORING
// ============================================================
MERGE (:Concept {id: 'environmental-reading', name: 'Environmental Reading', isa95_level: 2, source: 'pack:sustainability', description: 'Measurement of environmental conditions — air quality, noise, dust. Loosely backed by SiteWise FoodEnvironment / AutoEnvironment.'});
MERGE (:Concept {id: 'emissions', name: 'Emissions', isa95_level: 2, source: 'pack:sustainability', description: 'Pollutants or greenhouse gases released by a process or facility.'});
MERGE (:Concept {id: 'water-usage', name: 'Water Usage', isa95_level: 2, source: 'pack:sustainability', description: 'Process and utility water consumption.'});
MERGE (:Concept {id: 'waste-stream', name: 'Waste Stream', isa95_level: 3, source: 'pack:sustainability', description: 'Categorized waste output from a process — solid, liquid, hazardous.'});

// ============================================================
// SAFETY & COMPLIANCE
// ============================================================
MERGE (:Concept {id: 'incident', name: 'Incident', isa95_level: 3, source: 'pack:sustainability', description: 'Recorded safety or quality event requiring investigation.'});
MERGE (:Concept {id: 'permit', name: 'Permit', isa95_level: 3, source: 'pack:sustainability', description: 'Authorization required before performing hazardous work.'});
MERGE (:Concept {id: 'regulatory-standard', name: 'Regulatory Standard', isa95_level: 4, source: 'pack:sustainability', description: 'External compliance requirement governing product or process.'});
MERGE (:Concept {id: 'lockout-tagout', name: 'Lockout / Tagout (LOTO)', isa95_level: 3, source: 'pack:sustainability', description: 'Procedure for isolating energy sources during maintenance or service.'});
MERGE (:Concept {id: 'near-miss', name: 'Near Miss', isa95_level: 3, source: 'pack:sustainability', description: 'Unplanned event that did not result in injury but had potential to.'});
