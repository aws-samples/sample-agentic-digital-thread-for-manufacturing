# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the live-updates runtime toggle seam (``agents.live_settings``).

Mirrors ``test_demo_state`` (the proven runtime-override pattern) but with the
OPPOSITE default: live updates default **ON** (``CARD_LIVE=1`` is the deployed
state), so the toggle only PAUSES the already-wired background auto-updates to
save cost. Precedence is ``override (if set) → LIVE_UPDATES_ENABLED env →
default ON``. No AWS, no boto3, no network — persistence is a duck-typed fake.
"""

import pytest

from agents import live_settings


@pytest.fixture(autouse=True)
def _reset_live_state():
    """Reset the module-level override/persister slots around every test.

    ``_interval_persister = None`` (not ``_UNSET``) keeps tests hermetic — the
    lazy production default is never armed here; interval tests inject a fake
    through ``configure_interval``."""
    live_settings._override = None
    live_settings._persister = None
    live_settings._interval_override = None
    live_settings._interval_persister = None
    live_settings._interval_loaded = False
    yield
    live_settings._override = None
    live_settings._persister = None
    live_settings._interval_override = None
    live_settings._interval_persister = None
    live_settings._interval_loaded = False


class _FakePersister:
    """Duck-typed stand-in (``get_flag``/``set_flag``); records writes."""

    def __init__(self, value=None):
        self._value = value
        self.set_calls = []

    def get_flag(self):
        return self._value

    def set_flag(self, value):
        self.set_calls.append(value)


class _FakeIntervalPersister:
    """Duck-typed interval stand-in (``get_interval``/``set_interval``)."""

    def __init__(self, interval=None):
        self._interval = interval
        self.set_interval_calls = []

    def get_interval(self):
        return self._interval

    def set_interval(self, value):
        self.set_interval_calls.append(value)


# ---------------------------------------------------------------------------
# Precedence: override > LIVE_UPDATES_ENABLED env > default ON
# ---------------------------------------------------------------------------

def test_default_enabled_when_no_override_and_env_unset(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    # Default is ON (unlike the demo seam) — CARD_LIVE=1 is the deployed state.
    assert live_settings.live_updates_enabled() is True


def test_env_can_disable_when_no_override(monkeypatch):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", "0")
    assert live_settings.live_updates_enabled() is False


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "FALSE", "Off"])
def test_env_falsy_values_disable(monkeypatch, falsy):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", falsy)
    assert live_settings.live_updates_enabled() is False


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "On"])
def test_env_truthy_values_enable(monkeypatch, truthy):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", truthy)
    assert live_settings.live_updates_enabled() is True


def test_env_unrecognized_value_falls_back_to_default_on(monkeypatch):
    # Fail-soft: a misconfigured value must never silently stop live updates.
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", "maybe")
    assert live_settings.live_updates_enabled() is True


def test_override_false_wins_over_default_on(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    live_settings.set_live_updates(False)
    assert live_settings.live_updates_enabled() is False


def test_override_true_wins_over_falsy_env(monkeypatch):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", "0")
    live_settings.set_live_updates(True)
    assert live_settings.live_updates_enabled() is True


def test_override_none_reverts_to_env_then_default(monkeypatch):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", "0")
    live_settings.set_live_updates(True)
    assert live_settings.live_updates_enabled() is True
    # Clearing the override reverts to the env value.
    live_settings.set_live_updates(None)
    assert live_settings.live_updates_enabled() is False
    # And with the env unset, back to the default ON.
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    assert live_settings.live_updates_enabled() is True


# ---------------------------------------------------------------------------
# set_live_updates write-through to the configured persister
# ---------------------------------------------------------------------------

def test_set_live_updates_writes_through_to_persister():
    persister = _FakePersister()
    live_settings.configure(persister)
    live_settings.set_live_updates(False)
    assert persister.set_calls == [False]
    live_settings.set_live_updates(True)
    assert persister.set_calls == [False, True]


def test_set_live_updates_none_does_not_write_through():
    persister = _FakePersister()
    live_settings.configure(persister)
    live_settings.set_live_updates(None)
    assert persister.set_calls == []


def test_set_live_updates_without_persister_is_safe(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    live_settings.set_live_updates(False)
    assert live_settings.live_updates_enabled() is False


def test_set_live_updates_propagates_persister_error():
    class _BoomPersister:
        def get_flag(self):
            return None

        def set_flag(self, value):
            raise RuntimeError("SSM write failed")

    live_settings.configure(_BoomPersister())
    with pytest.raises(RuntimeError):
        live_settings.set_live_updates(False)


# ---------------------------------------------------------------------------
# load_persisted with a fake persister returning True / False / None
# ---------------------------------------------------------------------------

def test_load_persisted_false_sets_override(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    live_settings.configure(_FakePersister(value=False))
    live_settings.load_persisted()
    assert live_settings._override is False
    # Persisted PAUSE survives a restart even with the default ON.
    assert live_settings.live_updates_enabled() is False


def test_load_persisted_true_sets_override(monkeypatch):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", "0")
    live_settings.configure(_FakePersister(value=True))
    live_settings.load_persisted()
    assert live_settings._override is True
    # Persisted True wins over a falsy env default.
    assert live_settings.live_updates_enabled() is True


def test_load_persisted_none_leaves_override_unset(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    live_settings.configure(_FakePersister(value=None))
    live_settings.load_persisted()
    assert live_settings._override is None
    assert live_settings.live_updates_enabled() is True


def test_load_persisted_without_persister_is_noop(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    live_settings.load_persisted()
    assert live_settings._override is None
    assert live_settings.live_updates_enabled() is True


# ---------------------------------------------------------------------------
# Snapshot interval: precedence override → DASHBOARD_SNAPSHOT_INTERVAL env →
# default 120; bounds 30..3600 (invalid → ignored/None); fail-soft reads.
# ---------------------------------------------------------------------------

def test_interval_default_when_no_override_and_env_unset(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    assert live_settings.snapshot_interval_override() is None
    assert live_settings.snapshot_interval_seconds() == 120.0


def test_interval_env_wins_when_no_override(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SNAPSHOT_INTERVAL", "45")
    assert live_settings.snapshot_interval_seconds() == 45.0


def test_interval_env_non_numeric_falls_back_to_default(monkeypatch):
    # Fail-soft: a misconfigured env value keeps the default cadence.
    monkeypatch.setenv("DASHBOARD_SNAPSHOT_INTERVAL", "soon")
    assert live_settings.snapshot_interval_seconds() == 120.0


def test_interval_override_wins_over_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SNAPSHOT_INTERVAL", "45")
    live_settings.set_snapshot_interval(300)
    assert live_settings.snapshot_interval_override() == 300
    assert live_settings.snapshot_interval_seconds() == 300.0


def test_interval_override_none_reverts_to_env_then_default(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SNAPSHOT_INTERVAL", "45")
    live_settings.set_snapshot_interval(300)
    live_settings.set_snapshot_interval(None)
    assert live_settings.snapshot_interval_seconds() == 45.0
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    assert live_settings.snapshot_interval_seconds() == 120.0


@pytest.mark.parametrize("valid", [30, 3600, 120, "300", " 900 "])
def test_coerce_interval_accepts_in_bounds(valid):
    assert live_settings.coerce_interval(valid) is not None


@pytest.mark.parametrize("invalid", [29, 3601, 0, -5, "soon", "", None, 12.5, True, False])
def test_coerce_interval_rejects_out_of_bounds_and_non_numeric(invalid):
    assert live_settings.coerce_interval(invalid) is None


@pytest.mark.parametrize("invalid", [29, 3601, "soon"])
def test_set_interval_invalid_is_ignored_keeps_current(monkeypatch, invalid):
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    live_settings.set_snapshot_interval(300)
    live_settings.set_snapshot_interval(invalid)
    # The invalid ask changed NOTHING — the prior override stands.
    assert live_settings.snapshot_interval_override() == 300


def test_set_interval_writes_through_to_persister():
    persister = _FakeIntervalPersister()
    live_settings.configure_interval(persister)
    live_settings.set_snapshot_interval(300)
    assert persister.set_interval_calls == [300]


def test_set_interval_invalid_never_writes_through():
    persister = _FakeIntervalPersister()
    live_settings.configure_interval(persister)
    live_settings.set_snapshot_interval(5)
    live_settings.set_snapshot_interval("soon")
    live_settings.set_snapshot_interval(None)  # clear is in-memory only
    assert persister.set_interval_calls == []


def test_set_interval_without_persister_is_safe(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    live_settings.set_snapshot_interval(60)
    assert live_settings.snapshot_interval_seconds() == 60.0


def test_set_interval_propagates_persister_error():
    class _BoomIntervalPersister:
        def get_interval(self):
            return None

        def set_interval(self, value):
            raise RuntimeError("SSM write failed")

    live_settings.configure_interval(_BoomIntervalPersister())
    with pytest.raises(RuntimeError):
        live_settings.set_snapshot_interval(300)
    # No phantom override — the write failed BEFORE the cache update.
    assert live_settings.snapshot_interval_override() is None


def test_persisted_interval_loads_lazily_on_first_read(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    live_settings.configure_interval(_FakeIntervalPersister(interval=900))
    assert live_settings.snapshot_interval_override() == 900
    assert live_settings.snapshot_interval_seconds() == 900.0


@pytest.mark.parametrize("bad", [None, 5, 999999, "soon"])
def test_persisted_interval_invalid_leaves_override_unset(monkeypatch, bad):
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)
    live_settings.configure_interval(_FakeIntervalPersister(interval=bad))
    assert live_settings.snapshot_interval_override() is None
    assert live_settings.snapshot_interval_seconds() == 120.0


def test_persisted_interval_read_error_is_fail_soft(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SNAPSHOT_INTERVAL", raising=False)

    class _BoomRead:
        def get_interval(self):
            raise RuntimeError("SSM read failed")

        def set_interval(self, value):
            pass

    live_settings.configure_interval(_BoomRead())
    # Boot-resilient: a broken read never wedges the cadence — default applies.
    assert live_settings.snapshot_interval_seconds() == 120.0


def test_default_interval_persister_honors_settings_store_optout(monkeypatch):
    """ADT_SETTINGS_STORE=none ⇒ the lazy production default resolves to None
    (local mode never reads a deployed account's parameter)."""
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    live_settings._interval_persister = live_settings._UNSET
    assert live_settings._resolve_interval_persister() is None


def test_dashboard_snapshot_interval_consults_override(monkeypatch):
    """dashboard._snapshot_interval_seconds delegates: override wins over the
    DASHBOARD_SNAPSHOT_INTERVAL env default."""
    from agents import dashboard

    monkeypatch.setenv("DASHBOARD_SNAPSHOT_INTERVAL", "45")
    assert dashboard._snapshot_interval_seconds() == 45.0
    live_settings.set_snapshot_interval(600)
    assert dashboard._snapshot_interval_seconds() == 600.0
