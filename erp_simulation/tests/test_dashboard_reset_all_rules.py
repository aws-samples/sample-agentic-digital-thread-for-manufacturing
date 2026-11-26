# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reset All Data re-enables the simulator rules it paused, on every exit.

``ERPDashboard.reset_all_data`` (erp_simulation/streamlit_dashboard.py)
disables each ENABLED ``erp-simulation-*`` EventBridge rule, then runs the
cleanup, the balance reset, the config reset, the db-init reset and ten
simulation cycles. A db-init reset that fails RAISES (lambda-db-init/handler.py,
so a failed Create fails its stack), so it reaches the dashboard as a
``FunctionError``, one of the step's early returns. Every exit, those early
returns included, must re-enable the rules this call disabled, or a failed
Reset All leaves the simulator stopped. A rule that was already disabled
stays disabled.

The destructive invokes (the cleanup, the balance reset and the db-init full
reset) are sent once, through a client that waits longer than db-init's own
timeout; db-init re-applies the optional plant itself, so the dashboard runs
the same steps whether the plant is on or not.

Streamlit, pandas, plotly and boto3 are stubbed; no network, no database:

    python -m pytest erp_simulation/tests/test_dashboard_reset_all_rules.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

_DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "streamlit_dashboard.py"
_RULE = "erp-simulation-schedule"
# What the Lambda Python runtime returns for db-init's DatabaseInitError.
_DB_INIT_RAISED = {
    "errorMessage": "ERP database initialization failed during seed_data: UniqueViolation: duplicate key",
    "errorType": "DatabaseInitError", "requestId": "r", "stackTrace": ["  File \"/var/task/handler.py\""],
}


class _State(dict):
    """``st.session_state``: a dict with attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _Box:
    """``st.status()``: records each call made on it."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))

    def texts(self, name):
        return [str(args[0]) for called, args, _ in self.calls if called == name and args]


class _FakeSt:
    def __init__(self):
        self.session_state = _State()
        self.boxes = []

    def status(self, *args, **kwargs):
        box = _Box()
        self.boxes.append(box)
        return box

    def dialog(self, *args, **kwargs):
        return lambda fn: fn

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture(scope="module")
def dash():
    patch = pytest.MonkeyPatch()
    for name in ("pandas", "plotly", "plotly.graph_objects", "plotly.express"):
        patch.setitem(sys.modules, name, types.ModuleType(name))
    patch.setitem(sys.modules, "streamlit", _FakeSt())
    spec = importlib.util.spec_from_file_location("_reset_all_streamlit_dashboard", _DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patch.undo()
    return module


class _Payload:
    def __init__(self, body):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw


class _Lambda:
    """``invoke`` answers per (function, action); ``answers`` overrides one
    with (FunctionError or None, payload). ``configs`` records, per invoke,
    the botocore config of the client it was sent with (None: the defaults)."""

    def __init__(self, answers):
        self.answers, self.invoked, self.configs = answers, [], []

    def bound(self, config):
        """The client ``session.client('lambda', config=config)`` returns:
        this recorder, noting ``config`` on each invoke."""
        recorder = self

        class _Bound:
            def invoke(self, FunctionName, InvocationType, Payload):
                body = json.loads(Payload)
                recorder.configs.append(((FunctionName, body.get("action") or body.get("RequestType")),
                                         config))
                return recorder.invoke(FunctionName, InvocationType, Payload)
        return _Bound()

    def invoke(self, FunctionName, InvocationType, Payload):
        body = json.loads(Payload)
        key = (FunctionName, body.get("action") or body.get("RequestType"))
        self.invoked.append(key)
        error, payload = self.answers.get(key, (None, {"statusCode": 200, "body": "{}"}))
        response = {"StatusCode": 200, "Payload": _Payload(payload)}
        if error:
            response["FunctionError"] = error
        return response


class _Events:
    def __init__(self, rules):
        self.rules = dict(rules)       # name -> state
        self.disabled, self.enabled = [], []

    def list_rules(self, NamePrefix):
        return {"Rules": [{"Name": n, "State": s} for n, s in self.rules.items()
                          if n.startswith(NamePrefix)]}

    def disable_rule(self, Name):
        self.disabled.append(Name)
        self.rules[Name] = "DISABLED"

    def enable_rule(self, Name):
        self.enabled.append(Name)
        self.rules[Name] = "ENABLED"


class _Cursor:
    def __init__(self, raises):
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._raises:
            raise RuntimeError("simulation_config unreachable")


class _LockConn:
    """The dashboard's own session for the Tulip sender lock: answers each
    pg_try_advisory_lock from ``answers`` (the last one repeats) and records
    every statement, in the order the reset ran them, into ``log``."""

    def __init__(self, answers, log):
        self.answers, self.log, self.tries, self.closed = list(answers), log, 0, False
        self.autocommit = False

    def cursor(self):
        conn = self

        class _LockCursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.log.append(("sql", " ".join(sql.split()), params))
                self._row = None
                if "pg_try_advisory_lock" in sql:
                    self._row = (conn.answers[min(conn.tries, len(conn.answers) - 1)],)
                    conn.tries += 1

            def fetchone(self):
                return self._row
        return _LockCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True
        self.log.append(("closed",))


class _DB:
    def __init__(self, raises=False, lock_answers=(True,), log=None):
        self._raises = raises
        self.lock_answers, self.log, self.locks = lock_answers, log if log is not None else [], []

    def dedicated_connection(self, *, idle_in_transaction_timeout_s, application_name):
        conn = _LockConn(self.lock_answers, self.log)
        self.locks.append((conn, idle_in_transaction_timeout_s, application_name))
        return conn

    def connection(self):
        raises = self._raises

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return _Cursor(raises)

            def commit(self):
                pass
        return _Conn()


def _fake_boto3(monkeypatch, events, lambdas):
    """boto3 whose Session hands out ``events`` and ``lambdas`` (bound to the
    config each client was created with)."""
    def client(name, region_name=None, config=None):
        return lambdas.bound(config) if name == "lambda" else events
    boto3 = types.ModuleType("boto3")
    boto3.Session = lambda **_kw: types.SimpleNamespace(client=client)
    monkeypatch.setitem(sys.modules, "boto3", boto3)


def _reset_all(dash, monkeypatch, rules, answers=None, db_raises=False, lambda_client=None, db=None):
    """Run reset_all_data against the fakes: (result, events, lambda, box)."""
    st = _FakeSt()
    monkeypatch.setattr(dash, "st", st)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    events, lambdas = _Events(rules), lambda_client or _Lambda(answers or {})
    _fake_boto3(monkeypatch, events, lambdas)
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = db or _DB(raises=db_raises)
    result = board.reset_all_data()
    (box,) = st.boxes
    return result, events, lambdas, box


# --------------------------------------------------------------------------
# The Tulip sender: its rule is paused like the simulator's, and its lock is
# taken before anything destructive runs
# --------------------------------------------------------------------------

_SENDER_RULE = "erp-simulation-tulip-sender"
_SENDER_LOCK = int.from_bytes(b"TULIPSND", "big", signed=True)


class _OrderedLambda(_Lambda):
    def __init__(self, log):
        super().__init__({})
        self.log = log

    def invoke(self, FunctionName, InvocationType, Payload):
        self.log.append(("invoke", FunctionName))
        return super().invoke(FunctionName, InvocationType, Payload)


def test_reset_all_pauses_the_sender_rule_and_holds_its_lock_through_the_destructive_steps(dash, monkeypatch):
    log = []
    db = _DB(log=log)
    result, events, _lambdas, _box = _reset_all(
        dash, monkeypatch, {_RULE: "ENABLED", _SENDER_RULE: "ENABLED"},
        lambda_client=_OrderedLambda(log), db=db)
    assert result is True
    assert sorted(events.disabled) == sorted([_RULE, _SENDER_RULE])
    assert sorted(events.enabled) == sorted([_RULE, _SENDER_RULE])
    ((conn, idle_timeout, name),) = db.locks
    assert conn.autocommit is True and name == "erp-dashboard-reset-all" and idle_timeout > 0
    kinds = [entry[0] if entry[0] != "sql" else ("lock" if "pg_try_advisory_lock" in entry[1] else "sql")
             for entry in log]
    first_invoke = kinds.index("invoke")
    assert "lock" in kinds[:first_invoke]                         # the lock before the first destructive step
    assert [e for e in log if e[0] == "sql" and "pg_try_advisory_lock" in e[1]][-1][2] == (_SENDER_LOCK,)
    assert log[-1] == ("closed",) and ("closed",) not in log[:first_invoke]   # held to the end


def test_reset_all_waits_out_a_running_sender_cycle(dash, monkeypatch):
    db = _DB(lock_answers=(False, False, True))
    result, _events, lambdas, _box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"}, db=db)
    assert result is True and db.locks[0][0].tries == 3
    assert ("erp-cleanup-old-data", "cleanup") in lambdas.invoked


def test_reset_all_refuses_while_a_sender_cycle_keeps_the_lock(dash, monkeypatch):
    db = _DB(lock_answers=(False,))
    monkeypatch.setattr(dash.ERPDashboard, "SENDER_LOCK_WAIT_SECONDS", 0)
    result, events, lambdas, box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED", _SENDER_RULE: "ENABLED"},
                                              db=db)
    assert result is False
    assert lambdas.invoked == []                                  # nothing destructive ran
    assert any("Tulip sender" in text for text in box.texts("error")), box.calls
    assert sorted(events.enabled) == sorted([_RULE, _SENDER_RULE])
    assert db.locks[0][0].closed


_SERVER_ERROR = {"statusCode": 500, "body": json.dumps({"error": "deadlock detected"})}
_EARLY_EXITS = [
    pytest.param({("erp-database-init", "Update"): ("Unhandled", _DB_INIT_RAISED)}, False,
                 "Seed data Lambda crashed", id="db-init-raised"),
    pytest.param({("erp-database-init", "Update"): (None, _SERVER_ERROR)}, False,
                 "Seed data failed", id="db-init-500"),
    pytest.param({("erp-cleanup-old-data", "cleanup"): ("Unhandled", {"errorMessage": "x"})}, False,
                 "Cleanup Lambda crashed", id="cleanup-raised"),
    pytest.param({("erp-cleanup-old-data", "cleanup"): (None, _SERVER_ERROR)}, False,
                 "Cleanup failed", id="cleanup-500"),
    pytest.param({("erp-cleanup-old-data", "reset_balances"): ("Unhandled", {"errorMessage": "x"})},
                 False, "Reset balances Lambda crashed", id="reset-balances-raised"),
    pytest.param({("erp-cleanup-old-data", "reset_balances"): (None, _SERVER_ERROR)}, False,
                 "Reset balances failed", id="reset-balances-500"),
    pytest.param({}, True, "Reset failed", id="config-reset-exception"),
]


@pytest.mark.parametrize("answers, db_raises, shown", _EARLY_EXITS)
def test_a_failed_reset_re_enables_the_rule_it_paused(dash, monkeypatch, answers, db_raises, shown):
    """The rule was ENABLED; a step fails: the error is shown, the reset
    reports failure, nothing after the step runs, and the rule is enabled
    again, once."""
    result, events, lambdas, box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"}, answers, db_raises)
    assert result is False
    assert any(shown in text for text in box.texts("error")), box.calls
    assert ("erp-simulation-orchestrator", "single_cycle") not in lambdas.invoked
    assert events.disabled == [_RULE]
    assert events.enabled == [_RULE] and events.rules[_RULE] == "ENABLED"


def test_a_failed_db_init_reset_shows_its_message(dash, monkeypatch):
    """The raised DatabaseInitError's message reaches the error box."""
    answers = {("erp-database-init", "Update"): ("Unhandled", _DB_INIT_RAISED)}
    _result, _events, _lambdas, box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"}, answers)
    assert any(_DB_INIT_RAISED["errorMessage"] in text for text in box.texts("error"))


def test_a_successful_reset_re_enables_the_rule_once(dash, monkeypatch):
    """Pinned (green before and after): success runs every step and enables
    the paused rule again, once."""
    result, events, lambdas, _box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"})
    assert result is True
    assert lambdas.invoked.count(("erp-simulation-orchestrator", "single_cycle")) == 10
    assert events.disabled == [_RULE] and events.enabled == [_RULE]


class _BusyThenRan(_Lambda):
    """The simulation Lambda answers ``simulator: busy`` ``busy`` times (a
    scheduled cycle still running when the reset paused the rule), then runs."""

    def __init__(self, busy):
        super().__init__({})
        self.busy = busy

    def invoke(self, FunctionName, InvocationType, Payload):
        response = super().invoke(FunctionName, InvocationType, Payload)
        if FunctionName == "erp-simulation-orchestrator" and self.busy:
            self.busy -= 1
            response["Payload"] = _Payload({"statusCode": 200,
                                            "body": json.dumps({"simulator": "busy"})})
        return response


def test_a_busy_warmup_cycle_is_waited_out_not_counted(dash, monkeypatch):
    lambdas = _BusyThenRan(busy=2)
    result, _events, invoked, box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"},
                                               lambda_client=lambdas)
    assert result is True
    assert invoked.invoked.count(("erp-simulation-orchestrator", "single_cycle")) == 12
    assert sum("Cycle" in t and "complete" in t for t in box.texts("write")) == 10
    assert any("is still running; waiting for it to finish" in t for t in box.texts("info"))


@pytest.mark.parametrize("answers", [
    pytest.param({}, id="success"),
    pytest.param({("erp-database-init", "Update"): ("Unhandled", _DB_INIT_RAISED)}, id="db-init-raised"),
])
def test_a_rule_that_was_already_disabled_stays_disabled(dash, monkeypatch, answers):
    _result, events, _lambdas, _box = _reset_all(dash, monkeypatch, {_RULE: "DISABLED"}, answers)
    assert events.disabled == [] and events.enabled == []
    assert events.rules[_RULE] == "DISABLED"


@pytest.mark.parametrize("answers", [
    pytest.param({}, id="success"),
    pytest.param({("erp-database-init", "Update"): ("Unhandled", _DB_INIT_RAISED)}, id="db-init-raised"),
])
def test_only_the_rules_this_reset_paused_are_enabled_again(dash, monkeypatch, answers):
    """Two simulator rules, one ENABLED, one disabled by hand: the reset
    pauses and re-enables the first and leaves the second off."""
    rules = {_RULE: "ENABLED", "erp-simulation-stopped-by-hand": "DISABLED"}
    _result, events, _lambdas, _box = _reset_all(dash, monkeypatch, rules, answers)
    assert events.disabled == [_RULE] and events.enabled == [_RULE]
    assert events.rules["erp-simulation-stopped-by-hand"] == "DISABLED"


def test_a_rule_that_cannot_be_re_enabled_does_not_change_the_result(dash, monkeypatch):
    """enable_rule failing is logged; the successful reset still reports
    success."""
    def refuse(self, Name):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(_Events, "enable_rule", refuse)
    result, events, _lambdas, _box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"})
    assert result is True and events.disabled == [_RULE]


# --------------------------------------------------------------------------
# The destructive invokes are sent once and wait out db-init
# --------------------------------------------------------------------------

#: db-init's own timeout (stacks/erp_database_stack.py; the synth test in
#: tests/test_system_db_readers_stack.py pins the dashboard's wait above the
#: deployed value).
_DB_INIT_TIMEOUT_SECONDS = 600
_DESTRUCTIVE = [("erp-cleanup-old-data", "cleanup"), ("erp-cleanup-old-data", "reset_balances"),
                ("erp-database-init", "Update")]


def test_the_destructive_invokes_wait_out_db_init_and_are_sent_once(dash, monkeypatch):
    """The cleanup, the balance reset and the db-init full reset (which runs
    the plant re-apply and the login re-grants before it answers) go through
    a client that waits longer than db-init's own timeout and makes one
    attempt. botocore's defaults wait 60 s and, with legacy retries, send a
    timed-out invoke again: a second full reset while the first still runs."""
    _result, _events, lambdas, _box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"})
    sent = dict(lambdas.configs[:3])
    assert list(sent) == _DESTRUCTIVE
    for key, config in sent.items():
        assert config is not None, f"{key} was sent with botocore's default timeout and retries"
        assert config.read_timeout > _DB_INIT_TIMEOUT_SECONDS, (key, config.read_timeout)
        assert config.retries == {"mode": "standard", "total_max_attempts": 1}, (key, config.retries)
    assert config.read_timeout == dash.RESET_INVOKE_READ_TIMEOUT_SECONDS


def test_the_warm_up_cycles_keep_the_default_client(dash, monkeypatch):
    """A cycle is not destructive (a second one sent while the first runs
    answers busy and is waited out), so the cycles keep botocore's retries
    for a throttle."""
    _result, _events, lambdas, _box = _reset_all(dash, monkeypatch, {_RULE: "ENABLED"})
    cycles = [config for key, config in lambdas.configs if key[0] == "erp-simulation-orchestrator"]
    assert len(cycles) == 10 and set(cycles) == {None}


# --------------------------------------------------------------------------
# The optional fifth plant: db-init re-applies it during its reset
# --------------------------------------------------------------------------

class _PlantDB:
    """A database whose sites query answers the Tulip plant's state
    (``active``: True, False or None for no row); every other statement is
    accepted, and the Tulip sender lock is free."""

    def __init__(self, active):
        self.active = active

    def dedicated_connection(self, *, idle_in_transaction_timeout_s, application_name):
        return _LockConn((True,), [])

    def connection(self):
        active = self.active

        class _PlantCursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                self._row = (active,) if "FROM sites" in sql and active is not None else None

            def fetchone(self):
                return self._row

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return _PlantCursor()

            def commit(self):
                pass
        return _Conn()


def _reset_with_plant(dash, monkeypatch, active, answers=None, lambdas=None):
    st = _FakeSt()
    monkeypatch.setattr(dash, "st", st)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    events, lambdas = _Events({_RULE: "ENABLED"}), lambdas or _Lambda(answers or {})
    _fake_boto3(monkeypatch, events, lambdas)
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = _PlantDB(active)
    result = board.reset_all_data()
    (box,) = st.boxes
    return result, events, lambdas, box


_TODAY = [("erp-cleanup-old-data", "cleanup"), ("erp-cleanup-old-data", "reset_balances"),
          ("erp-database-init", "Update")] + [("erp-simulation-orchestrator", "single_cycle")] * 10


def test_the_plant_is_left_to_the_db_init_reset(dash, monkeypatch):
    """db-init re-applies the optional plant during its own reset, before it
    re-grants the logins on the plant's views, so the dashboard runs exactly
    the usual steps whatever the plant's state: no plant row, an inactive
    plant, an active one."""
    for active in (None, False, True):
        result, events, lambdas, _box = _reset_with_plant(dash, monkeypatch, active)
        assert result is True, active
        assert lambdas.invoked == _TODAY, (active, lambdas.invoked)
        assert events.enabled == [_RULE]


def test_a_db_init_step_that_fails_fails_the_reset_and_re_enables_the_rule(dash, monkeypatch):
    """Pinned (green before and after): a step db-init runs after its reset
    (the plant re-apply, a login re-grant) that fails reaches the dashboard
    as db-init's FunctionError: Reset All fails with that message, runs no
    cycle, and enables the paused rule again."""
    failed = {"errorType": "ResetFollowUpError", "errorMessage":
              "ERP database reset and reseeded, but the optional fifth plant was not restored "
              "(erp-tulip-site): Unhandled: TulipPlantError: boom"}
    answers = {("erp-database-init", "Update"): ("Unhandled", failed)}
    result, events, lambdas, box = _reset_with_plant(dash, monkeypatch, True, answers)
    assert result is False
    assert any(failed["errorMessage"] in text for text in box.texts("error"))
    assert ("erp-simulation-orchestrator", "single_cycle") not in lambdas.invoked
    assert events.enabled == [_RULE]
