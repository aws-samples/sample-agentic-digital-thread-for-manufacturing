<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Front-end libraries served from /js/vendor/

The SPA serves Chart.js from its own origin at `/js/vendor/chart.umd.min.js`
instead of loading it from a public CDN at runtime.

## Where the file comes from

The repository does not carry a copy of Chart.js. The file is
`node_modules/chart.js/dist/chart.umd.min.js` from the `chart.js` package
pinned in `frontend/package.json` and `package-lock.json` (4.5.1, MIT). A
small plugin in `frontend/vite.config.ts` serves it at that URL from the dev
server, and `npm run build` writes it to `dist/js/vendor/chart.umd.min.js`.

## Why it is a separate file

Everything else the SPA uses is bundled by Vite at build time. Chart.js is the
one exception because it is injected into **sandboxed chart-rendering
iframes** (`frontend/src/chat/charts.ts`, thread data modal). Those iframes use
`srcdoc` with `sandbox="allow-scripts"`, so they have an opaque origin and a
root-relative `/js/vendor/...` path will not resolve. The template therefore
uses an absolute same-origin URL,
`${window.location.origin}/js/vendor/chart.umd.min.js`. A sandboxed iframe can
still fetch a subresource from an absolute URL. If you change the filename,
update those template literals and `vite.config.ts` as well.

Serving it from the same origin means the page loads no third-party script, so
no `integrity=` attribute is needed.

License texts: attribution and the complete upstream license text are in the
repository-root `NOTICE` and `THIRD_PARTY_LICENSES` files.
