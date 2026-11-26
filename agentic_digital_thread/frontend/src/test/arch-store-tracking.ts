// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Test helper: investigation-graph stores made by a test file are disposed
 * after each test, so a store does not keep following catalog name changes
 * (lib/system-names.ts) once its test is over. Call installArchStoreDisposal()
 * at the top level of the file and create stores with trackedArchStore(). */
import { afterEach } from 'vitest';

import { createArchStore, type ArchStore } from '../lib/arch-store';

const created: ArchStore[] = [];

/** A fresh store, disposed after the current test. */
export function trackedArchStore(): ArchStore {
  const store = createArchStore();
  created.push(store);
  return store;
}

/** Register the per-test disposal for this file. */
export function installArchStoreDisposal(): void {
  afterEach(() => {
    for (const store of created.splice(0)) store.dispose();
  });
}
