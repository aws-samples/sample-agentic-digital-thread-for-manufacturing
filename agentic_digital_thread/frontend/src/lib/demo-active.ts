// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Demo-mode activity flag — ONE store both sides consult:
 * - views/dashboard/demo.ts (the kiosk driver) sets it around a run;
 * - chat/ChatSidebar.tsx suppresses the end-of-turn modal auto-open and the
 *   "response ready" toast while it is true (the demo narrates through the
 *   chat sidebar beside the live investigation graph — a full-screen modal
 *   would cover the graph mid-show).
 * Lives in lib/ (not views/dashboard) so chat/ does not import a view. */
import { createStore } from './store';

export const demoActiveStore = createStore<boolean>(false);

export function setDemoActive(active: boolean): void {
  demoActiveStore.set(active);
}
