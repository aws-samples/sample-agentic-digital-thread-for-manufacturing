// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Live MQTT feed logic (#live-feed-panel / #live-feed) — the non-render half
 * of the old feed.ts (jsx-migration W2.1). pollLiveFeed seeds the store's
 * feed slice from GET /api/iot/recent (+/api/iot/stats); the SSE 'mqtt'
 * handler appends through appendLiveFeedMessage (buffering while paused,
 * cap 50). Lines are pre-formatted here ({ts, topic, val} — the seed and
 * append paths keep their original, different timestamp decodings) and
 * rendered by LiveFeed.tsx. */
import { requestJson } from '../../api/client';
import type { IotMessage, IotStats } from '../../api/types/dashboard';
import {
  appendFeed,
  dashboardStore,
  makeFeedLine,
  seedFeed,
  setFeedPaused,
  setIotStats,
  type FeedLine,
} from './store';

declare global {
  interface Window {
    _feedCategories?: Set<string>;
  }
}

/** Honest empty-state for the live feed (Task 1C) — derived from the
 * subscriber lifecycle stats. Returns null when there is nothing definite to
 * say (stats never loaded, or events are flowing). Pure — unit-tested. */
export interface FeedEmptyState {
  text: string;
  tooltip?: string;
}

export function liveFeedEmptyState(stats: IotStats | null | undefined): FeedEmptyState | null {
  if (!stats) return null;
  if (!stats.enabled || !stats.endpoint_configured) {
    return { text: 'Live events not configured — IoT Core ingestion is disabled on this deployment.' };
  }
  if (stats.started && !stats.connected) {
    const err = stats.last_error;
    const tooltip = err && err.message
      ? err.message + (err.timestamp ? ' (' + err.timestamp + ')' : '')
      : undefined;
    return { text: 'Not connected to IoT Core — retrying.', tooltip };
  }
  if (stats.connected && (stats.total_messages || 0) === 0) {
    // Vendor-neutral: true whether or not the deployment ships an OT hub
    // (e.g. a workshop package built with --highbyte off).
    return { text: 'Connected, but nothing is publishing to IoT Core yet.' };
  }
  return null;
}

let _lastFeedTs: number | string | null = null;
let _feedPaused = false;
let _feedBuffer: IotMessage[] = []; // buffer messages while paused

/** One-shot seed of the feed from /api/iot/recent + /api/iot/stats.
 * (dashboard.js `pollLiveFeed`; live updates arrive via SSE.) Stats are
 * fetched FIRST and unconditionally (Task 1C): the honest empty states need
 * the lifecycle stats even when no message has ever arrived — the old code
 * skipped the stats fetch entirely when the recent list was empty. */
export async function pollLiveFeed(): Promise<void> {
  try {
    // A non-2xx JSON error envelope must never be stored as IotStats — it
    // would render a misleading "not configured" empty state. requestJson
    // rejects so the catch keeps the last good stats.
    setIotStats(await requestJson<IotStats>('/api/iot/stats'));
  } catch {
    /* stats unavailable — leave the slice as-is */
  }
  try {
    const { messages } = await requestJson<{ messages?: IotMessage[] }>(
      '/api/iot/recent?limit=15',
    );
    if (!messages || !messages.length) return;
    // Only append new messages
    const newest = messages[0];
    if (_lastFeedTs && newest.timestamp === _lastFeedTs) return;
    _lastFeedTs = newest.timestamp ?? null;
    const lines: FeedLine[] = messages
      .slice(0, 8)
      .reverse()
      .map((m) => {
        const ts = m.timestamp
          ? new Date((m.timestamp as number) * 1000).toLocaleTimeString()
          : '';
        const topic = (m.topic || '').split('/').slice(-2).join('/');
        const val =
          typeof m.payload === 'object'
            ? JSON.stringify(m.payload).slice(0, 80)
            : String(m.payload).slice(0, 80);
        return makeFeedLine(ts, topic, val);
      });
    seedFeed(lines);
  } catch {
    /* MQTT may not be connected — silent */
  }
}

export function toggleFeedPause(): void {
  _feedPaused = !_feedPaused;
  setFeedPaused(_feedPaused);
  if (!_feedPaused && _feedBuffer.length > 0) {
    _feedBuffer.forEach((m) => appendFeedLine(m));
    _feedBuffer = [];
  }
}

function appendFeedLine(msg: IotMessage): void {
  const ts = msg.timestamp ? new Date(msg.timestamp as string).toLocaleTimeString() : '';
  const topic = (msg.topic || '').split('/').slice(-2).join('/');
  const val =
    typeof msg.payload === 'object'
      ? JSON.stringify(msg.payload).slice(0, 80)
      : String(msg.payload || '').slice(0, 80);
  appendFeed(makeFeedLine(ts, topic, val));
  // Track source categories
  const dt = (topic.split('/').pop() || '').toLowerCase();
  if (!window._feedCategories) window._feedCategories = new Set();
  if (['state', 'oee', 'flowrate'].includes(dt)) window._feedCategories.add('Production');
  if (['quality'].includes(dt)) window._feedCategories.add('Quality');
  if (['vibration', 'temperature', 'pressure', 'current', 'sensor'].includes(dt)) {
    window._feedCategories.add('Equipment');
  }
}

/** SSE 'mqtt' handler entry point — buffers while paused (cap 50). */
export function appendLiveFeedMessage(msg: IotMessage): void {
  if (_feedPaused) {
    _feedBuffer.push(msg);
    if (_feedBuffer.length > 50) _feedBuffer.shift();
    return;
  }
  appendFeedLine(msg);
}

/** Test hook parity: expose the paused flag reader for the component. */
export function isFeedPaused(): boolean {
  return dashboardStore.get().feed.paused;
}
