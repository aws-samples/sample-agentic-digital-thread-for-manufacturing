#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Entry bundle budget — runs as part of `npm run build`.
 *
 * The graph stack (cytoscape + cola + popper, ~177 KB gzip) is reached ONLY
 * through the lazy Architecture view, so no entry page may preload it. Before
 * this gate the retired globals-shim (static imports attached to `window`) and
 * a Rollup-hoisted CJS helper put the 554 KB chunk on the login, admin and main
 * entry preload lists. Also caps the login entry's eager JS (its own module +
 * every modulepreload) so a regression that routes around `vendor-graph` by
 * name is still caught.
 *
 * Fails the build (exit 1) with the offending entry and file. */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, '..', 'dist');
const ENTRIES = ['login.html', 'index.html', 'admin.html'];
const FORBIDDEN_PRELOAD = /\/assets\/vendor-graph-[^"']+\.js/;
// Raw (uncompressed) bytes of the login entry's eager JS. The login page today
// is ~2 KB of its own code plus the shared React/Branding chunk (~180 KB raw).
const LOGIN_EAGER_JS_BUDGET_BYTES = 400 * 1024;

function eagerScripts(html) {
  const out = new Set();
  for (const m of html.matchAll(/<script[^>]+type="module"[^>]+src="([^"]+)"/g)) out.add(m[1]);
  for (const m of html.matchAll(/<link[^>]+rel="modulepreload"[^>]+href="([^"]+)"/g)) out.add(m[1]);
  return [...out];
}

let failed = false;
for (const entry of ENTRIES) {
  const path = join(dist, entry);
  let html;
  try {
    html = readFileSync(path, 'utf8');
  } catch {
    console.error(`entry-bundle-budget: missing ${path} — run vite build first`);
    failed = true;
    continue;
  }
  const scripts = eagerScripts(html);
  const graph = scripts.filter((s) => FORBIDDEN_PRELOAD.test(s));
  if (graph.length) {
    console.error(`entry-bundle-budget: ${entry} eagerly loads the graph chunk: ${graph.join(', ')}`);
    failed = true;
  }
  if (entry === 'login.html') {
    const bytes = scripts.reduce((sum, s) => sum + statSync(join(dist, s.replace(/^\//, ''))).size, 0);
    if (bytes > LOGIN_EAGER_JS_BUDGET_BYTES) {
      console.error(`entry-bundle-budget: login eager JS ${bytes} B exceeds ${LOGIN_EAGER_JS_BUDGET_BYTES} B`);
      failed = true;
    } else {
      console.log(`entry-bundle-budget: login eager JS ${bytes} B (budget ${LOGIN_EAGER_JS_BUDGET_BYTES} B)`);
    }
  }
}

// The graph chunk must exist and be reachable only from lazy chunks: an entry
// module (index-/admin-/login-*.js referenced by an HTML <script>) must not
// import it statically.
const assets = readdirSync(join(dist, 'assets'));
const graphChunks = assets.filter((f) => /^vendor-graph-.*\.js$/.test(f));
if (graphChunks.length !== 1) {
  console.error(`entry-bundle-budget: expected exactly one vendor-graph chunk, found ${graphChunks.length}`);
  failed = true;
}

if (failed) process.exit(1);
console.log('entry-bundle-budget: OK — no entry preloads the graph chunk');
