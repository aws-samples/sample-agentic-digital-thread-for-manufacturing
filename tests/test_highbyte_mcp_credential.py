# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The HighByte setup Lambda's MCP credential reconciliation, run under node
against highbyte/lambda/highbyte-setup/mcp-credential.mjs with injected fake
fetch + Secrets Manager facades (no AWS, no network).

MCP credential reconciliation is a REQUIRED postcondition of the setup custom
resource: the secret adt/conn/highbyte must
exist AND its token must authenticate against the hub, or the custom resource
fails. These tests cover: a missing secret gets a key; a first-run key-creation
failure then a rerun repairs it; a stored-but-invalid token is rotated and the
prior key deleted; a valid stored token mints nothing (idempotent); and
reconciliation throws when no key ever authenticates.

This file also pins:
  * verification must perform the stateful Streamable-HTTP
    handshake -- initialize, capture Mcp-Session-Id, notifications/initialized,
    then tools/list with that session -- and accept ONLY a real tools-list
    JSON-RPC result. The fake hub below is a state machine: a bare tools/list
    before initialize returns HTTP 400 (protocol error), exactly as 4.4.2 does.
    A bearer that never initializes is NOT a valid key. mcpVerify classifies
    outcomes as ok / bad_key (401/403) / unavailable (transport/5xx/timeout) /
    protocol_error (anything else).
  * rotation deletes by the key's friendlyName (the 4.4.2
    delete route is DELETE /v1/user/{id}/{ver}/apiKey/{friendlyName}); a
    transient hub outage (5xx) that recovers mints NOTHING -- availability is
    retried with bounded backoff and only a real auth rejection rotates; and a
    minted-but-unusable key is deleted before the next attempt.

Runs under node (installed in the root-suites + unit-lane CI jobs); fails -- not
skips -- if node is absent in those jobs so a green lane proves it actually ran.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = REPO_ROOT / "highbyte" / "lambda" / "highbyte-setup"
MODULE = LAMBDA_DIR / "mcp-credential.mjs"

#: A node driver with an in-memory fake hub + Secrets Manager. It reads a
#: scenario from ADT_MCP_ARGS (JSON) and prints one JSON result describing what
#: happened: the returned token, how many keys were created, the final secret,
#: whether reconcileMcpCredential threw, and -- for the mcpVerify unit tests --
#: the classification of a single verify call.
#:
#: The fake MCP endpoint is a STATE MACHINE that mirrors HighByte 4.4.2's
#: stateful Streamable HTTP transport:
#:   * a `tools/list` with no established session -> HTTP 400 (protocol error);
#:   * `initialize` with a bad/absent bearer      -> HTTP 401;
#:   * `initialize` with a valid bearer           -> 200 + a fresh Mcp-Session-Id;
#:   * `tools/list` with a known session          -> 200 + a real tools-list
#:                                                   JSON-RPC result;
#:   * `tools/list` with an unknown session        -> HTTP 400 (protocol error).
#: A `transientOutage` counter makes the first N MCP requests return 503, after
#: which the hub recovers -- the "hub restarting" case, which must never be
#: mistaken for a bad key.
_DRIVER = r"""
import { pathToFileURL } from 'node:url';
const args = JSON.parse(process.env.ADT_MCP_ARGS);
const m = await import(pathToFileURL(args.module).href);

const hub = {
  validTokens: new Set(),                 // bearers the hub accepts at initialize
  createFails: args.createFails || 0,     // first N createAndStore POSTs return 500
  created: [],                            // bearer tokens created
  createdNames: [],                       // friendlyNames created (parallel to created)
  deleted: [],                            // friendlyNames deleted
  keyLimitRejectAfter: args.keyLimitRejectAfter ?? Infinity,
  transientOutage: args.transientOutage || 0,  // first N MCP requests return 503
  sessions: new Set(),                    // established Mcp-Session-Id values
};
for (const t of (args.preValid || [])) hub.validTokens.add(t);
let createCalls = 0;
let seq = 0;
let sessionSeq = 0;
let mcpCalls = 0;
const clock = { t: args.startMs || 0 };
const naps = [];
const logLines = [];

function makeResponse(status, bodyObj, headers = {}) {
  const h = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => h.get(String(name).toLowerCase()) ?? null },
    json: async () => bodyObj,
    text: async () => JSON.stringify(bodyObj),
  };
}

async function fetchFn(url, opts) {
  // Fake clock: every request costs its configured
  // latency; a request slower than the caller's timeout (opts.timeoutMs, or
  // the 10 s AbortSignal.timeout the pre-deadline code always used) costs the
  // timeout and rejects with a TimeoutError, as AbortSignal.timeout would.
  const body0 = opts.body ? (() => { try { return JSON.parse(opts.body); } catch { return {}; } })() : {};
  const kind = url === args.mcpEndpoint ? body0.method : (opts.method || 'GET') + ' ' + url.split('/').pop();
  const lat = (args.latency || {})[kind] || 0;
  const limit = opts.timeoutMs ?? 10000;
  if (lat > limit) {
    clock.t += limit;
    const e = new Error('The operation was aborted due to timeout');
    e.name = 'TimeoutError';
    throw e;
  }
  clock.t += lat;
  return hubFetch(url, opts);
}

async function hubFetch(url, opts) {
  // MCP endpoint: a stateful Streamable-HTTP state machine.
  if (url === args.mcpEndpoint) {
    mcpCalls += 1;
    if (mcpCalls <= hub.transientOutage) return makeResponse(503, {});  // hub restarting
    const auth = (opts.headers && (opts.headers.Authorization || opts.headers.authorization)) || '';
    const token = auth.replace(/^Bearer /, '');
    const body = opts.body ? JSON.parse(opts.body) : {};
    const sessionId = (opts.headers && (opts.headers['Mcp-Session-Id'] || opts.headers['mcp-session-id'])) || '';
    // Persistent non-auth MCP fault: the hub answers this
    // method with this status on every call -- a protocol problem, not a bad key.
    if (args.mcpFault && body.method === args.mcpFault.method) {
      return makeResponse(args.mcpFault.status, {});
    }
    if (args.malformedToolsList && body.method === 'tools/list' && hub.sessions.has(sessionId)) {
      return makeResponse(200, { jsonrpc: '2.0', id: body.id, result: { notTools: true } });
    }
    if (body.method === 'initialize') {
      if (!hub.validTokens.has(token)) return makeResponse(401, {});
      sessionSeq += 1;
      const sid = 'sess-' + sessionSeq;
      hub.sessions.add(sid);
      return makeResponse(200, { jsonrpc: '2.0', id: body.id, result: { capabilities: {} } },
                          { 'Mcp-Session-Id': sid });
    }
    if (body.method === 'notifications/initialized') {
      return makeResponse(202, {});  // notification: accepted, no body
    }
    if (body.method === 'tools/list') {
      if (!sessionId || !hub.sessions.has(sessionId)) return makeResponse(400, {});  // no session
      return makeResponse(200, { jsonrpc: '2.0', id: body.id, result: { tools: [{ name: 't' }] } });
    }
    return makeResponse(400, {});
  }
  // Create API key: POST /v1/user/{id}/{ver}/apiKey
  if (opts.method === 'POST' && url.endsWith('/apiKey')) {
    createCalls += 1;
    if (createCalls <= hub.createFails) return makeResponse(500, {});
    if (hub.created.length >= hub.keyLimitRejectAfter) return makeResponse(429, {});
    seq += 1;
    const bearer = 'key-' + seq;
    const friendlyName = (opts.body && JSON.parse(opts.body).friendlyName) || 'mcpkey';
    hub.created.push(bearer);
    hub.createdNames.push(friendlyName);
    hub.validTokens.add(bearer);  // a freshly created key authenticates (unless the scenario forbids)
    if ((args.mintBadKeys || 0) >= hub.created.length) hub.validTokens.delete(bearer);
    return makeResponse(200, { secret: bearer, storedKey: 'stored-' + seq, friendlyName });
  }
  // Delete API key: DELETE .../apiKey/{friendlyName}
  if (opts.method === 'DELETE') {
    const name = decodeURIComponent(url.split('/apiKey/')[1] || '');
    hub.deleted.push(name);
    return makeResponse(200, {});
  }
  return makeResponse(404, {});
}

const store = Object.assign({}, args.initialSecrets || {});
// The fake Secrets Manager facade enforces IAM. If
// 'secretsmanager:GetSecretValue' is not in args.grantedActions, get() denies
// with an AccessDeniedException -- exactly what the setup role hit when the
// scoped conn/highbyte statement lacked GetSecretValue -- so an un-granted
// read is a test failure, not a silent "secret absent".
const grantedActions = args.grantedActions
  || ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue',
      'secretsmanager:CreateSecret', 'secretsmanager:DescribeSecret'];
let putCalls = 0;
const secrets = {
  get: async (id) => {
    if (!grantedActions.includes('secretsmanager:GetSecretValue')) {
      const e = new Error('is not authorized to perform: secretsmanager:GetSecretValue');
      e.name = 'AccessDeniedException';
      throw e;
    }
    // An injected read failure (throttling, timeout, ...).
    if (args.getError) { const e = new Error(args.getError.message || 'read failed'); e.name = args.getError.name; throw e; }
    if (!(id in store)) { const e = new Error('no'); e.name = 'ResourceNotFoundException'; throw e; }
    return store[id];
  },
  // The first args.putFails secret writes fail (throttled).
  put: async (id, value) => {
    putCalls += 1;
    if (putCalls <= (args.putFails || 0)) {
      const e = new Error('Rate exceeded'); e.name = 'ThrottlingException'; throw e;
    }
    store[id] = value;
  },
};

const out = { created: null, deleted: null, finalSecret: null, threw: null };

// mcpVerify-only mode: classify a single verify call and stop.
if (args.verifyOnly) {
  for (const t of (args.establishedBad || [])) { /* no-op, kept for symmetry */ }
  try {
    const result = await m.mcpVerify(fetchFn, args.mcpEndpoint, args.verifyToken, () => {});
    out.verify = result;
  } catch (e) {
    out.verifyThrew = e.message;
  }
  out.mcpCalls = mcpCalls;
  console.log(JSON.stringify(out));
} else {
  // The deadline a 5-minute setup Lambda hands the reconcile when it starts at
  // t=0 (index.mjs: reconcileDeadline(start, context.getRemainingTimeInMillis())).
  out.deadline = (args.lambdaRemainingMs && m.reconcileDeadline)
    ? m.reconcileDeadline(0, args.lambdaRemainingMs) : null;
  out.reserveMs = m.SETUP_RESERVE_MS ?? null;
  try {
    const token = await m.reconcileMcpCredential({
      fetchFn, secrets,
      configEndpoint: 'http://hub:45245',
      mcpEndpoint: args.mcpEndpoint,
      adminId: 'u1', apiVersion: '42', authToken: 'admintok',
      secretId: args.secretId, maxAttempts: args.maxAttempts || 3,
      // Fake clock: sleeps advance it instantly (no real wait).
      sleep: async (ms) => { naps.push(ms); clock.t += ms; },
      now: () => clock.t,
      deadline: out.deadline ?? undefined,
      backoffBaseMs: args.backoffBaseMs ?? 0,
      log: (m) => { logLines.push(String(m)); },
    });
    out.token = token;
  } catch (e) {
    out.threw = e.message;
  }
  out.clock = clock.t;
  out.log = logLines;
  out.naps = naps;
  out.created = hub.created;
  out.createdNames = hub.createdNames;
  out.deleted = hub.deleted;
  out.createCalls = createCalls;
  out.finalSecret = store[args.secretId] ? JSON.parse(store[args.secretId]) : null;
  console.log(JSON.stringify(out));
}
"""


def _run(**scenario) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node not on PATH -- the root-suites/unit-lane CI jobs install nodejs, "
                    "so this must run, not skip (the credential contract needs a real CI run)")
    env = {**os.environ, "ADT_MCP_ARGS": json.dumps({
        "module": str(MODULE), "mcpEndpoint": "http://hub:8885/mcp",
        "secretId": "adt/conn/highbyte", **scenario})}
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        [node, "--input-type=module", "-e", _DRIVER],
        capture_output=True, text=True, timeout=60, env=env, check=True)
    return json.loads(result.stdout)


# --------------------------------------------------------------------------- #
# The handshake + classification contract of mcpVerify.
# --------------------------------------------------------------------------- #

def test_verify_does_the_full_handshake_for_a_valid_key():
    """A valid key verifies ONLY by initialize -> session -> tools/list; the
    classification is 'ok'. (A bare tools/list would get HTTP 400 from the fake,
    so a pass proves the handshake happened.)"""
    out = _run(verifyOnly=True, verifyToken="good", preValid=["good"])
    assert out.get("verifyThrew") is None  # mcpVerify never throws
    assert out["verify"]["status"] == "ok"


def test_verify_bare_tools_list_before_initialize_is_protocol_error_not_bad_key():
    """A verifier that sends tools/list first reads HTTP 400 as 'bad key'.
    mcpVerify must do the handshake; a hub that
    rejects an un-initialized tools/list with 400 is a protocol error, never a
    bad key. (Here the key IS valid, so a handshake-doing verifier returns
    'ok' -- a first-request-tools/list verifier would see the fake's 400 and
    say the key is bad.)"""
    out = _run(verifyOnly=True, verifyToken="good", preValid=["good"])
    assert out["verify"]["status"] == "ok"  # NOT bad_key


def test_verify_bad_key_is_classified_bad_key():
    """A bearer the hub rejects at initialize (401) classifies as bad_key."""
    out = _run(verifyOnly=True, verifyToken="nope", preValid=["other"])
    assert out["verify"]["status"] == "bad_key"


def test_verify_transient_5xx_is_unavailable_not_bad_key():
    """A 503 at initialize (hub restarting) classifies as 'unavailable', never
    'bad_key' -- so the caller retries rather than rotating a good key."""
    out = _run(verifyOnly=True, verifyToken="good", preValid=["good"], transientOutage=1)
    assert out["verify"]["status"] == "unavailable"


# --------------------------------------------------------------------------- #
# Idempotency + required-postcondition behaviour.
# --------------------------------------------------------------------------- #

def test_missing_secret_gets_a_working_key():
    """An already-configured hub whose adt/conn/highbyte secret is absent gets
    one created, and the secret ends with a token that authenticates."""
    out = _run(initialSecrets={})
    assert out["threw"] is None
    assert out["createCalls"] == 1 and len(out["created"]) == 1
    assert out["finalSecret"]["token"] == out["token"]
    assert out["deleted"] == []  # nothing to delete on first provision


def test_first_run_key_failure_then_rerun_repairs():
    """The hub rejects the first key-creation POST (HTTP 500); the bounded
    retry inside the same run mints a second key that authenticates, so the
    custom resource still ends with a working credential."""
    out = _run(initialSecrets={}, createFails=1, maxAttempts=3)
    assert out["threw"] is None
    assert out["createCalls"] == 2  # one failure, one success
    assert out["finalSecret"]["token"] == out["token"]


def test_stale_token_is_rotated_and_prior_key_deleted_by_friendly_name():
    """A stored token that no longer authenticates is replaced, and the key the
    secret previously named is deleted first -- BY ITS friendlyName (the 4.4.2
    delete route), so repeated runs don't leak keys."""
    out = _run(initialSecrets={
        "adt/conn/highbyte": json.dumps(
            {"token": "dead-token", "storedKey": "stored-old", "friendlyName": "mcpkey"})},
        preValid=[])
    assert out["threw"] is None
    assert out["deleted"] == ["mcpkey"]            # deletes by friendlyName, not storedKey
    assert out["finalSecret"]["token"] == out["token"] != "dead-token"
    assert out["finalSecret"]["friendlyName"]      # the new secret persists a friendlyName


def test_valid_stored_token_mints_nothing():
    """Idempotent: when the stored token already authenticates (full handshake),
    no key is created and no key is deleted."""
    out = _run(initialSecrets={
        "adt/conn/highbyte": json.dumps({"token": "prevalid", "storedKey": "stored-pre",
                                         "friendlyName": "mcpkey"})},
        preValid=["prevalid"])
    assert out["threw"] is None
    assert out["created"] == [] and out["deleted"] == []
    assert out["token"] == "prevalid"


def test_persistent_failure_throws():
    """When key creation keeps failing, reconciliation throws after the bounded
    attempts so the custom resource fails rather than reporting a broken key."""
    out = _run(initialSecrets={}, createFails=99, maxAttempts=3)
    assert out["threw"] is not None and "after 3 attempts" in out["threw"]
    assert out["createCalls"] == 3


# --------------------------------------------------------------------------- #
# Outage retries mint nothing; minted-bad keys are cleaned up.
# --------------------------------------------------------------------------- #

def test_transient_outage_that_recovers_mints_no_key():
    """A valid stored token with the hub briefly unavailable (503 on the first
    verify) must be retried, NOT rotated: once the hub recovers the stored token
    verifies and the run mints nothing and deletes nothing."""
    out = _run(initialSecrets={
        "adt/conn/highbyte": json.dumps({"token": "good", "storedKey": "stored-pre",
                                         "friendlyName": "mcpkey"})},
        preValid=["good"], transientOutage=2)
    assert out["threw"] is None
    assert out["created"] == [] and out["deleted"] == []
    assert out["token"] == "good"


def test_minted_but_unusable_key_is_deleted_before_the_next_attempt():
    """When a freshly minted key does not authenticate, it is deleted (by its
    friendlyName) before another is minted, so failed attempts don't leak keys.
    Here the first minted key is forced invalid; the second works."""
    out = _run(initialSecrets={}, mintBadKeys=1, maxAttempts=3)
    assert out["threw"] is None
    assert len(out["created"]) == 2                       # first bad, second good
    assert out["deleted"] == ["mcpkey"]                   # the bad first key cleaned up
    assert out["finalSecret"]["token"] == out["token"]


# --------------------------------------------------------------------------- #
# The handler path can READ an existing valid secret and mint
# nothing -- but ONLY when the role is granted GetSecretValue.
# --------------------------------------------------------------------------- #

def test_existing_valid_secret_is_read_and_no_key_is_minted_when_get_is_granted():
    """With GetSecretValue granted (the fix), an existing valid secret is read,
    its token verifies, and reconciliation mints and deletes nothing."""
    out = _run(initialSecrets={
        "adt/conn/highbyte": json.dumps({"token": "good", "storedKey": "stored-pre",
                                         "friendlyName": "mcpkey"})},
        preValid=["good"],
        grantedActions=["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
                        "secretsmanager:CreateSecret"])
    assert out["threw"] is None
    assert out["created"] == [] and out["deleted"] == []
    assert out["token"] == "good"


@pytest.mark.parametrize("scenario", [
    {"grantedActions": ["secretsmanager:PutSecretValue", "secretsmanager:CreateSecret"]},
    {"getError": {"name": "ThrottlingException", "message": "Rate exceeded"}},
    {"getError": {"name": "TimeoutError", "message": "socket timed out"}},
    {"getError": {"name": "InternalServiceError", "message": "boom"}},
], ids=["access-denied", "throttling", "timeout", "sdk-error"])
def test_secret_read_errors_rethrow_with_zero_key_mutations(scenario):
    """Only ResourceNotFoundException means the secret is
    absent. AccessDenied, throttling, timeouts and other SDK errors say nothing
    about the stored key, so the reconcile rethrows naming the read failure with
    zero key creates and zero deletes (never swallowed as 'absent', which
    would mint a replacement)."""
    out = _run(initialSecrets=dict(_VALID_STORED), preValid=["good"], **scenario)
    assert out["createCalls"] == 0 and out["deleted"] == [], out
    assert out["threw"] is not None and "read" in out["threw"].lower(), out["threw"]
    assert out["finalSecret"]["token"] == "good"


def test_absent_secret_is_minted():
    """ResourceNotFoundException is the one 'absent' case: one key minted, nothing deleted."""
    out = _run(initialSecrets={})
    assert out["threw"] is None and out["createCalls"] == 1 and out["deleted"] == []


@pytest.mark.parametrize("raw", ["not json at all", json.dumps({"storedKey": "s", "friendlyName": "mcpkey"}),
                                 json.dumps({"token": ""}), json.dumps(["token"])],
                         ids=["unparseable", "no-token", "empty-token", "not-an-object"])
def test_malformed_secret_is_an_explicit_logged_repair(raw):
    """A secret that exists but is unparseable or has no token is unusable by
    ADT too, so it is repaired by rotation -- with a log line naming why."""
    out = _run(initialSecrets={"adt/conn/highbyte": raw}, captureLog=True)
    assert out["threw"] is None, out["threw"]
    assert out["createCalls"] == 1
    assert out["finalSecret"]["token"] == out["token"]
    assert any("repair" in line.lower() and "adt/conn/highbyte" in line for line in out["log"]), out["log"]


# --------------------------------------------------------------------------- #
# ONLY bad_key rotates. protocol_error / unavailable
# throw without deleting or minting -- for the stored key and a minted one.
# --------------------------------------------------------------------------- #

_VALID_STORED = {"adt/conn/highbyte": json.dumps(
    {"token": "good", "storedKey": "stored-pre", "friendlyName": "mcpkey"})}


@pytest.mark.parametrize("fault", [
    {"mcpFault": {"method": "tools/list", "status": 400}},
    {"mcpFault": {"method": "initialize", "status": 429}},
    {"malformedToolsList": True},
], ids=["tools-list-400", "initialize-429", "malformed-tools-list"])
def test_protocol_error_on_a_valid_stored_token_never_deletes_or_mints(fault):
    """A valid stored token that hits a non-auth MCP problem must not be
    rotated: zero DELETEs, zero key creates, the secret is unchanged, and the
    reconcile fails with a protocol error naming the cause."""
    out = _run(initialSecrets=dict(_VALID_STORED), preValid=["good"], **fault)
    assert out["threw"] is not None and "protocol" in out["threw"].lower(), out["threw"]
    assert out["deleted"] == [] and out["createCalls"] == 0
    assert out["finalSecret"]["token"] == "good"


def test_protocol_error_on_a_freshly_minted_key_neither_deletes_nor_mints_again():
    """No secret yet (bad_key -> one mint); the minted key then hits a protocol
    error: the reconcile throws without deleting that key or minting another."""
    out = _run(initialSecrets={}, mcpFault={"method": "tools/list", "status": 400})
    assert out["threw"] is not None and "protocol" in out["threw"].lower(), out["threw"]
    assert out["createCalls"] == 1 and out["deleted"] == []


# --------------------------------------------------------------------------- #
# One absolute wall-clock deadline for the whole
# reconcile -- requests AND sleeps -- that leaves the setup handler time to
# import the project and report inside its 5-minute Lambda timeout.
# --------------------------------------------------------------------------- #

#: The setup Lambda's configured timeout (stacks/highbyte_opcua_stack.py,
#: pinned by test_highbyte_stack.py::test_setup_lambda_timeout_is_five_minutes).
LAMBDA_TIMEOUT_MS = 300_000
#: What the handler needs AFTER the reconcile: project fetch + import and the
#: return to the CloudFormation provider framework. 30 s is the floor the test
#: holds the module's own reserve to.
MIN_POST_RECONCILE_MS = 30_000


def test_slow_half_responsive_hub_fails_inside_the_lambda_timeout():
    """The slow half-responsive path: initialize answers after 9 s, but the
    initialized notification and tools/list hang past every timeout, so each
    verify is 'unavailable' after ~29 s. Pre-reconcile setup (hub wait, login,
    Secrets Manager) is taken to have used 60 s. The reconcile must stop at its
    deadline with a clear error -- never mint, never delete -- early enough that
    the handler still has its reserve for the project import and the provider
    response before the 5-minute Lambda timeout. A sleep-only 120 s budget
    would let seven ~29 s verifies plus 120 s of naps run to ~383 s."""
    out = _run(initialSecrets=dict(_VALID_STORED), preValid=["good"],
               startMs=60_000, lambdaRemainingMs=LAMBDA_TIMEOUT_MS, backoffBaseMs=2000,
               latency={"initialize": 9_000, "notifications/initialized": 60_000,
                        "tools/list": 60_000})
    assert out["threw"] is not None, "the reconcile must fail on a hub that never answers"
    assert "unavailable" in out["threw"] or "time budget" in out["threw"], out["threw"]
    assert out["created"] == [] and out["deleted"] == []
    assert out["clock"] <= LAMBDA_TIMEOUT_MS - MIN_POST_RECONCILE_MS, (
        f"reconcile ended at {out['clock']} ms, past the {LAMBDA_TIMEOUT_MS} ms Lambda "
        f"timeout minus the {MIN_POST_RECONCILE_MS} ms the handler still needs")
    assert out["deadline"] is not None, "mcp-credential.mjs exports no reconcileDeadline()"
    assert out["clock"] <= out["deadline"], (out["clock"], out["deadline"])
    assert out["reserveMs"] >= MIN_POST_RECONCILE_MS


def test_every_request_and_nap_is_capped_to_the_time_left():
    """With only ~12 s left at the start, the reconcile neither sleeps past the
    deadline nor starts a request it cannot finish: it ends at or before the
    deadline and reports running out of time."""
    out = _run(initialSecrets=dict(_VALID_STORED), preValid=["good"],
               startMs=LAMBDA_TIMEOUT_MS - 72_000, lambdaRemainingMs=LAMBDA_TIMEOUT_MS,
               backoffBaseMs=8000,
               latency={"initialize": 9_000, "notifications/initialized": 60_000,
                        "tools/list": 60_000})
    assert out["threw"] is not None
    assert out["clock"] <= LAMBDA_TIMEOUT_MS - MIN_POST_RECONCILE_MS, out["clock"]
    assert out["deadline"] is not None and out["clock"] <= out["deadline"], (out["clock"], out["deadline"])
    assert all(n >= 0 for n in out["naps"])
    assert out["created"] == [] and out["deleted"] == []


def test_handler_hands_the_reconcile_its_lambda_deadline():
    """index.mjs derives the reconcile deadline from the invocation's own
    remaining time, so the bound tracks the configured Lambda timeout."""
    src = (LAMBDA_DIR / "index.mjs").read_text(encoding="utf-8")
    assert "getRemainingTimeInMillis" in src
    assert "reconcileDeadline(" in src and "deadline:" in src


# --------------------------------------------------------------------------- #
# Key create and secret write are separate steps;
# every minted key that never reached the secret is deleted.
# --------------------------------------------------------------------------- #

def test_transient_secret_write_failure_deletes_the_unpersisted_key_then_recovers():
    """The first Put/CreateSecret fails (throttled): the key minted for it is
    deleted before the retry, the retry mints and stores a working key."""
    out = _run(initialSecrets={}, putFails=1, maxAttempts=3)
    assert out["threw"] is None, out["threw"]
    assert out["createCalls"] == 2
    assert out["deleted"] == ["mcpkey"]          # exactly the one unpersisted key
    assert out["finalSecret"]["token"] == out["token"] == out["created"][1]


def test_persistent_secret_write_failure_deletes_every_unpersisted_key():
    """Every secret write fails: each minted key is deleted, and the reconcile
    fails naming the secret write -- no key is left orphaned in HighByte."""
    out = _run(initialSecrets={}, putFails=99, maxAttempts=3)
    assert out["deleted"] == ["mcpkey"] * 3     # one delete per unpersisted key
    assert out["threw"] is not None and "secret" in out["threw"].lower(), out["threw"]
    assert out["createCalls"] == 3
    assert out["finalSecret"] is None
