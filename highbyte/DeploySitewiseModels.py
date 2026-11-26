#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DeploySitewiseModels.py
=======================
Creates SiteWise asset models for the Agentic Digital Thread demo with FULL measurement
schemas pre-defined. With models fully defined, the HighByte pipeline can run with
create:true and ONLY create assets — it does not need to mutate model schemas.

WHAT THIS DOES:
- Creates 24 process-level models with all their measurements + correct dataTypes
- Creates Line model with 24 Processes_<Model> hierarchies + _path STRING attribute
- Creates Area model with Lines hierarchy -> Line + _hierarchyName measurement
- Creates Site model with Areas hierarchy -> Area + _hierarchyName measurement
- Creates Enterprise model with Sites hierarchy -> Site + _hierarchyName measurement
- Total: 28 models (24 process + Line + Area + Site + Enterprise)
  Note: 'Filler' (no pump) is intentionally NOT created — all real fillers have
  pump telemetry and use FillerPump. If you need a non-pump Filler, add it back
  to PROCESS_MEASUREMENTS.

DESIGN PRINCIPLES:
- HighByte never modifies model schemas — every measurement and hierarchy is
  pre-declared here.
- All Pump_* fields use DOUBLE dataType (decimal sensor values, not INTEGER).
- HighByte writes _path and _hierarchyName as MEASUREMENTS during pipeline runs;
  these are pre-declared on every model that HighByte writes to.
- The _path on Line is a STRING attribute (default 'unknown') — different from
  process models because HighByte doesn't currently write _path to the Line itself.

WORKFLOW:
  1. Run highbyte/cleanup_sitewise.py to remove existing models/assets (if redeploying)
  2. Run this script to pre-create all 29 models with full measurement schemas
  3. Configure HighByte SiteWise pipeline with create:true
  4. The HighByte first pipeline cycle creates the assets and associates them
     into the hierarchy. After that, every cycle just writes telemetry values
     to existing measurement properties.

USAGE:
  python3 highbyte/DeploySitewiseModels.py --profile <YOUR_PROFILE> --region us-east-2
  # Or rely on AWS_PROFILE env var / default credential chain:
  python3 highbyte/DeploySitewiseModels.py --region us-east-2
"""

import boto3
import time
import argparse


def attr(name, dataType='STRING', defaultValue='unknown'):
    return {'name': name, 'dataType': dataType,
            'type': {'attribute': {'defaultValue': defaultValue}}}


def meas(name, dataType, unit=None):
    """Measurement property; ``unit`` is emitted only when given (SiteWise's
    PropertyUnit is a free string of 1-256 non-control chars, so '°C' is
    valid and an empty unit must be omitted rather than sent)."""
    prop = {'name': name, 'dataType': dataType}
    if unit:
        prop['unit'] = unit
    prop['type'] = {'measurement': {}}
    return prop


# Common HighByte routing fields written as measurements on every process model
HB_ROUTING = [
    meas('_path', 'STRING'),
    meas('_hierarchyName', 'STRING'),
]

# Common counters/state present on most "machine" process models
COUNTERS = [
    meas('Good', 'INTEGER'),
    meas('Bad', 'INTEGER'),
    meas('Total', 'INTEGER'),
    meas('Down', 'BOOLEAN'),
    meas('Blocked', 'BOOLEAN'),
    meas('Starved', 'BOOLEAN'),
]

# Pump telemetry (decimal sensor data — must be DOUBLE)
PUMP_FIELDS = [
    meas('Pump_pump_general_speed', 'DOUBLE'),
    meas('Pump_pump_process_flow', 'DOUBLE'),
    meas('Pump_pump_process_inlet_pressure', 'DOUBLE'),
    meas('Pump_pump_process_oil_temperature', 'DOUBLE'),
    meas('Pump_pump_journal_bearing_de_temperature', 'DOUBLE'),
    meas('Pump_pump_journal_bearing_de_x_displacement', 'DOUBLE'),
    meas('Pump_pump_journal_bearing_de_y_displacement', 'DOUBLE'),
    meas('Pump_pump_journal_bearing_nde_temperature', 'DOUBLE'),
    meas('Pump_pump_journal_bearing_nde_x_displacement', 'DOUBLE'),
    meas('Pump_pump_journal_bearing_nde_y_displacement', 'DOUBLE'),
    meas('Pump_pump_thrust_bearing_active_temperature', 'DOUBLE'),
    meas('Pump_pump_thrust_bearing_inactive_temperature', 'DOUBLE'),
    meas('Pump_pump_thrust_bearing_axial_position_1', 'DOUBLE'),
    meas('Pump_pump_thrust_bearing_axial_position_2', 'DOUBLE'),
    meas('Pump_motor_journal_bearing_de_temperature', 'DOUBLE'),
    meas('Pump_motor_journal_bearing_de_x_displacement', 'DOUBLE'),
    meas('Pump_motor_journal_bearing_de_y_displacement', 'DOUBLE'),
    meas('Pump_motor_journal_bearing_nde_temperature', 'DOUBLE'),
    meas('Pump_motor_journal_bearing_nde_x_displacement', 'DOUBLE'),
    meas('Pump_motor_journal_bearing_nde_y_displacement', 'DOUBLE'),
]

# Per-model measurement lists (process models). Includes _path/_hierarchyName via HB_ROUTING.
PROCESS_MEASUREMENTS = {
    'Level': [
        meas('Level', 'INTEGER'),
    ],
    'BottlingMachine': COUNTERS,
    'Washer': COUNTERS,
    'WasherPump': COUNTERS + PUMP_FIELDS,
    'FillerPump': COUNTERS + PUMP_FIELDS,
    'Mixer': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('BatchWeight', 'DOUBLE'), meas('MixTime', 'DOUBLE'), meas('Speed', 'DOUBLE'),
    ],
    'Extruder': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('DieTemp', 'DOUBLE'), meas('Pressure', 'DOUBLE'), meas('ScrewSpeed', 'DOUBLE'),
        meas('TempZone1', 'DOUBLE'), meas('TempZone2', 'DOUBLE'), meas('TempZone3', 'DOUBLE'),
    ],
    'Oven': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('ConveyorSpeed', 'DOUBLE'), meas('Temperature', 'DOUBLE'),
    ],
    'Cooler': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('MoistureContent', 'DOUBLE'), meas('Temperature', 'DOUBLE'),
    ],
    'Packager': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Blocked', 'BOOLEAN'), meas('Starved', 'BOOLEAN'),
        meas('Status', 'STRING'),
        meas('FillWeight', 'DOUBLE'), meas('SealPressure', 'DOUBLE'),
        meas('SealTemp', 'DOUBLE'), meas('Speed', 'DOUBLE'),
    ],
    'MetalDetect': [
        meas('Pass', 'BOOLEAN'), meas('RejectCount', 'INTEGER'),
        meas('TotalScanned', 'INTEGER'), meas('Status', 'STRING'),
    ],
    'CheckWeigher': [
        meas('Pass', 'BOOLEAN'), meas('RejectCount', 'INTEGER'),
        meas('TargetWeight', 'INTEGER'), meas('Weight', 'DOUBLE'),
    ],
    'FoodPalletizer': COUNTERS,
    'HACCP': [
        meas('AllergenVerified', 'BOOLEAN'), meas('LabelVerified', 'BOOLEAN'),
        meas('PackageIntegrity', 'BOOLEAN'), meas('TempCompliant', 'BOOLEAN'),
        meas('CriticalTemp', 'DOUBLE'),
    ],
    'CIP': [
        meas('ChemConcentration', 'DOUBLE'), meas('Duration', 'INTEGER'),
        meas('Status', 'STRING'), meas('Temperature', 'DOUBLE'),
    ],
    'FoodEnvironment': [
        meas('Humidity', 'DOUBLE'), meas('RefrigerationTemp', 'DOUBLE'),
        meas('RoomTemp', 'DOUBLE'),
    ],
    'OEE': [
        meas('Availability', 'DOUBLE'), meas('Performance', 'DOUBLE'),
        meas('Quality', 'DOUBLE'), meas('Overall', 'DOUBLE'),
    ],
    'CNC': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('CoolantFlowRate', 'DOUBLE'), meas('CoolantTemp', 'DOUBLE', unit='°C'),
        meas('CycleTime', 'DOUBLE'), meas('FeedRate', 'DOUBLE'),
        meas('HydraulicPressure', 'DOUBLE'), meas('PartsCount', 'INTEGER'),
        meas('PowerConsumption', 'DOUBLE'), meas('SpindleSpeed', 'DOUBLE'),
        meas('ToolChangeEvent', 'BOOLEAN'), meas('ToolLifeRemaining', 'DOUBLE'),
        meas('ToolPosition_X', 'DOUBLE'), meas('ToolPosition_Y', 'DOUBLE'),
        meas('ToolPosition_Z', 'DOUBLE'),
        # RMS velocity per axis (lambda-sitewise-feed story: 1.8 -> 2.8 mm/s).
        meas('Vibration_X', 'DOUBLE', unit='mm/s'), meas('Vibration_Y', 'DOUBLE', unit='mm/s'),
        meas('Vibration_Z', 'DOUBLE', unit='mm/s'),
        # Edge-FFT features of the spindle bearing (ADR-0073): defect bands
        # (mm/s velocity, erp_simulation/condition_monitor.py), dimensionless
        # impulsiveness and the band alarm that raises the CMMS work order.
        meas('Spectrum_1X', 'DOUBLE', unit='mm/s'), meas('Spectrum_BPFO', 'DOUBLE', unit='mm/s'),
        meas('Spectrum_BPFI', 'DOUBLE', unit='mm/s'), meas('CrestFactor', 'DOUBLE'),
        meas('Kurtosis', 'DOUBLE'), meas('BPFO_Alarm', 'BOOLEAN'),
        meas('BPFO_Hz', 'DOUBLE', unit='Hz'), meas('BPFI_Hz', 'DOUBLE', unit='Hz'),
    ],
    'HeatTreat': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('FurnaceTemp', 'DOUBLE'), meas('QuenchTemp', 'DOUBLE'),
        meas('SoakTime', 'DOUBLE'),
    ],
    'SurfaceTreat': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('BathConcentration', 'DOUBLE'), meas('BathTemp', 'DOUBLE'),
        meas('CoatingThickness', 'DOUBLE'),
    ],
    'Assembly': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('Down', 'BOOLEAN'), meas('Status', 'STRING'),
        meas('TorqueTarget', 'INTEGER'), meas('TorqueValue', 'DOUBLE'),
    ],
    'QualityInspect': [
        meas('Good', 'INTEGER'), meas('Bad', 'INTEGER'), meas('Total', 'INTEGER'),
        meas('DefectCode', 'STRING'),
        meas('DimensionX', 'DOUBLE'), meas('DimensionY', 'DOUBLE'), meas('DimensionZ', 'DOUBLE'),
        meas('FirstPassYield', 'DOUBLE'),
        meas('SurfaceFinish_Ra', 'DOUBLE'), meas('TolerancePass', 'BOOLEAN'),
    ],
    'Packaging': COUNTERS,
    'AutoEnvironment': [
        meas('AmbientTemp', 'DOUBLE'), meas('CompressedAirPressure', 'DOUBLE'),
        meas('Humidity', 'DOUBLE'),
    ],
}

# Order matters — process models must be created before Line (which references them)
PROCESS_MODEL_NAMES = list(PROCESS_MEASUREMENTS.keys())


def wait_for_active(client, model_id, name, timeout=120):
    for _ in range(timeout // 2):
        resp = client.describe_asset_model(assetModelId=model_id)
        state = resp['assetModelStatus']['state']
        if state == 'ACTIVE':
            return True
        if state == 'FAILED':
            print(f"  ERROR: {name} failed: {resp['assetModelStatus'].get('error', {})}")
            return False
        time.sleep(2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
    print(f"  TIMEOUT waiting for {name}")
    return False


def create_or_get_model(client, name, properties=None, hierarchies=None, reconcile=True):
    """Find the model by name or create it. ``reconcile=False`` keeps the
    found model untouched: a caller whose role has no UpdateAssetModel (the
    workshop seed) must never attempt the update."""
    for page in client.get_paginator('list_asset_models').paginate():
        for m in page['assetModelSummaries']:
            if m['name'] == name:
                print(f"  Found existing: {name} ({m['id']})")
                if reconcile:
                    reconcile_model(client, m['id'], name, properties, hierarchies)
                return m['id']
    kwargs = {'assetModelName': name, 'assetModelProperties': properties or []}
    if hierarchies:
        kwargs['assetModelHierarchies'] = hierarchies
    resp = client.create_asset_model(**kwargs)
    model_id = resp['assetModelId']
    print(f"  Created: {name} ({model_id}) — {len(properties or [])} props, {len(hierarchies or [])} hierarchies")
    wait_for_active(client, model_id, name)
    return model_id


# Keys of a described property/hierarchy that UpdateAssetModel accepts back.
_PROPERTY_KEYS = ('id', 'externalId', 'name', 'dataType', 'dataTypeSpec', 'unit', 'type')
_HIERARCHY_KEYS = ('id', 'externalId', 'name', 'childAssetModelId')


def _keep(entry, keys):
    return {k: entry[k] for k in keys if k in entry}


def reconcile_model(client, model_id, name, properties=None, hierarchies=None):
    """ADD the declared properties and hierarchies an existing model lacks.

    A model left by an older deploy (the account kept its SiteWise models
    after a stack delete) keeps its old property set, and SFC drops every
    channel whose property is missing, so the new channels never reach
    SiteWise. Matching is by name. Nothing existing is removed: UpdateAssetModel
    replaces the whole definition, so every existing property and hierarchy
    is sent back with its id. The one change to an existing property is its
    unit, when the declaration sets one the model lacks or spells differently:
    a unit-less vibration channel left the model to guess g or mm/s. A model
    with composite models is left alone (none of ours has one). Returns the
    names added or given their declared unit."""
    desc = client.describe_asset_model(assetModelId=model_id)
    old_props = desc.get('assetModelProperties', [])
    old_hiers = desc.get('assetModelHierarchies', [])
    have_p = {p['name'] for p in old_props}
    have_h = {h['name'] for h in old_hiers}
    add_p = [p for p in properties or [] if p['name'] not in have_p]
    add_h = [h for h in hierarchies or [] if h['name'] not in have_h]
    declared_unit = {p['name']: p['unit'] for p in properties or [] if p.get('unit')}
    unit_fix = {p['name']: declared_unit[p['name']] for p in old_props
                if p['name'] in declared_unit and p.get('unit') != declared_unit[p['name']]}
    added = [p['name'] for p in add_p] + [h['name'] for h in add_h] + sorted(unit_fix)
    if not added:
        return []
    if desc.get('assetModelCompositeModels'):
        print(f"  WARNING: {name} has composite models; not adding {added}")
        return []
    kwargs = {
        'assetModelId': model_id,
        'assetModelName': desc.get('assetModelName', name),
        'assetModelProperties': [
            {**_keep(p, _PROPERTY_KEYS),
             **({'unit': unit_fix[p['name']]} if p['name'] in unit_fix else {})}
            for p in old_props] + add_p,
        'assetModelHierarchies': [_keep(h, _HIERARCHY_KEYS) for h in old_hiers] + add_h,
    }
    if desc.get('assetModelDescription'):
        kwargs['assetModelDescription'] = desc['assetModelDescription']
    client.update_asset_model(**kwargs)
    print(f"  Updated: {name} ({model_id}) — added {added}")
    wait_for_active(client, model_id, name)
    return added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', default=None,
                        help='AWS profile name (defaults to AWS_PROFILE env var or default credential chain)')
    parser.add_argument('--region', default='eu-central-1')
    args = parser.parse_args()
    if args.profile:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
    else:
        session = boto3.Session(region_name=args.region)
    client = session.client('iotsitewise')

    print(f"=== Deploying SiteWise models to {args.region} ===\n")
    deploy_models(client)


def deploy_models(client):
    """Create or find all 28 models; returns ``{name: model_id}``. Idempotent.

    Shared by this CLI (deploy.sh step 1a) and the SFC stack's asset-tree
    custom resource (lambda-sfc-sitewise-tree), so a Workshop Studio account,
    which never runs deploy.sh, gets the same models."""
    print(f"Step 1: Creating {len(PROCESS_MODEL_NAMES)} process models with full measurement schemas...")
    model_ids = {}
    for name in PROCESS_MODEL_NAMES:
        # Each process model gets its specific measurements + HighByte routing fields
        properties = PROCESS_MEASUREMENTS[name] + HB_ROUTING
        model_ids[name] = create_or_get_model(client, name, properties=properties)
        time.sleep(0.5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    print(f"\nStep 2: Creating Line model with {len(PROCESS_MODEL_NAMES)} Processes_<Model> hierarchies + _path attribute...")
    line_hierarchies = [
        {'name': f'Processes_{m}', 'childAssetModelId': model_ids[m]}
        for m in PROCESS_MODEL_NAMES
    ]
    line_properties = [
        attr('_path', 'STRING', 'unknown'),
    ]
    model_ids['Line'] = create_or_get_model(client, 'Line',
                                             properties=line_properties,
                                             hierarchies=line_hierarchies)
    time.sleep(2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    print("\nStep 3: Creating Area model with Lines hierarchy...")
    model_ids['Area'] = create_or_get_model(client, 'Area',
                                             properties=[meas('_hierarchyName', 'STRING')],
                                             hierarchies=[{'name': 'Lines', 'childAssetModelId': model_ids['Line']}])
    time.sleep(2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    print("\nStep 4: Creating Site model with Areas hierarchy...")
    model_ids['Site'] = create_or_get_model(client, 'Site',
                                             properties=[meas('_hierarchyName', 'STRING')],
                                             hierarchies=[{'name': 'Areas', 'childAssetModelId': model_ids['Area']}])
    time.sleep(2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    print("\nStep 5: Creating Enterprise model with Sites hierarchy...")
    model_ids['Enterprise'] = create_or_get_model(client, 'Enterprise',
                                                   properties=[meas('_hierarchyName', 'STRING')],
                                                   hierarchies=[{'name': 'Sites', 'childAssetModelId': model_ids['Site']}])

    print(f"\nDone. Created/found {len(model_ids)} models.")
    for name in sorted(model_ids):
        print(f"  {name}: {model_ids[name]}")
    return model_ids


if __name__ == '__main__':
    main()
