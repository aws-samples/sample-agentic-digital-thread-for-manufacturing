// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Catalog UI store — per-card interaction state only. Server-owned catalog
 * systems/loading/errors live in onboard-queries.ts (TanStack Query). */
import { createStore } from '../../lib/store';

export interface TestResultVm {
  state: 'idle' | 'testing' | 'done';
  color?: string;
  text?: string;
  /** Button flips to "Retest" once a test has run. */
  ran?: boolean;
}

export interface CatalogState {
  /** Editable connection-string values keyed by system id. */
  connValues: Record<string, string>;
  /** Connection toggle open flags. */
  connOpen: Record<string, boolean>;
  testResults: Record<string, TestResultVm>;
  editOpen: Record<string, boolean>;
  /** Onboard button disabled while its pipeline runs. */
  busy: Record<string, boolean>;
  /** Connect New System modal visibility. */
  connectNewOpen: boolean;
  /** A registration request from the Connect New form is in flight: the
   * form stays open (and keeps its values) until it succeeds. */
  connectNewBusy: boolean;
  /** Why the last Connect New attempt stopped (validation or a failed
   * request), shown inline in the form until the next attempt. */
  connectNewError: string | null;
}

const INITIAL: CatalogState = {
  connValues: {},
  connOpen: {},
  testResults: {},
  editOpen: {},
  busy: {},
  connectNewOpen: false,
  connectNewBusy: false,
  connectNewError: null,
};

export const catalogStore = createStore<CatalogState>(INITIAL);

function patch(partial: Partial<CatalogState>): void {
  catalogStore.update((state) => ({ ...state, ...partial }));
}

export function sysId(system: { id?: string; system_id?: string }): string {
  return (system.id || system.system_id) as string;
}

export function setConnValue(systemId: string, value: string): void {
  catalogStore.update((state) => ({
    ...state,
    connValues: { ...state.connValues, [systemId]: value },
  }));
}

/** The connection string obStart consumes: edited input or caller fallback.
 * The catalog masks inline credentials to "://***@" server-side; that display
 * value is never a usable DSN, so it is treated as absent — the server then
 * resolves the real connection itself (node config or seed bootstrap). */
export function getConnString(
  systemId: string,
  fallback?: string,
): string | undefined {
  const state = catalogStore.get();
  const value =
    systemId in state.connValues ? state.connValues[systemId] : fallback;
  return value && value.includes('://***@') ? undefined : value;
}

export function toggleConnOpen(systemId: string): void {
  catalogStore.update((state) => ({
    ...state,
    connOpen: { ...state.connOpen, [systemId]: !state.connOpen[systemId] },
  }));
}

export function setTestResult(systemId: string, result: TestResultVm): void {
  catalogStore.update((state) => ({
    ...state,
    testResults: { ...state.testResults, [systemId]: result },
  }));
}

export function toggleEditOpen(systemId: string): void {
  catalogStore.update((state) => ({
    ...state,
    editOpen: { ...state.editOpen, [systemId]: !state.editOpen[systemId] },
  }));
}

export function setBusy(systemId: string, busy: boolean): void {
  catalogStore.update((state) => ({
    ...state,
    busy: { ...state.busy, [systemId]: busy },
  }));
}

export function setConnectNewOpen(open: boolean): void {
  // Opening or closing the form abandons any attempt still in flight: its
  // late reply must not close a newer form or write an old error into it.
  liveAttempt = 0;
  patch({ connectNewOpen: open, connectNewBusy: false, connectNewError: null });
}

let attemptSeq = 0;
let liveAttempt = 0;

/** Start a Connect New registration attempt: the form shows "Connecting…"
 * and stays open. Returns the attempt id its completion must present. */
export function beginConnectNewAttempt(): number {
  liveAttempt = ++attemptSeq;
  patch({ connectNewBusy: true, connectNewError: null });
  return liveAttempt;
}

/** A failed attempt reports inline, unless it was abandoned (form closed or
 * reopened since): then its result is dropped. */
export function failConnectNewAttempt(attempt: number, message: string): void {
  if (attempt !== liveAttempt) return;
  liveAttempt = 0;
  patch({ connectNewBusy: false, connectNewError: message });
}

/** A successful attempt closes the form and returns true; an abandoned one
 * returns false and changes nothing (the caller must not start onboarding). */
export function finishConnectNewAttempt(attempt: number): boolean {
  if (attempt !== liveAttempt) return false;
  setConnectNewOpen(false);
  return true;
}

/** Stop a Connect New attempt before it starts (validation); the form stays
 * open with the reason. */
export function setConnectNewError(message: string): void {
  patch({ connectNewBusy: false, connectNewError: message });
}
