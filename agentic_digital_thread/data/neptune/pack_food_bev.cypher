// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Neptune Graph — Food & Beverage Industry Pack
// source: 'pack:food_bev'
// Load after core.cypher. Adds food/beverage-specific concepts beyond the
// universal core. Backed by SiteWise HACCP/CIP/Mixer/Extruder/Oven/Cooler
// /Packager/MetalDetect/CheckWeigher/FoodEnvironment models and CMMS Cedar
// Rapids assets.

// ============================================================
// FOOD SAFETY & SANITATION
// ============================================================
MERGE (:Concept {id: 'haccp-control-point', name: 'HACCP Control Point', isa95_level: 3, source: 'pack:food_bev', description: 'Critical control point in a HACCP plan. SiteWise HACCP model with AllergenVerified, LabelVerified, PackageIntegrity, TempCompliant.'});
MERGE (:Concept {id: 'allergen', name: 'Allergen', isa95_level: 3, source: 'pack:food_bev', description: 'Substance requiring declaration and cross-contact control. SiteWise HACCP.AllergenVerified.'});
MERGE (:Concept {id: 'label-verification', name: 'Label Verification', isa95_level: 3, source: 'pack:food_bev', description: 'Confirmation that the correct label is applied to a product. SiteWise HACCP.LabelVerified.'});
MERGE (:Concept {id: 'package-integrity', name: 'Package Integrity', isa95_level: 3, source: 'pack:food_bev', description: 'Verification that packaging seal/closure is intact. SiteWise HACCP.PackageIntegrity.'});
MERGE (:Concept {id: 'sanitation-record', name: 'Sanitation Record', isa95_level: 3, source: 'pack:food_bev', description: 'Documented evidence that cleaning and sanitizing procedures were completed.'});
MERGE (:Concept {id: 'cip-cycle', name: 'CIP Cycle', isa95_level: 3, source: 'pack:food_bev', description: 'Clean-in-place automated cleaning sequence. SiteWise CIP model with ChemConcentration, Duration, Temperature.'});
MERGE (:Concept {id: 'pathogen-test', name: 'Pathogen Test', isa95_level: 3, source: 'pack:food_bev', description: 'Microbiological test for harmful organisms (Salmonella, Listeria, etc.).'});
MERGE (:Concept {id: 'metal-detection', name: 'Metal Detection', isa95_level: 3, source: 'pack:food_bev', description: 'Inline metal contamination detection. SiteWise MetalDetect with Pass, RejectCount, TotalScanned.'});
MERGE (:Concept {id: 'checkweighing', name: 'Check Weighing', isa95_level: 3, source: 'pack:food_bev', description: 'Inline weight verification of packaged product. SiteWise CheckWeigher with Weight vs TargetWeight.'});

// ============================================================
// PROCESS — FOOD/BEVERAGE
// ============================================================
MERGE (:Concept {id: 'pasteurization', name: 'Pasteurization', isa95_level: 3, source: 'pack:food_bev', description: 'Heat treatment process that destroys pathogens. Backed by SiteWise Oven Temperature + ConveyorSpeed parameters.'});
MERGE (:Concept {id: 'fermentation', name: 'Fermentation', isa95_level: 3, source: 'pack:food_bev', description: 'Biological conversion using microorganisms.'});
MERGE (:Concept {id: 'mixing-blending', name: 'Mixing / Blending', isa95_level: 3, source: 'pack:food_bev', description: 'Batch mixing of ingredients. SiteWise Mixer with BatchWeight, MixTime, Speed.'});
MERGE (:Concept {id: 'extrusion', name: 'Extrusion', isa95_level: 3, source: 'pack:food_bev', description: 'Forming process pushing material through a die. SiteWise Extruder with DieTemp, ScrewSpeed, multi-zone temps.'});
MERGE (:Concept {id: 'baking-oven-process', name: 'Baking / Oven Process', isa95_level: 3, source: 'pack:food_bev', description: 'Thermal processing on a conveyor. SiteWise Oven with ConveyorSpeed, Temperature.'});
MERGE (:Concept {id: 'fluidized-bed-cooling', name: 'Fluidized Bed Cooling', isa95_level: 3, source: 'pack:food_bev', description: 'Post-bake cooling. SiteWise Cooler with MoistureContent, Temperature.'});
MERGE (:Concept {id: 'vffs-packaging', name: 'VFFS Packaging', isa95_level: 3, source: 'pack:food_bev', description: 'Vertical form-fill-seal packaging. SiteWise Packager with FillWeight, SealPressure, SealTemp.'});
MERGE (:Concept {id: 'carbonation', name: 'Carbonation', isa95_level: 3, source: 'pack:food_bev', description: 'CO2 dissolution into beverage. CMMS Milwaukee carbonator assets.'});

// ============================================================
// SUPPLY CHAIN & TRACEABILITY
// ============================================================
MERGE (:Concept {id: 'cold-chain', name: 'Cold Chain', isa95_level: 4, source: 'pack:food_bev', description: 'Temperature-controlled supply chain. SiteWise FoodEnvironment.RefrigerationTemp.'});
MERGE (:Concept {id: 'refrigeration-monitoring', name: 'Refrigeration Monitoring', isa95_level: 2, source: 'pack:food_bev', description: 'Continuous monitoring of refrigeration temperature. SiteWise FoodEnvironment.RefrigerationTemp/RoomTemp/Humidity.'});
MERGE (:Concept {id: 'shelf-life', name: 'Shelf Life', isa95_level: 3, source: 'pack:food_bev', description: 'Duration a product remains safe and meets quality. ERP material_lots.expiry_date.'});
MERGE (:Concept {id: 'best-before-date', name: 'Best Before Date', isa95_level: 3, source: 'pack:food_bev', description: 'Date indicating peak quality period.'});
MERGE (:Concept {id: 'expiry-date', name: 'Expiry Date', isa95_level: 3, source: 'pack:food_bev', description: 'Date after which product is no longer guaranteed. ERP material_lots.expiry_date.'});
MERGE (:Concept {id: 'traceability-lot', name: 'Traceability Lot', isa95_level: 3, source: 'pack:food_bev', description: 'Lot-level linkage for forward and backward trace. ERP lot_warehouse_movements, lot_mes_tracking.'});
MERGE (:Concept {id: 'recall', name: 'Product Recall', isa95_level: 4, source: 'pack:food_bev', description: 'Removal of product from commerce due to safety or quality issue.'});

// ============================================================
// REGULATORY
// ============================================================
MERGE (:Concept {id: 'nutritional-label', name: 'Nutritional Label', isa95_level: 4, source: 'pack:food_bev', description: 'Regulatory-required nutritional content declaration.'});
MERGE (:Concept {id: 'organic-certification', name: 'Organic Certification', isa95_level: 4, source: 'pack:food_bev', description: 'Third-party certification for organic production standards.'});
