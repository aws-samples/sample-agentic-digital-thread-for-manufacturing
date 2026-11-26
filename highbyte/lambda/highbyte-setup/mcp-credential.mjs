// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MCP credential reconciliation for the HighByte setup Lambda.
 *
 * The ADT app calls the HighByte MCP server with a bearer token read from the
 * Secrets Manager secret `adt/conn/highbyte` ({"token": "<key>"}). The MCP
 * server rejects a missing or wrong key with HTTP 401 (verified on 4.4.2), so
 * an absent or stale secret is NOT a degraded-but-working mode -- the whole
 * HighByte MCP workshop path is broken for that account.
 *
 * `reconcileMcpCredential` is therefore a REQUIRED postcondition on every
 * Create/Update run: after it returns, `adt/conn/highbyte` exists and its
 * token authenticates against the hub. It is idempotent -- when the stored
 * token already authenticates it mints no new key -- and it fails loudly
 * (throws) when it cannot establish a working key, so the custom resource
 * fails instead of reporting a success the account cannot use.
 *
 * Verification does the real HighByte 4.4.2 MCP handshake: 4.4.2 is
 * stateful Streamable HTTP -- a client first POSTs
 * `initialize`, captures the `Mcp-Session-Id` response header, sends
 * `notifications/initialized`, then POSTs `tools/list` carrying that session
 * header, and the production probe (connection_probes.py) does exactly this.
 * A bare `tools/list` with no session is answered HTTP 400, so the old
 * "first request is tools/list, 200 == good" check mis-read a protocol
 * rejection as a bad key and rotated working credentials. `mcpVerify`
 * classifies each outcome:
 *   - "ok"             the handshake returned a real tools-list JSON-RPC result
 *   - "bad_key"        401/403 at initialize -- the key is genuinely rejected
 *   - "unavailable"    transport error / 5xx / timeout -- the hub is down or
 *                      restarting (trial-expiry task replacement), retry it
 *   - "protocol_error" anything else (e.g. a 400 because the session rules
 *                      changed) -- surfaced, never treated as a bad key
 * Only "bad_key" rotates; "unavailable" is retried with bounded exponential
 * backoff before the reconcile gives up.
 *
 * Rotation deletes the prior key by its HighByte `friendlyName` -- the 4.4.2
 * delete route is `DELETE /v1/user/{id}/{ver}/apiKey/{friendlyName}` (captured
 * from the 4.4.2 UI bundle: `deleteApiKey` calls that path with
 * `t.friendlyName`), NOT by the create response's `storedKey`. The secret
 * therefore persists `{token, storedKey, friendlyName}` and cleanup deletes by
 * `friendlyName`. A freshly minted key that does not authenticate is deleted
 * before the next attempt so failed attempts never leak keys toward the
 * per-user key limit.
 *
 * All I/O is injected (fetch-like `fetch`, a small `secrets` facade, an
 * optional `sleep`), so the reconciliation logic is unit-tested under node
 * with fakes, no AWS or network. index.mjs supplies the real clients.
 */

/** The one HighByte API-key friendlyName this reconciler manages. */
export const MANAGED_KEY_NAME = "mcpkey";

// --------------------------------------------------------------------------
// Time budget. The setup custom resource runs in a
// 5-minute Lambda. The reconcile therefore works to ONE absolute wall-clock
// deadline that covers request time AND backoff sleeps: every request timeout
// and every nap is capped to the time left, and no request is started with
// less than MIN_REQUEST_MS left. index.mjs sets the deadline from the
// invocation's own getRemainingTimeInMillis() minus SETUP_RESERVE_MS, the time
// kept back for the project fetch + import and the return to the CloudFormation
// provider framework, so the handler always fails (and reports) with a clear
// error instead of being killed by the Lambda timeout.
// --------------------------------------------------------------------------

/** Ceiling for any single hub request this module makes. */
export const MCP_REQUEST_TIMEOUT_MS = 10000;
/** No new hub request is started with less than this much time left. */
export const MIN_REQUEST_MS = 1000;
/** Kept back after the reconcile deadline: project fetch + import + return. */
export const SETUP_RESERVE_MS = 60000;
/** Budget when a caller passes no deadline: bounded, never open-ended. */
export const DEFAULT_RECONCILE_BUDGET_MS = 180000;
/** Hard cap on availability re-verifies, independent of the clock. */
const MAX_VERIFY_TRIES = 20;

/** The absolute reconcile deadline for a handler that started at *startMs*
 *  with *remainingMs* of Lambda time left. */
export function reconcileDeadline(startMs, remainingMs, reserveMs = SETUP_RESERVE_MS) {
    return startMs + remainingMs - reserveMs;
}

/** Thrown when the reconcile reaches its deadline before it can finish. */
export class ReconcileDeadlineError extends Error {
    constructor(message) {
        super(message);
        this.name = "ReconcileDeadlineError";
    }
}

/** A deadline-bound budget: remaining time and per-request timeouts. */
export function makeBudget(deadline, now = Date.now) {
    return {
        remaining() { return deadline - now(); },
        /** A request timeout capped to the time left; throws if too little is left. */
        requestTimeout(what) {
            const left = deadline - now();
            if (left < MIN_REQUEST_MS) {
                throw new ReconcileDeadlineError(
                    `MCP credential reconciliation ran out of its time budget before ${what} `
                    + `(${Math.max(0, Math.round(left))} ms left); failing now so the setup `
                    + "custom resource reports instead of hitting the Lambda timeout");
            }
            return Math.min(MCP_REQUEST_TIMEOUT_MS, left);
        },
    };
}

/** No deadline: every request still gets the 10 s ceiling. */
const UNBOUNDED = makeBudget(Infinity, () => 0);

/** fetch init with a budget-capped timeout, computed BEFORE any try block so a
 *  deadline error is never mistaken for an unreachable hub. `timeoutMs` is
 *  ignored by real fetch and read by the test fakes. */
function timed(budget, what, init) {
    const timeoutMs = budget.requestTimeout(what);
    return { ...init, signal: AbortSignal.timeout(timeoutMs), timeoutMs };
}

/** JSON-RPC initialize body: opens a Streamable-HTTP session. */
function initializeBody() {
    return {
        jsonrpc: "2.0", id: 1, method: "initialize",
        params: {
            protocolVersion: "2024-11-05", capabilities: {},
            clientInfo: { name: "adt-setup", version: "1.0" },
        },
    };
}

/** JSON-RPC body that lists tools: the authenticated round-trip we accept as proof. */
const MCP_TOOLS_LIST = { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} };

const MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
};

/**
 * Perform the HighByte 4.4.2 MCP handshake with *token* against *mcpEndpoint*
 * and classify the result. Returns `{status, detail}` where status is one of
 * "ok" | "bad_key" | "unavailable" | "protocol_error". Throws only
 * ReconcileDeadlineError, when *budget* has too little time left for a request.
 */
export async function mcpVerify(fetchFn, mcpEndpoint, token, log = console.log, budget = UNBOUNDED) {
    if (!token) return { status: "bad_key", detail: "no stored token" };
    if (!mcpEndpoint) return { status: "unavailable", detail: "no MCP endpoint" };
    const authHeaders = { ...MCP_HEADERS, "Authorization": "Bearer " + token };

    // Step 1: initialize -> establishes a session (or rejects the key).
    let initResp;
    const initInit = timed(budget, "MCP initialize", {
        method: "POST", headers: authHeaders, body: JSON.stringify(initializeBody()),
    });
    try {
        initResp = await fetchFn(mcpEndpoint, initInit);
    } catch (e) {
        log(`MCP initialize could not reach ${mcpEndpoint}: ${e.message}`);
        return { status: "unavailable", detail: e.message };
    }
    if (initResp.status === 401 || initResp.status === 403) {
        return { status: "bad_key", detail: `initialize HTTP ${initResp.status}` };
    }
    if (initResp.status >= 500) {
        return { status: "unavailable", detail: `initialize HTTP ${initResp.status}` };
    }
    if (initResp.status !== 200) {
        return { status: "protocol_error", detail: `initialize HTTP ${initResp.status}` };
    }
    const sessionId = initResp.headers.get("Mcp-Session-Id");

    // Step 2: notifications/initialized (best-effort; some servers require it).
    const sessionHeaders = sessionId
        ? { ...authHeaders, "Mcp-Session-Id": sessionId } : { ...authHeaders };
    const notifyInit = timed(budget, "MCP notifications/initialized", {
        method: "POST", headers: sessionHeaders,
        body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    });
    try {
        await fetchFn(mcpEndpoint, notifyInit);
    } catch (e) {
        log(`MCP notifications/initialized skipped (non-fatal): ${e.message}`);
    }

    // Step 3: tools/list on that session -> accept ONLY a real tools-list result.
    let listResp;
    const listInit = timed(budget, "MCP tools/list", {
        method: "POST", headers: sessionHeaders, body: JSON.stringify(MCP_TOOLS_LIST),
    });
    try {
        listResp = await fetchFn(mcpEndpoint, listInit);
    } catch (e) {
        log(`MCP tools/list could not reach ${mcpEndpoint}: ${e.message}`);
        return { status: "unavailable", detail: e.message };
    }
    if (listResp.status === 401 || listResp.status === 403) {
        return { status: "bad_key", detail: `tools/list HTTP ${listResp.status}` };
    }
    if (listResp.status >= 500) {
        return { status: "unavailable", detail: `tools/list HTTP ${listResp.status}` };
    }
    if (listResp.status !== 200) {
        return { status: "protocol_error", detail: `tools/list HTTP ${listResp.status}` };
    }
    let body;
    try {
        body = await listResp.json();
    } catch (e) {
        return { status: "protocol_error", detail: `tools/list body unparseable: ${e.message}` };
    }
    if (body && body.result && Array.isArray(body.result.tools)) {
        return { status: "ok", detail: `${body.result.tools.length} tool(s)` };
    }
    return { status: "protocol_error", detail: "tools/list returned no JSON-RPC tools result" };
}

/**
 * The {token, storedKey, friendlyName} currently in the secret.
 *
 * Only ResourceNotFoundException means "absent"
 * (returns {} -> a key is minted). Every other read failure -- AccessDenied,
 * throttling, timeouts, any SDK error -- says nothing about the stored key, so
 * it is RETHROWN and the reconcile stops with zero key mutations. A secret that
 * exists but is unparseable, not an object, or has no token is unusable by the
 * ADT app too: it returns {malformed: reason}, which the caller repairs by
 * rotation with a log line naming why.
 */
export async function readStoredCredential(secrets, secretId, log = console.log,
                                           budget = UNBOUNDED) {
    // Capped to the reconcile budget; computed outside the
    // try so a spent budget surfaces as a deadline error, not a read error.
    const timeoutMs = budget.requestTimeout(`reading secret ${secretId}`);
    let raw;
    try {
        raw = await secrets.get(secretId, { timeoutMs });
    } catch (e) {
        if (e && e.name === "ResourceNotFoundException") {
            log(`Secret ${secretId} does not exist yet`);
            return {};
        }
        throw new Error(
            `reading secret ${secretId} failed (${(e && e.name) || "Error"}: ${e && e.message}); `
            + "the stored MCP key is unknown, so no key was deleted or minted");
    }
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (e) {
        return { malformed: `not valid JSON (${e.message})` };
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { malformed: "not a JSON object" };
    }
    if (typeof parsed.token !== "string" || parsed.token === "") {
        return { malformed: "it has no token", friendlyName: parsed.friendlyName };
    }
    return { token: parsed.token, storedKey: parsed.storedKey, friendlyName: parsed.friendlyName };
}

/**
 * Best-effort delete of a managed API key by its HighByte `friendlyName`
 * (`DELETE /v1/user/{id}/{ver}/apiKey/{friendlyName}` -- the 4.4.2 route).
 * Never throws: a failed cleanup must not fail the reconcile (the new key
 * still works). A missing friendlyName falls back to MANAGED_KEY_NAME, since
 * every key this reconciler creates uses that one fixed name.
 */
export async function deleteKeyByName(fetchFn, configEndpoint, adminId, apiVersion,
                                      authToken, friendlyName, log = console.log,
                                      budget = UNBOUNDED) {
    const name = friendlyName || MANAGED_KEY_NAME;
    const url = `${configEndpoint}/v1/user/${adminId}/${apiVersion}/apiKey/${encodeURIComponent(name)}`;
    const init = timed(budget, `deleting MCP API key '${name}'`, {
        method: "DELETE", headers: { "Authorization": authToken },
    });
    try {
        const response = await fetchFn(url, init);
        log(response.ok
            ? `Deleted MCP API key '${name}'`
            : `MCP API key '${name}' delete returned HTTP ${response.status} (ignored)`);
    } catch (e) {
        log(`MCP API key '${name}' delete skipped (non-fatal): ${e.message}`);
    }
}

/**
 * Create one MCP API key for the administrator. Returns
 * {token, storedKey, friendlyName} -- from this moment the CALLER owns the
 * key: the secret write is a separate step, so a
 * failed write can never orphan a key the caller does not know about. Throws
 * when key creation fails.
 */
export async function createKey(fetchFn, configEndpoint, adminId, apiVersion, authToken,
                                log = console.log, budget = UNBOUNDED) {
    const oneYearMs = Date.now() + 365 * 24 * 60 * 60 * 1000;
    const friendlyName = MANAGED_KEY_NAME;
    const init = timed(budget, "creating the MCP API key", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": authToken },
        body: JSON.stringify({ expiration: oneYearMs, friendlyName }),
    });
    let response;
    try {
        response = await fetchFn(`${configEndpoint}/v1/user/${adminId}/${apiVersion}/apiKey`, init);
    } catch (e) {
        // A timeout or transport error leaves it unknown whether the hub
        // created the key: the caller deletes the managed name to be sure.
        const err = new Error(`HighByte MCP API key creation got no answer (${e.message})`);
        err.ambiguous = true;
        throw err;
    }
    if (!response.ok) {
        throw new Error(`HighByte MCP API key creation failed: HTTP ${response.status}`);
    }
    const body = await response.json();
    const token = body.secret;
    if (!token) {
        const err = new Error("HighByte MCP API key creation returned no secret value");
        err.ambiguous = true;  // a key exists that nobody can use
        throw err;
    }
    log(`Created MCP API key '${friendlyName}'`);
    return { token, storedKey: body.storedKey, friendlyName };
}

/**
 * Write a minted key's {token, storedKey, friendlyName} to the secret. Throws
 * on a Put/CreateSecret failure; the caller then deletes the key it owns.
 */
export async function storeKey(secrets, secretId, minted, log = console.log, budget = UNBOUNDED) {
    const timeoutMs = budget.requestTimeout(`writing secret ${secretId}`);
    await secrets.put(secretId, JSON.stringify({
        token: minted.token, storedKey: minted.storedKey, friendlyName: minted.friendlyName }),
        { timeoutMs });
    log(`Wrote new MCP bearer token to ${secretId}`);
}

/** Sleep *ms* -- the default; injected as `sleep` in tests so no real wait. */
function defaultSleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Verify *token*, retrying "unavailable" with exponential backoff until the
 * budget's deadline. Returns the final classification. A hub that is
 * restarting (transport/5xx) is given time to come back BEFORE the caller
 * decides anything, so a transient outage never mints a key. Every nap is
 * capped to the time left (keeping MIN_REQUEST_MS for the next verify), and
 * each verify's requests are capped by the same budget, so the whole loop --
 * requests and sleeps -- ends by the deadline.
 */
export async function verifyWithAvailabilityRetry(fetchFn, mcpEndpoint, token, opts) {
    const { sleep = defaultSleep, backoffBaseMs = 2000, log = console.log,
            budget = UNBOUNDED } = opts || {};
    let delay = backoffBaseMs;
    let tries = 1;
    let result = await mcpVerify(fetchFn, mcpEndpoint, token, log, budget);
    while (result.status === "unavailable" && tries < MAX_VERIFY_TRIES) {
        const left = budget.remaining();
        if (left <= MIN_REQUEST_MS) break;  // no time for another verify
        const nap = Math.max(0, Math.min(delay, left - MIN_REQUEST_MS));
        log(`MCP hub unavailable (${result.detail}); retrying in ${nap}ms (${Math.round(left)} ms left)`);
        await sleep(nap);
        delay *= 2;
        tries += 1;
        result = await mcpVerify(fetchFn, mcpEndpoint, token, log, budget);
    }
    return result;
}

/**
 * The error a non-rotating verify outcome turns into. Only "bad_key" ever
 * rotates; "protocol_error" and "unavailable" say
 * nothing about the key itself, so the reconcile stops WITHOUT deleting or
 * minting anything and leaves the stored credential exactly as it was.
 */
function nonRotatingError(result, which) {
    if (result.status === "unavailable") {
        return new Error(
            `HighByte MCP hub unavailable while verifying the ${which} key, retried until the `
            + `setup time budget ran out (${result.detail}); no key was deleted or minted`);
    }
    return new Error(
        `HighByte MCP protocol error while verifying the ${which} key (${result.detail}); `
        + "this is not an auth rejection, so no key was deleted or minted");
}

/**
 * Reconcile the MCP credential as a required postcondition. Returns the
 * working bearer token. Rotates ONLY on a real auth rejection (bad_key).
 * Throws -- without deleting or minting -- when the hub stays unavailable past
 * the backoff budget or answers with a protocol error, and throws when, after
 * *maxAttempts* rotations, no minted key authenticates.
 *
 * @param {object} opts
 * @param {Function} opts.fetchFn               fetch-like
 * @param {object}   opts.secrets               {get, put} facade over Secrets Manager
 * @param {string}   opts.configEndpoint        hub config API base (ALB :45245)
 * @param {string}   opts.mcpEndpoint           hub MCP endpoint to verify against (:8885/mcp)
 * @param {string}   opts.adminId               administrator user id
 * @param {string}   opts.apiVersion            HighByte /v1/user API version segment
 * @param {string}   opts.authToken             admin bearer (from POST /login)
 * @param {string}   opts.secretId              Secrets Manager id for the conn/highbyte secret
 * @param {number}   [opts.maxAttempts=3]       bounded rotation attempts
 * @param {Function} [opts.sleep]               injected sleep (tests advance a fake clock)
 * @param {number}   [opts.backoffBaseMs=2000]  first availability backoff
 * @param {Function} [opts.now=Date.now]        clock (tests inject a fake)
 * @param {number}   [opts.deadline]            absolute ms deadline for the whole reconcile
 *                                              (default now + DEFAULT_RECONCILE_BUDGET_MS)
 * @param {number}   [opts.cleanupDeadline]     absolute ms deadline for deleting a key the
 *                                              reconcile OWNS (minted, unpersisted or
 *                                              rejected); later than *deadline* so a write
 *                                              that ate the reconcile budget can still be
 *                                              cleaned up (default: *deadline*)
 * @param {Function} [opts.log]
 */
export async function reconcileMcpCredential(opts) {
    const { fetchFn, secrets, configEndpoint, mcpEndpoint, adminId, apiVersion,
            authToken, secretId, maxAttempts = 3, sleep, backoffBaseMs,
            now = Date.now, log = console.log } = opts;
    const deadline = opts.deadline ?? (now() + DEFAULT_RECONCILE_BUDGET_MS);
    const budget = makeBudget(deadline, now);
    // Owned-key cleanup gets its own, later deadline.
    const cleanup = makeBudget(Math.max(deadline, opts.cleanupDeadline ?? deadline), now);
    const retryOpts = { sleep, backoffBaseMs, log, budget };
    const deleteOwned = (why) => {
        log(`Deleting MCP API key '${MANAGED_KEY_NAME}' this run owns: ${why}`);
        return deleteKeyByName(fetchFn, configEndpoint, adminId, apiVersion,
                               authToken, MANAGED_KEY_NAME, log, cleanup);
    };

    const stored = await readStoredCredential(secrets, secretId, log, budget);
    if (stored.malformed) {
        // Explicit, logged repair: the secret exists but neither ADT nor this
        // reconcile can use it, so the managed key it should name is replaced.
        log(`Secret ${secretId} exists but ${stored.malformed}; ADT cannot use it -- `
            + "repairing by rotating the managed MCP key");
    }
    const storedResult = stored.malformed
        ? { status: "bad_key", detail: `malformed secret: ${stored.malformed}` }
        : await verifyWithAvailabilityRetry(fetchFn, mcpEndpoint, stored.token, retryOpts);
    if (storedResult.status === "ok") {
        log("Stored MCP token already authenticates -- no new key minted");
        return stored.token;
    }
    if (storedResult.status !== "bad_key") {
        // unavailable past the backoff budget, or a protocol error: neither is
        // evidence the key is bad, so fail without touching the credential.
        throw nonRotatingError(storedResult, "stored");
    }
    log(`Stored MCP token rejected (${storedResult.detail}); rotating`);

    // Delete the key the secret named BEFORE minting a replacement, by its
    // friendlyName (the 4.4.2 delete route). On a fresh provision there is
    // nothing to delete.
    if (stored.token || stored.friendlyName || stored.malformed) {
        await deleteKeyByName(fetchFn, configEndpoint, adminId, apiVersion,
                              authToken, stored.friendlyName, log, budget);
    }

    let lastError;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        let minted;
        try {
            minted = await createKey(fetchFn, configEndpoint, adminId, apiVersion,
                                     authToken, log, budget);
        } catch (e) {
            if (e instanceof ReconcileDeadlineError) throw e;
            lastError = e;
            log(`MCP reconcile attempt ${attempt}/${maxAttempts} key-create failed: ${e.message}`);
            if (e.ambiguous) await deleteOwned("the create got no usable answer");
            continue;
        }
        // The key now exists in HighByte and we own it. If it cannot be
        // persisted, delete it before retrying or failing, so a Put/CreateSecret
        // failure never leaks a key slot.
        try {
            await storeKey(secrets, secretId, minted, log, budget);
        } catch (e) {
            lastError = new Error(`writing the MCP key to secret ${secretId} failed: ${e.message}`);
            log(`MCP reconcile attempt ${attempt}/${maxAttempts}: ${lastError.message}`);
            await deleteOwned("it could not be written to the secret");
            if (e instanceof ReconcileDeadlineError || budget.remaining() < MIN_REQUEST_MS) {
                throw lastError;  // no time for another attempt
            }
            continue;
        }
        const result = await verifyWithAvailabilityRetry(
            fetchFn, mcpEndpoint, minted.token, retryOpts);
        if (result.status === "ok") {
            log(`MCP credential reconciled on attempt ${attempt}`);
            return minted.token;
        }
        if (result.status !== "bad_key") {
            // The minted key is the one now in the secret; a non-auth problem
            // says nothing about it, so keep it and stop -- no delete, no mint.
            throw nonRotatingError(result, "newly minted");
        }
        // The minted key was rejected (bad_key): delete it before trying again
        // so failed attempts do not leak keys toward the per-user limit.
        lastError = new Error(`new MCP key was rejected (${result.detail})`);
        log(`MCP reconcile attempt ${attempt}/${maxAttempts}: ${lastError.message}`);
        await deleteOwned("the hub rejected it");
    }
    throw new Error(
        `MCP credential reconciliation failed after ${maxAttempts} attempts: `
        + (lastError ? lastError.message : "unknown"));
}
