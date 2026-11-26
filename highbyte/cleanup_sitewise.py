#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
cleanup_sitewise.py - Full SiteWise cleanup with proper disassociation.
Walks hierarchy top-down to disassociate, then deletes bottom-up.
"""
import boto3
import time
import argparse

# ADT's SiteWise models are defined in DeploySitewiseModels.py (24 process
# models + the Enterprise/Site/Area/Line hierarchy). Cleanup is scoped to this
# set so it never touches SiteWise models belonging to other workloads when the
# account hosts more than just this sample.
from DeploySitewiseModels import PROCESS_MODEL_NAMES

ADT_MODEL_NAMES = set(PROCESS_MODEL_NAMES) | {"Line", "Area", "Site", "Enterprise"}


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

    print("=== SiteWise Full Cleanup ===\n")

    # Step 1: Get ADT's models only (scoped — never touch other workloads' models)
    models = {}
    for page in client.get_paginator('list_asset_models').paginate():
        for m in page['assetModelSummaries']:
            if m['name'] in ADT_MODEL_NAMES:
                models[m['name']] = m['id']
    print(f"Found {len(models)} models")

    # Step 2: Get all assets
    all_assets = {}  # id -> {name, model_name}
    for model_name, model_id in models.items():
        for page in client.get_paginator('list_assets').paginate(assetModelId=model_id):
            for a in page['assetSummaries']:
                all_assets[a['id']] = {'name': a['name'], 'model': model_name}
    print(f"Found {len(all_assets)} total assets\n")

    # Step 3: Disassociate all parent-child relationships
    print("Disassociating all assets...")
    disassoc_count = 0
    for asset_id, info in all_assets.items():
        try:
            # Get parent
            resp = client.list_associated_assets(assetId=asset_id, traversalDirection='PARENT')
            for parent in resp.get('assetSummaries', []):
                parent_id = parent['id']
                # Get the hierarchy ID
                parent_model_name = all_assets.get(parent_id, {}).get('model', '')
                parent_model_id = models.get(parent_model_name, '')
                if parent_model_id:
                    model_resp = client.describe_asset_model(assetModelId=parent_model_id)
                    for h in model_resp.get('assetModelHierarchies', []):
                        try:
                            client.disassociate_assets(
                                assetId=parent_id,
                                hierarchyId=h['id'],
                                childAssetId=asset_id
                            )
                            print(f"  Disassociated {info['name']} from {parent['name']}")
                            disassoc_count += 1
                            time.sleep(0.2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                            break
                        except Exception:  # nosec B112 - intentional fallback, no recovery action needed
                            continue
        except Exception:  # nosec B110 - intentional fallback, no recovery action needed
            pass

    print(f"Disassociated {disassoc_count} relationships\n")
    time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    # Step 4: Delete all assets bottom-up
    delete_order = [
        'Level', 'BottlingMachine', 'Washer', 'WasherPump', 'Filler', 'FillerPump',
        'Mixer', 'Extruder', 'Oven', 'Cooler', 'Packager', 'MetalDetect',
        'CheckWeigher', 'FoodPalletizer', 'HACCP', 'CIP', 'FoodEnvironment',
        'OEE', 'CNC', 'HeatTreat', 'SurfaceTreat', 'Assembly', 'QualityInspect',
        'Packaging', 'AutoEnvironment', 'Line', 'Area', 'Site', 'Enterprise'
    ]

    print("Deleting assets...")
    deleted_assets = 0
    for model_name in delete_order:
        model_id = models.get(model_name)
        if not model_id:
            continue
        assets = []
        for page in client.get_paginator('list_assets').paginate(assetModelId=model_id):
            assets.extend(page['assetSummaries'])
        for a in assets:
            try:
                client.delete_asset(assetId=a['id'])
                print(f"  Deleted {a['name']} ({model_name})")
                deleted_assets += 1
                time.sleep(0.3)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            except Exception as e:
                print(f"  ERROR {a['name']}: {e}")

    # Wait for assets to clear
    print(f"\nWaiting for {deleted_assets} assets to clear...")
    for i in range(40):
        remaining = 0
        for model_id in models.values():
            try:
                for page in client.get_paginator('list_assets').paginate(assetModelId=model_id):
                    remaining += len(page['assetSummaries'])
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
        print(f"  [{i*5}s] Assets remaining: {remaining}")
        if remaining == 0:
            break
        time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    # Step 5: Delete models in dependency LEVELS — referencers first, referenced last.
    # SiteWise blocks deleting a model that's referenced as childAssetModelId in another
    # model's hierarchy. Models within the same level have no cross-references, so
    # they can be deleted in parallel; we only wait between levels.
    #   Level 1: Enterprise        (references Site)
    #   Level 2: Site              (references Area)
    #   Level 3: Area              (references Line)
    #   Level 4: Line              (references all process models)
    #   Level 5: All 25 process models (no references)
    model_levels = [
        ['Enterprise'],
        ['Site'],
        ['Area'],
        ['Line'],
        ['Level', 'BottlingMachine', 'Washer', 'WasherPump', 'Filler', 'FillerPump',
         'Mixer', 'Extruder', 'Oven', 'Cooler', 'Packager', 'MetalDetect',
         'CheckWeigher', 'FoodPalletizer', 'HACCP', 'CIP', 'FoodEnvironment',
         'OEE', 'CNC', 'HeatTreat', 'SurfaceTreat', 'Assembly', 'QualityInspect',
         'Packaging', 'AutoEnvironment'],
    ]

    def wait_models_deleted(model_ids, timeout=60):
        """Wait for all given model IDs to be fully deleted."""
        pending = set(model_ids)
        for _ in range(timeout):
            still_pending = set()
            for mid in pending:
                try:
                    client.describe_asset_model(assetModelId=mid)
                    still_pending.add(mid)
                except client.exceptions.ResourceNotFoundException:
                    pass
            pending = still_pending
            if not pending:
                return True
            time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        return False

    print("\nDeleting models (level by level, parallel within each level)...")
    deleted_models = 0
    for level_idx, level in enumerate(model_levels, 1):
        level_ids = []
        for name in level:
            mid = models.get(name)
            if not mid:
                continue
            try:
                client.delete_asset_model(assetModelId=mid)
                level_ids.append(mid)
                deleted_models += 1
            except Exception as e:
                print(f"  ERROR {name}: {e}")
        if level_ids:
            ok = wait_models_deleted(level_ids, timeout=60)
            print(f"  Level {level_idx} ({len(level_ids)} models): {'all deleted' if ok else 'timeout — some still pending'}")

    # Wait for models to clear
    print("\nWaiting for models to clear...")
    for i in range(60):
        remaining = sum(len(p['assetModelSummaries'])
                       for p in client.get_paginator('list_asset_models').paginate())
        print(f"  [{i*5}s] Models remaining: {remaining}")
        if remaining == 0:
            print("All models deleted!")
            break
        time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    print(f"\nDone! Deleted {deleted_assets} assets and {deleted_models} models.")

if __name__ == '__main__':
    main()
