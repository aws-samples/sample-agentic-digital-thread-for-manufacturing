<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADT frontend (React + Vite multi-page app)

React 18 + TypeScript port of the ADT web UI, built as a Vite **multi-page
app** with three HTML entries — `index.html`, `login.html`, `admin.html` —
preserving the exact URLs the server auth middleware and the Playwright e2e
suite depend on. In-page view switching on `index.html` is
`location.hash`-driven; there is no client-side router.

## Local development

```bash
cd agentic_digital_thread/frontend
npm ci
npm run dev
```

The Vite dev server proxies `/api` (and `/i3x-explorer`) to a backend. By
default it targets `http://localhost:8000` (the local-mode app from
`docs/local-development.md`). To point at another backend (for example, a deployed
environment), set `VITE_PROXY_TARGET`:

```bash
VITE_PROXY_TARGET=https://<YOUR_BACKEND_BASE_URL> npm run dev
```

## Frontend-only development against a deployed backend

The lightest way to work on the UI: **no Docker, no PostgreSQL, no local
backend at all** — just the Vite dev server proxying every `/api` call to an
already-deployed environment (its Amazon CloudFront domain):

```bash
cd agentic_digital_thread/frontend
npm ci                                                    # first time only
VITE_PROXY_TARGET=https://<YOUR_CLOUDFRONT_DOMAIN> npm run dev
```

Then open `http://localhost:5173` and **sign in through the app itself** with
the deployed environment's credentials. Cookie auth works transparently
through the proxy: the browser only ever talks to `localhost:5173`, so the
`adt_session` cookie the backend sets on `/api/auth/login` is stored — and sent
back — as a first-party, same-origin cookie; the dev proxy forwards it with
every `/api` request (`changeOrigin` rewrites the Host header for
CloudFront). No CORS configuration and no token copying are needed.

**What works:** everything the deployed UI can do — sign-in, the streaming
`/api/ask` chat (SSE through the proxy), dashboard live feeds, onboarding
flows, the admin page, prompt saving. All read **and write** API flows run
against the deployed backend with real data, while the frontend hot-reloads
from your working tree.

**Caveats:**

> **Warning:** Frontend development sends writes to the configured backend.
> Use a development environment. Saved prompts, onboarded or deleted systems,
> and accepted learnings are written to that backend.

- The proxy covers `/api` and `/i3x-explorer` only (see `vite.config.ts`).
  Frontend assets (CSS, the IBM Plex Mono and Outfit web fonts, and Chart.js,
  all from pinned npm packages) are served by the dev server, which is what
  you want when iterating on them.
- Cookie sign-in state is per-origin: `localhost:5173` keeps its own session,
  independent of any session on the deployed domain.

For iterating on **backend** code, use the full local stack in
`docs/local-development.md` instead.

## Build

```bash
npm run build        # emits dist/{index,login,admin}.html + assets/ + public passthroughs
npx tsc --noEmit     # typecheck
npx vitest run       # unit tests
```

## How serving works (dist-only)

`agents/server.py` mounts the UI at `/` from **`frontend/dist/`** when that
directory exists. When it does not, the server logs a warning
(`frontend/dist not found — UI not served; build with: cd frontend && npm run
build`) and serves the API only — there is no fallback UI. Two related details:

- `/i3x-explorer` is mounted from `static/i3x-explorer/` when the deploy.sh-built
  bundle is present (keyed on its `index.html`), mounted before the `/` mount
  so it always wins.
- `agents/auth/middleware.py` lists `/assets/` in `PUBLIC_PREFIXES` so the Vite
  hashed asset files are served without a session (`/css/`, `/js/`, `/fonts/`
  cover the `public/` passthroughs).

The production container builds `dist/` inside the image: the `Dockerfile`
has a `node:22-slim` stage that runs `npm ci && npm run build` and the python
stage copies the result to `frontend/dist`. Local `frontend/dist` and
`frontend/node_modules` are excluded from the Docker build context by using
`.dockerignore`, so the image build is reproducible from the committed
lockfile.

## Rollback

There is no fallback UI. To roll back a bad frontend change,
`git revert` the offending commits and redeploy (the container rebuilds
`dist` from the reverted sources). For local runs, rebuild with
`cd frontend && npm run build`.
