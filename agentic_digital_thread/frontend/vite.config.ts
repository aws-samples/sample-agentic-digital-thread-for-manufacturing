// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Vite config — MPA with three HTML entries (index/login/admin), preserving the
 * exact URLs the server auth middleware and the e2e suite depend on
 * (`/login.html` redirect semantics, `/admin.html`). No react-router. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

const r = (p: string) => fileURLToPath(new URL(p, import.meta.url));

// Chart.js for the sandboxed chart iframes, which load it by absolute URL
// (src/chat/charts.ts). The file comes from the pinned chart.js npm package,
// so the repository carries no copy of it: the dev server answers the URL and
// the build writes the file to the same path in dist/.
const CHART_JS_URL = '/js/vendor/chart.umd.min.js';
const CHART_JS_FILE = r('node_modules/chart.js/dist/chart.umd.min.js');

function chartJsAsset(): Plugin {
  return {
    name: 'adt-chart-js-asset',
    configureServer(server) {
      server.middlewares.use(CHART_JS_URL, (_req, res) => {
        res.setHeader('Content-Type', 'text/javascript');
        res.end(readFileSync(CHART_JS_FILE));
      });
    },
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: CHART_JS_URL.slice(1),
        source: readFileSync(CHART_JS_FILE),
      });
    },
  };
}

// Dev proxy target — the local-mode app (docs/local-development.md) or a
// deployed base URL. Override: VITE_PROXY_TARGET=https://... npm run dev
const proxyTarget = process.env.VITE_PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react(), chartJsAsset()],
  build: {
    rollupOptions: {
      input: {
        index: r('index.html'),
        login: r('login.html'),
        admin: r('admin.html'),
      },
      output: {
        // Split the graph stack into its own chunk — clears the >500 kB
        // warning AND keeps it off the entry preload sets: the graph libraries
        // are reached only through the lazy Architecture view (AppShell) and
        // the lazy thread-click import, so no entry should preload them.
        //
        // The CJS interop helper (Rollup's virtual `commonjsHelpers` module,
        // `getDefaultExportFromCjs`) is shared by cytoscape AND by react-dom /
        // other CJS deps. Left to Rollup's default placement it was hoisted
        // INTO vendor-graph, so the 1 KB login entry imported the 554 KB graph
        // chunk for one helper function. Pin it to its own tiny chunk.
        // Pinned by src/test/entry-bundle-budget.test.ts.
        // (Function form: vitest 4's config types only accept it; equivalent
        // to { 'vendor-graph': ['cytoscape', ...] } under vite 5's rollup.)
        manualChunks(id: string) {
          if (id.includes('commonjsHelpers')) {
            return 'vendor-cjs';
          }
          if (/node_modules\/(cytoscape|cytoscape-cola|cytoscape-popper|webcola)\//.test(id)) {
            return 'vendor-graph';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': { target: proxyTarget, changeOrigin: true },
      '/i3x-explorer': { target: proxyTarget, changeOrigin: true },
      // Public health endpoint (agents/auth/middleware.py PUBLIC_ROUTES) — the
      // e2e suite GETs {base}/health (test_health.py + the conftest liveness
      // probe); without this the dev server would answer with index.html.
      '/health': { target: proxyTarget, changeOrigin: true },
    },
  },
  test: {
    environment: 'node',
    include: ['src/test/**/*.test.ts'],
  },
});
