// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Neptune Graph — Automotive Industry Pack
// source: 'pack:automotive'
// Load after core.cypher. Adds automotive-specific concepts beyond the
// universal core. Backed by SiteWise CNC/HeatTreat/SurfaceTreat/Assembly
// /QualityInspect models, MES Detroit Cp/Cpk records, and PLM specifications
// (BORE_DIA, OD_TOL, HRC, TENSILE, etc.).

// ============================================================
// MACHINING & PROCESS PARAMETERS
// ============================================================
MERGE (:Concept {id: 'cnc-machining', name: 'CNC Machining', isa95_level: 3, source: 'pack:automotive', description: 'Computer-controlled machining process. SiteWise CNC model, MES Detroit cell_id (CNC-MILL-04).'});
MERGE (:Concept {id: 'spindle-speed', name: 'Spindle Speed', isa95_level: 2, source: 'pack:automotive', description: 'Rotational speed of CNC spindle. SiteWise CNC.SpindleSpeed.'});
MERGE (:Concept {id: 'feed-rate', name: 'Feed Rate', isa95_level: 2, source: 'pack:automotive', description: 'Rate of cutting tool advance. SiteWise CNC.FeedRate.'});
MERGE (:Concept {id: 'coolant-flow', name: 'Coolant Flow', isa95_level: 2, source: 'pack:automotive', description: 'CNC coolant delivery rate. SiteWise CNC.CoolantFlowRate, CoolantTemp.'});
MERGE (:Concept {id: 'tool-life', name: 'Tool Life', isa95_level: 3, source: 'pack:automotive', description: 'Remaining useful life of a cutting tool. SiteWise CNC.ToolLifeRemaining, ToolChangeEvent.'});
MERGE (:Concept {id: 'tool-position', name: 'Tool Position', isa95_level: 2, source: 'pack:automotive', description: 'CNC axis position. SiteWise CNC.ToolPosition_X/Y/Z.'});
MERGE (:Concept {id: 'spindle-vibration', name: 'Spindle Vibration', isa95_level: 2, source: 'pack:automotive', description: 'Vibration on CNC spindle indicating wear. SiteWise CNC.Vibration_X/Y/Z, CMMS WO failure_code=VIB-BEARING.'});
MERGE (:Concept {id: 'tooling', name: 'Tooling', isa95_level: 3, source: 'pack:automotive', description: 'Fixtures, jigs, and tools used in manufacturing.'});
MERGE (:Concept {id: 'die-set', name: 'Die Set', isa95_level: 3, source: 'pack:automotive', description: 'Matched stamping dies used to form sheet metal parts.'});

// ============================================================
// HEAT & SURFACE TREATMENT
// ============================================================
MERGE (:Concept {id: 'heat-treatment', name: 'Heat Treatment', isa95_level: 3, source: 'pack:automotive', description: 'Thermal processing for hardness/microstructure. SiteWise HeatTreat with FurnaceTemp, QuenchTemp, SoakTime.'});
MERGE (:Concept {id: 'surface-treatment', name: 'Surface Treatment / Coating', isa95_level: 3, source: 'pack:automotive', description: 'Phosphate, e-coat, paint coating processes. SiteWise SurfaceTreat with BathConcentration, BathTemp, CoatingThickness.'});
MERGE (:Concept {id: 'coating-thickness', name: 'Coating Thickness', isa95_level: 2, source: 'pack:automotive', description: 'Thickness of applied coating. SiteWise SurfaceTreat.CoatingThickness.'});
MERGE (:Concept {id: 'paint-process', name: 'Paint Process', isa95_level: 3, source: 'pack:automotive', description: 'Multi-stage coating: e-coat, primer, base, clear.'});

// ============================================================
// ASSEMBLY
// ============================================================
MERGE (:Concept {id: 'torque-control', name: 'Torque Control', isa95_level: 3, source: 'pack:automotive', description: 'Torque-controlled fastener tightening. SiteWise Assembly with TorqueTarget, TorqueValue.'});
MERGE (:Concept {id: 'assembly-station', name: 'Assembly Station', isa95_level: 3, source: 'pack:automotive', description: 'Fixed position on assembly line for specific operations.'});
MERGE (:Concept {id: 'fastener-tracking', name: 'Fastener Tracking', isa95_level: 3, source: 'pack:automotive', description: 'Traceability linking fastener torque results to a vehicle.'});
MERGE (:Concept {id: 'welding', name: 'Welding', isa95_level: 3, source: 'pack:automotive', description: 'Joining process. CMMS Detroit Robotic-Welding-Cell, MES Detroit weld parameters.'});
MERGE (:Concept {id: 'weld-parameter', name: 'Weld Parameter', isa95_level: 3, source: 'pack:automotive', description: 'Weld settings (current, voltage, time, bead width, penetration depth).'});
MERGE (:Concept {id: 'stamping', name: 'Stamping / Press', isa95_level: 3, source: 'pack:automotive', description: 'Sheet metal forming. CMMS Stamping-Press-701, Hydraulic-Press-402.'});

// ============================================================
// INSPECTION & QUALITY
// ============================================================
MERGE (:Concept {id: 'cmm-inspection', name: 'CMM Inspection', isa95_level: 3, source: 'pack:automotive', description: 'Coordinate measuring machine inspection. SiteWise QualityInspect with DimensionX/Y/Z. PLM inspection_method=CMM/Bore Gauge/Runout/Roundness/Cylindricity/Flatness.'});
MERGE (:Concept {id: 'vision-inspection', name: 'Vision Inspection', isa95_level: 3, source: 'pack:automotive', description: 'Camera-based automated inspection. CMMS Cognex In-Sight assets.'});
MERGE (:Concept {id: 'dimensional-tolerance', name: 'Dimensional Tolerance', isa95_level: 3, source: 'pack:automotive', description: 'Acceptable deviation from nominal dimension. PLM OD_TOL, LEN_TOL, BORE_DIA, ROUNDNESS, CYLINDRIC, DECK_FLAT.'});
MERGE (:Concept {id: 'surface-finish', name: 'Surface Finish (Ra)', isa95_level: 3, source: 'pack:automotive', description: 'Roughness average measurement. PLM SURF_RA, SiteWise QualityInspect.SurfaceFinish_Ra.'});
MERGE (:Concept {id: 'hardness', name: 'Hardness', isa95_level: 3, source: 'pack:automotive', description: 'Material hardness measurement. PLM HRC (Rockwell C), HRB (Rockwell B), HB (Brinell).'});
MERGE (:Concept {id: 'tensile-strength', name: 'Tensile Strength', isa95_level: 3, source: 'pack:automotive', description: 'Maximum stress before fracture. PLM TENSILE, MES Detroit Tensile Strength characteristic.'});
MERGE (:Concept {id: 'wall-thickness', name: 'Wall Thickness', isa95_level: 3, source: 'pack:automotive', description: 'Thickness measurement, often via ultrasonic. PLM WALL_THK with UT Thickness method.'});
MERGE (:Concept {id: 'porosity-inspection', name: 'Porosity Inspection', isa95_level: 3, source: 'pack:automotive', description: 'X-ray detection of internal porosity. PLM POROSITY with X-Ray Inspection method.'});
MERGE (:Concept {id: 'spc-cp-cpk', name: 'Cp / Cpk Process Capability', isa95_level: 3, source: 'pack:automotive', description: 'Statistical process capability indices. MES Detroit quality_records.cp_value, cpk_value.'});

// ============================================================
// MATERIAL SPECIFICATIONS
// ============================================================
MERGE (:Concept {id: 'astm-material-spec', name: 'ASTM Material Specification', isa95_level: 4, source: 'pack:automotive', description: 'Standard material specification. PLM material_spec values: ASTM-A108-1045, A322-4140, A295-52100, B211-7075-T6, A48-CL35.'});
MERGE (:Concept {id: 'spec-class', name: 'Specification Class', isa95_level: 3, source: 'pack:automotive', description: 'Key characteristic classification. PLM specifications.spec_class: CRITICAL/MAJOR/MINOR.'});

// ============================================================
// VEHICLE IDENTITY (vocabulary — for future use)
// ============================================================
MERGE (:Concept {id: 'vin', name: 'Vehicle Identification Number', isa95_level: 3, source: 'pack:automotive', description: 'Globally unique 17-character vehicle identifier.'});
MERGE (:Concept {id: 'vehicle-configuration', name: 'Vehicle Configuration', isa95_level: 4, source: 'pack:automotive', description: 'Complete options/features defining a specific vehicle build.'});
MERGE (:Concept {id: 'option-code', name: 'Option Code', isa95_level: 4, source: 'pack:automotive', description: 'Short code for selectable feature or package on a vehicle.'});
MERGE (:Concept {id: 'build-sequence', name: 'Build Sequence', isa95_level: 3, source: 'pack:automotive', description: 'Ordered position of a vehicle in the production schedule.'});

// ============================================================
// QUALITY METHODOLOGY (vocabulary)
// ============================================================
MERGE (:Concept {id: 'ppap', name: 'PPAP', isa95_level: 4, source: 'pack:automotive', description: 'Production Part Approval Process — supplier qualification submission.'});
MERGE (:Concept {id: 'fmea', name: 'FMEA', isa95_level: 4, source: 'pack:automotive', description: 'Failure Mode and Effects Analysis — risk assessment for process/design.'});
MERGE (:Concept {id: 'control-plan', name: 'Control Plan', isa95_level: 3, source: 'pack:automotive', description: 'Document specifying controls for each process step.'});
MERGE (:Concept {id: 'poka-yoke', name: 'Poka-Yoke', isa95_level: 3, source: 'pack:automotive', description: 'Error-proofing device or method.'});
MERGE (:Concept {id: 'iatf-audit', name: 'IATF Audit', isa95_level: 4, source: 'pack:automotive', description: 'Third-party audit against IATF 16949 automotive QMS standard.'});
MERGE (:Concept {id: 'kanban', name: 'Kanban', isa95_level: 3, source: 'pack:automotive', description: 'Pull-based replenishment signal.'});
MERGE (:Concept {id: 'sequencing', name: 'Build Sequencing', isa95_level: 3, source: 'pack:automotive', description: 'Ordering of parts to match assembly line consumption sequence.'});
MERGE (:Concept {id: 'warranty-claim', name: 'Warranty Claim', isa95_level: 4, source: 'pack:automotive', description: 'Customer-reported field failure submitted under warranty.'});
