// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Session bootstrap — mint the Conversation Session id eagerly so
 * window.currentSessionId is set before any module (chat, dashboard briefing,
 * thread) reads it; mirrors the top-level mint the retired legacy UI performed
 * at page load.
 *
 * This replaces the former globals-shim.ts, which ALSO attached marked,
 * DOMPurify, cytoscape, Popper and tippy to `window` for the retired legacy
 * <script> tags. Nothing in src/ reads those globals any more (every consumer
 * imports its library), and the static imports forced the 554 KB graph chunk
 * into every entry's preload set — including the login page. Graph libraries
 * now load only through the lazy Architecture view. Pinned by
 * src/test/entry-bundle-budget.test.ts.
 *
 * Import this ONCE from each app entry (main-app, main-admin). */
import { getSessionId } from './lib/session';

getSessionId();

export {};
