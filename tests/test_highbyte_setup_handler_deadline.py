# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Full-handler deadline tests for the HighByte setup Lambda
(highbyte/lambda/highbyte-setup/index.mjs).

The setup custom resource runs in a 5-minute Lambda behind the CDK Provider
framework, which reports FAILED to CloudFormation when the handler REJECTS. If
instead the invocation is killed by the Lambda timeout, the outcome is decided
by the timeout, not by a caught error, and any key the run minted can be left
behind. So EVERY HighByte request and EVERY Secrets Manager call must be bound
by one absolute handler deadline, and the handler must reject with a clear
error while there is still time to respond.

The handler runs unmodified under node in a temp copy of the Lambda directory,
with a stub ``@aws-sdk/client-secrets-manager`` package and a FAKE CLOCK:
``Date.now``, ``setTimeout`` and ``AbortSignal.timeout`` are patched, so a
request costs its configured latency and one slower than its abort timeout
costs exactly the timeout and rejects. A request with NO timeout waits its
full latency -- which is how an uncapped call shows up as running past the
Lambda timeout. Runs under node (installed in root-suites / unit-lane CI);
fails, not skips, without it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv lists, internal inputs only
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = REPO_ROOT / "highbyte" / "lambda" / "highbyte-setup"
PROJECT_CONFIG = REPO_ROOT / "config" / "highbyte" / "intelligencehub-configuration.json"

#: stacks/highbyte_opcua_stack.py (pinned by test_setup_lambda_timeout_is_five_minutes).
LAMBDA_TIMEOUT_MS = 300_000
#: The handler must reject with at least this much invocation time left, so
#: the Provider framework receives the rejection and sends CFN FAILED.
RESPONSE_MARGIN_MS = 5_000
#: A request that never answers (well past the Lambda timeout).
HANG = 10_000_000

_SDK_STUB = r"""
export class GetSecretValueCommand { constructor(input) { this.input = input; } }
export class PutSecretValueCommand { constructor(input) { this.input = input; } }
export class CreateSecretCommand { constructor(input) { this.input = input; } }
export class SecretsManagerClient {
  constructor(config) { this.config = config || {}; globalThis.__smConfigs.push(this.config); }
  send(command, options) { return globalThis.__smSend(command, options, this.config); }
}
"""

_DRIVER = r"""
import { pathToFileURL } from 'node:url';
const args = JSON.parse(process.env.ADT_HANDLER_ARGS);

// ---- fake clock -------------------------------------------------------------
const clock = { t: 0 };
const realNow = Date.now;
Date.now = () => clock.t;
globalThis.setTimeout = (fn, ms = 0, ...a) => { clock.t += Math.max(0, ms); queueMicrotask(() => fn(...a)); return 0; };
AbortSignal.timeout = (ms) => { const s = new AbortController().signal; s.__ms = ms; return s; };
const logs = [];
console.log = (...a) => logs.push(a.map(String).join(' '));
console.error = (...a) => logs.push('ERR ' + a.map(String).join(' '));

function costOf(kind, signal) {
  const lat = (args.latency || {})[kind] || 0;
  const limit = (signal && signal.__ms !== undefined) ? signal.__ms : Infinity;
  if (lat > limit) {
    clock.t += limit;
    const e = new Error(`${kind} aborted after ${limit} ms`); e.name = 'TimeoutError'; throw e;
  }
  clock.t += lat;
}

// ---- fake HighByte hub -------------------------------------------------------
const hub = { password: args.configured ? 'stored-pw' : '', tokens: new Set(args.preValid || []),
              sessions: new Set(), created: [], deleted: [], keys: new Set(), seq: 0 };
function resp(status, body = {}, headers = {}) {
  const h = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { ok: status >= 200 && status < 300, status, statusText: String(status),
           headers: { get: (n) => h.get(String(n).toLowerCase()) ?? null },
           json: async () => body, text: async () => JSON.stringify(body) };
}
globalThis.fetch = async (url, opts = {}) => {
  const method = opts.method || 'GET';
  const body = opts.body ? JSON.parse(opts.body) : {};
  const path = url.replace(/^https?:\/\/[^/]+/, '');
  let kind;
  if (url === args.mcpEndpoint) kind = 'mcp:' + body.method;
  else if (path === '/ui/') kind = 'ready';
  else if (path === '/login') kind = 'login';
  else if (path === '/v1/user/administrator') kind = 'admin';
  else if (method === 'PUT' && path.startsWith('/v1/user/')) kind = 'password';
  else if (method === 'POST' && path.endsWith('/apiKey')) kind = 'keyCreate';
  else if (method === 'DELETE' && path.includes('/apiKey/')) kind = 'keyDelete';
  else if (path === '/v1/project/import') kind = 'import';
  else kind = 'other:' + path;
  costOf(kind, opts.signal);
  switch (kind) {
    case 'ready': return resp(clock.t >= (args.readyAt || 0) ? 200 : 503);
    case 'login': return body.password === hub.password
      ? resp(200, { success: true, token: 'admintok' }) : resp(200, { success: false, message: 'bad password' });
    case 'admin': return resp(200, { id: 'u1', username: 'administrator', versionId: '42', policyVersion: 1, roles: [] });
    case 'password': hub.password = body.password; return resp(200, {});
    case 'keyCreate': {
      hub.seq += 1; const t = 'key-' + hub.seq;
      hub.created.push(t); hub.tokens.add(t); hub.keys.add(body.friendlyName);
      return resp(200, { secret: t, storedKey: 'stored-' + hub.seq });
    }
    case 'keyDelete': { const n = decodeURIComponent(path.split('/apiKey/')[1]); hub.deleted.push(n); hub.keys.delete(n); return resp(204, {}); }
    case 'import': return resp(200, {});
    case 'mcp:initialize': {
      const tok = (opts.headers.Authorization || '').replace(/^Bearer /, '');
      if (!hub.tokens.has(tok)) return resp(401);
      const sid = 'sess-' + hub.sessions.size; hub.sessions.add(sid);
      return resp(200, { jsonrpc: '2.0', id: 1, result: {} }, { 'Mcp-Session-Id': sid });
    }
    case 'mcp:notifications/initialized': return resp(202);
    case 'mcp:tools/list': return hub.sessions.has(opts.headers['Mcp-Session-Id'])
      ? resp(200, { jsonrpc: '2.0', id: 2, result: { tools: [{ name: 't' }] } }) : resp(400);
    default: return resp(404);
  }
};

// ---- fake Secrets Manager ----------------------------------------------------
const store = Object.assign({
  'pw-secret': 'stored-pw',
  'aurora-secret': JSON.stringify({ host: 'erp', port: 5432, dbname: 'erp', username: 'u', password: 'p' }),
  'adt-secret': JSON.stringify({ password: 'p' }),
}, args.initialSecrets || {});
globalThis.__smConfigs = [];
globalThis.__smCalls = [];
globalThis.__smSend = async (command, options) => {
  const op = command.constructor.name.replace('Command', '');
  const id = command.input.SecretId || command.input.Name;
  globalThis.__smCalls.push({ op, id, hasAbortSignal: !!(options && options.abortSignal) });
  costOf(`sm:${op}:${id}`, options && options.abortSignal);
  if (op === 'GetSecretValue') {
    if (!(id in store)) { const e = new Error('not found'); e.name = 'ResourceNotFoundException'; throw e; }
    return { SecretString: store[id] };
  }
  if (op === 'PutSecretValue') {
    if (!(id in store)) { const e = new Error('not found'); e.name = 'ResourceNotFoundException'; throw e; }
    store[id] = command.input.SecretString; return {};
  }
  store[id] = command.input.SecretString; return {};   // CreateSecret
};

// ---- run the handler -----------------------------------------------------------
const m = await import(pathToFileURL(args.module).href);
const context = { getRemainingTimeInMillis: () => args.timeoutMs - clock.t };
const out = { threw: null };
try {
  await m.handler({ RequestType: 'Create' }, context);
} catch (e) {
  out.threw = e.message;
}
out.endMs = clock.t;
out.created = hub.created;
out.deleted = hub.deleted;
out.liveKeys = [...hub.keys];
out.mcpSecret = store['adt/conn/highbyte'] ? JSON.parse(store['adt/conn/highbyte']) : null;
out.smConfigs = globalThis.__smConfigs;
out.smCalls = globalThis.__smCalls;
out.logs = logs;
process.stdout.write('\n@@RESULT@@' + JSON.stringify(out) + '\n');
"""


def _run(tmp_path: Path, lambda_src: Path = LAMBDA_DIR, **scenario) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node not on PATH -- the root-suites/unit-lane CI jobs install nodejs")
    work = tmp_path / "lambda"
    work.mkdir()
    for f in lambda_src.glob("*.mjs"):
        shutil.copy(f, work / f.name)
    shutil.copy(PROJECT_CONFIG, work / "intelligencehub-configuration.json")
    sdk = work / "node_modules" / "@aws-sdk" / "client-secrets-manager"
    sdk.mkdir(parents=True)
    (sdk / "package.json").write_text(json.dumps(
        {"name": "@aws-sdk/client-secrets-manager", "type": "module", "main": "index.mjs"}))
    (sdk / "index.mjs").write_text(_SDK_STUB)
    mcp = "http://hub-alb:8885/mcp"
    args = {"module": str(work / "index.mjs"), "mcpEndpoint": mcp, "timeoutMs": LAMBDA_TIMEOUT_MS,
            **scenario}
    env = {"PATH": os.environ.get("PATH", ""), "ADT_HANDLER_ARGS": json.dumps(args),
           "SECRET_HIGHBYTE_PASSWORD": "pw-secret", "HIGHBYTE_ALB_ENDPOINT": "http://hub-alb:45245",
           "HIGHBYTE_ENDPOINT": "https://hub.example", "MCP_VERIFY_ENDPOINT": mcp,
           "MCP_TOKEN_SECRET": "adt/conn/highbyte", "OPC_UA_ENDPOINT": "opc.tcp://opc:26543",
           "CONFIG_FILE_PATH": str(work / "intelligencehub-configuration.json"),
           "AURORA_SECRET_ARN": "aurora-secret", "ADT_POSTGRES_HOST": "adt-db",
           "ADT_SECRET_ARN": "adt-secret", "AWS_REGION": "us-east-1"}
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        [node, "--input-type=module", "-e", _DRIVER], cwd=work,
        capture_output=True, text=True, timeout=120, env=env, check=False)
    marker = result.stdout.rfind("@@RESULT@@")
    assert marker >= 0, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    return json.loads(result.stdout[marker + len("@@RESULT@@"):])


def _assert_failed_in_time(out: dict, *words: str) -> None:
    assert out["threw"] is not None, "the handler must reject (Provider framework -> CFN FAILED)"
    assert out["endMs"] <= LAMBDA_TIMEOUT_MS - RESPONSE_MARGIN_MS, (
        f"handler settled at {out['endMs']} ms; the {LAMBDA_TIMEOUT_MS} ms Lambda timeout minus the "
        f"{RESPONSE_MARGIN_MS} ms response margin would kill it first")
    msg = out["threw"].lower()
    assert any(w in msg for w in words), out["threw"]


def _assert_no_orphaned_key(out: dict) -> None:
    """Every key the run created is either the one the secret names or deleted."""
    persisted = (out["mcpSecret"] or {}).get("token")
    for i, tok in enumerate(out["created"]):
        assert tok == persisted or out["deleted"], (tok, out["deleted"], persisted)
    assert len(out["liveKeys"]) <= (1 if persisted else 0), out["liveKeys"]


def test_healthy_run_succeeds_well_inside_the_timeout(tmp_path):
    out = _run(tmp_path)
    assert out["threw"] is None, (out["threw"], out["logs"][-5:])
    assert out["mcpSecret"]["token"] == out["created"][0]
    assert out["endMs"] < 60_000


@pytest.mark.parametrize("scenario,words", [
    ({"configured": True, "latency": {"login": HANG}}, ("login", "time budget", "abort")),
    ({"configured": True, "latency": {"admin": HANG}}, ("admin", "time budget", "abort")),
    ({"configured": False, "latency": {"password": HANG}}, ("password", "time budget", "abort")),
    ({"configured": True, "latency": {"sm:GetSecretValue:pw-secret": HANG}},
     ("password", "secret", "time budget", "abort")),
    ({"configured": True, "latency": {"sm:GetSecretValue:aurora-secret": HANG}},
     ("aurora", "secret", "time budget", "abort")),
    ({"configured": True, "latency": {"sm:GetSecretValue:adt-secret": HANG}},
     ("secret", "time budget", "abort")),
    ({"configured": True, "latency": {"import": HANG}}, ("import", "time budget", "abort")),
], ids=["slow-login", "slow-admin-get", "slow-password-put", "slow-password-secret",
        "slow-aurora-secret-read", "slow-adt-secret-read", "slow-import"])
def test_every_slow_call_ends_in_a_thrown_error_before_the_timeout(tmp_path, scenario, words):
    out = _run(tmp_path, **scenario)
    _assert_failed_in_time(out, *words)
    _assert_no_orphaned_key(out)


def test_slow_secret_write_after_mint_deletes_the_owned_key_in_time(tmp_path):
    """No stored secret -> a key is minted -> the secret write never answers:
    the handler rejects in time and the minted key is deleted."""
    out = _run(tmp_path, configured=True,
               latency={"sm:GetSecretValue:adt/conn/highbyte": 0,
                        "sm:PutSecretValue:adt/conn/highbyte": HANG,
                        "sm:CreateSecret:adt/conn/highbyte": HANG})
    _assert_failed_in_time(out, "secret", "time budget", "abort")
    assert out["created"] and out["deleted"] == ["mcpkey"] * len(out["created"]), out
    assert out["liveKeys"] == []


def test_secret_write_that_eats_the_reconcile_deadline_still_cleans_up(tmp_path):
    """The hub becomes ready late and every config call is slow, so the secret
    write for the minted key starts a few seconds before the MCP-reconcile
    deadline and times out AT it. The cleanup DELETE must still run (it is
    bounded by the handler deadline, not the spent reconcile one)."""
    out = _run(tmp_path, configured=False, readyAt=170_000,
               latency={"sm:GetSecretValue:pw-secret": 9_000, "login": 9_000, "admin": 9_000,
                        "password": 9_000, "sm:GetSecretValue:adt/conn/highbyte": 9_000,
                        "keyCreate": 9_000, "sm:CreateSecret:adt/conn/highbyte": HANG,
                        "sm:PutSecretValue:adt/conn/highbyte": 0})
    _assert_failed_in_time(out, "secret", "time budget", "abort")
    assert out["created"], out
    assert out["deleted"] == ["mcpkey"], (out["deleted"], [l for l in out["logs"] if "key" in l.lower()][-6:])
    assert out["liveKeys"] == []


def test_secrets_manager_client_config_is_runtime_sdk_safe(tmp_path):
    """The asset ships no node_modules, so the Lambda runtime's own AWS SDK
    runs and its minor version varies by runtime and Region. The client config
    therefore carries only options every SDK v3 accepts: a small maxAttempts
    and NO requestHandler object. Each call is bounded by its own per-call
    abortSignal from the handler deadline instead."""
    out = _run(tmp_path)
    assert out["smConfigs"], "no SecretsManagerClient was constructed"
    for cfg in out["smConfigs"]:
        assert "requestHandler" not in cfg, cfg
        assert set(cfg) <= {"maxAttempts"}, cfg
        assert 1 <= cfg.get("maxAttempts", 0) <= 3, cfg


def test_every_secrets_manager_call_carries_an_abort_signal(tmp_path):
    """With no client-level timeout, the per-call abortSignal is the bound."""
    out = _run(tmp_path)
    assert out["smCalls"] and all(c["hasAbortSignal"] for c in out["smCalls"]), out["smCalls"]
