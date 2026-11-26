// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Neptune Graph — Pharmaceutical Industry Pack
// source: 'pack:pharma'
// Load after core.cypher. Adds pharma/biotech vocabulary. None of these concepts
// have direct schema backing in the demo data — they are vocabulary the LLM
// orchestrator can reason about and map onto manufacturing flows in pharma
// deployments.

// ============================================================
// BATCH RECORDS & QUALITY EVENTS
// ============================================================
MERGE (:Concept {id: 'batch-record', name: 'Batch Record', isa95_level: 3, source: 'pack:pharma', description: 'Complete documented history of materials, equipment, and steps for a production batch.'});
MERGE (:Concept {id: 'deviation', name: 'Deviation', isa95_level: 3, source: 'pack:pharma', description: 'Departure from an approved procedure or specification during manufacturing.'});
MERGE (:Concept {id: 'capa', name: 'CAPA', isa95_level: 3, source: 'pack:pharma', description: 'Corrective and Preventive Action — structured response to a quality event.'});
MERGE (:Concept {id: 'change-control', name: 'Change Control', isa95_level: 4, source: 'pack:pharma', description: 'Formal process for evaluating and approving changes to validated processes.'});
MERGE (:Concept {id: 'out-of-specification', name: 'Out of Specification (OOS)', isa95_level: 3, source: 'pack:pharma', description: 'Test result that falls outside established acceptance criteria.'});

// ============================================================
// PRODUCT & SUBSTANCE
// ============================================================
MERGE (:Concept {id: 'drug-substance', name: 'Drug Substance', isa95_level: 4, source: 'pack:pharma', description: 'Active pharmaceutical ingredient (API) before formulation.'});
MERGE (:Concept {id: 'drug-product', name: 'Drug Product', isa95_level: 4, source: 'pack:pharma', description: 'Finished dosage form containing the drug substance.'});

// ============================================================
// TESTING & VALIDATION
// ============================================================
MERGE (:Concept {id: 'sterility-test', name: 'Sterility Test', isa95_level: 3, source: 'pack:pharma', description: 'Microbiological test confirming absence of viable microorganisms.'});
MERGE (:Concept {id: 'endotoxin-test', name: 'Endotoxin Test', isa95_level: 3, source: 'pack:pharma', description: 'LAL or rFC assay measuring bacterial endotoxin levels.'});
MERGE (:Concept {id: 'stability-study', name: 'Stability Study', isa95_level: 4, source: 'pack:pharma', description: 'Long-term testing program confirming product quality over its shelf life.'});
MERGE (:Concept {id: 'validation-protocol', name: 'Validation Protocol', isa95_level: 4, source: 'pack:pharma', description: 'Pre-approved document defining how a process or method will be validated.'});

// ============================================================
// ENVIRONMENT & COMPLIANCE
// ============================================================
MERGE (:Concept {id: 'environmental-monitoring', name: 'Environmental Monitoring', isa95_level: 2, source: 'pack:pharma', description: 'Routine sampling of air, surfaces, and personnel in controlled areas.'});
MERGE (:Concept {id: 'clean-room-class', name: 'Clean Room Class', isa95_level: 2, source: 'pack:pharma', description: 'ISO or EU GMP classification defining allowable particle counts.'});
MERGE (:Concept {id: 'gmp-compliance', name: 'GMP Compliance', isa95_level: 4, source: 'pack:pharma', description: 'Adherence to Good Manufacturing Practice regulations.'});
