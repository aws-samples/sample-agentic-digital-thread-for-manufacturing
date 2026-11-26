// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Neptune Graph — Discrete Manufacturing Pack
// source: 'pack:discrete'
// Load after core.cypher. Adds discrete manufacturing concepts beyond the
// universal core. Backed by MES SPC data, PLM specifications, CMMS calibration.

// ============================================================
// QUALITY TOOLS & SPC
// ============================================================
MERGE (:Concept {id: 'spc-parameter', name: 'SPC Parameter', isa95_level: 3, source: 'pack:discrete', description: 'Statistical process control metric. MES Detroit cp_value, cpk_value.'});
MERGE (:Concept {id: 'subgroup-sample', name: 'SPC Subgroup Sample', isa95_level: 3, source: 'pack:discrete', description: 'Set of samples grouped for SPC analysis. MES Line3 quality_checks.subgroup_id, sample_number.'});
MERGE (:Concept {id: 'first-article-inspection', name: 'First Article Inspection', isa95_level: 3, source: 'pack:discrete', description: 'Inspection of the first piece of a production run. MES check_type=FIRST_ARTICLE.'});
MERGE (:Concept {id: 'inspection-method', name: 'Inspection Method', isa95_level: 3, source: 'pack:discrete', description: 'How a measurement is taken. PLM inspection_method: CMM, Caliper, Micrometer, UT Thickness, Rockwell Hardness, etc.'});
MERGE (:Concept {id: 'key-characteristic', name: 'Key Characteristic', isa95_level: 3, source: 'pack:discrete', description: 'Critical feature requiring tight control. PLM spec_class CRITICAL/MAJOR/MINOR.'});
MERGE (:Concept {id: 'calibration', name: 'Calibration', isa95_level: 3, source: 'pack:discrete', description: 'Verification and adjustment of measurement equipment. CMMS maintenance_type=calibration, PM work_type=CALIBRATION.'});
MERGE (:Concept {id: 'audit', name: 'Audit', isa95_level: 3, source: 'pack:discrete', description: 'Systematic review of processes or records for compliance.'});

// ============================================================
// MAINTENANCE TYPES
// ============================================================
MERGE (:Concept {id: 'preventive-maintenance', name: 'Preventive Maintenance', isa95_level: 3, source: 'pack:discrete', description: 'Scheduled maintenance to prevent failure. CMMS wo_type=preventive, PM01 in SAP-style.'});
MERGE (:Concept {id: 'corrective-maintenance', name: 'Corrective Maintenance', isa95_level: 3, source: 'pack:discrete', description: 'Maintenance after detecting an issue. CMMS wo_type=corrective, PM02 in SAP-style.'});
MERGE (:Concept {id: 'breakdown', name: 'Breakdown', isa95_level: 3, source: 'pack:discrete', description: 'Unplanned equipment failure event. CMMS Detroit notification_type=M1.'});

// ============================================================
// PRODUCTION PLANNING
// ============================================================
MERGE (:Concept {id: 'recipe', name: 'Recipe / Work Instruction', isa95_level: 3, source: 'pack:discrete', description: 'Defined process parameters and steps for a product. Backed by ERP bill_of_materials and PLM bom_items.'});
MERGE (:Concept {id: 'routing', name: 'Routing', isa95_level: 3, source: 'pack:discrete', description: 'Ordered sequence of operations to manufacture a part. ERP product_site_routing.'});
