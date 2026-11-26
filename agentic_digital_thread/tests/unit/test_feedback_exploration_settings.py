# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Feedback-exploration runtime toggle seam (agents/feedback_exploration_settings.py).

Offline — no AWS, no network; the persister is an injected fake. Each pin
names the mutation it kills (plan A1–A8, A13):

* A1 env-kill beats an admin ON flag;
* A2/A3 admin flag ON/OFF governs when no kill;
* A4 nothing set anywhere ⇒ OFF (source ``default``);
* A5 unrecognized/set-empty env values KILL (fail-closed);
* A6 a truthy env value does NOT enable (the admin flag is the single ON);
* A7 the 60 s TTL cache serves reads then refreshes (counting persister);
* A8 a persister write failure propagates AND leaves the cached state;
* A13 unconfigured module defaults OFF without crashing; ``set_enabled``
  with no persister raises (no phantom writes).
"""
from __future__ import annotations

import pytest

import agents.feedback_exploration_settings as fxs


class _FlagPersister:
    """Counting duck-typed persister (get_flag/set_flag)."""

    def __init__(self, value=None, read_error=None, write_error=None):
        self.value = value
        self.read_error = read_error
        self.write_error = write_error
        self.get_calls = 0
        self.set_calls: list[bool] = []

    def get_flag(self):
        self.get_calls += 1
        if self.read_error is not None:
            raise self.read_error
        return self.value

    def set_flag(self, value: bool) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.set_calls.append(value)
        self.value = value


@pytest.fixture(autouse=True)
def _clean_seam(monkeypatch):
    monkeypatch.delenv("FEEDBACK_EXPLORATION_ENABLED", raising=False)
    fxs._reset()
    yield
    fxs._reset()


# ── precedence: env-kill > admin flag > default OFF ─────────────────────────

def test_env_kill_beats_admin_on(monkeypatch):
    """A1: an active env kill wins over a persisted admin ON."""
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", "0")
    fxs.configure(_FlagPersister(True))
    assert fxs.env_kill_active() is True
    assert fxs.effective() == (False, "env-kill")
    assert fxs.exploration_enabled() is False


def test_admin_flag_on_enables():
    """A2: persisted admin ON enables (no kill)."""
    fxs.configure(_FlagPersister(True))
    assert fxs.effective() == (True, "admin-flag")
    assert fxs.exploration_enabled() is True


def test_admin_flag_off_disables():
    """A3: persisted admin OFF is an explicit admin-flag OFF, not default."""
    fxs.configure(_FlagPersister(False))
    assert fxs.effective() == (False, "admin-flag")


def test_both_unset_defaults_off():
    """A4: no env, no persisted flag ⇒ OFF with source ``default``."""
    fxs.configure(_FlagPersister(None))
    assert fxs.effective() == (False, "default")


@pytest.mark.parametrize("value", ["0", "false", "", "  ", "banana", "2", "TRUE_"])
def test_unrecognized_env_value_kills(monkeypatch, value):
    """A5: ANY set non-truthy value is a hard kill — fail-closed."""
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", value)
    fxs.configure(_FlagPersister(True))
    assert fxs.effective() == (False, "env-kill")


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_truthy_env_does_not_enable(monkeypatch, value):
    """A6: a truthy env value is NOT a kill but also NOT an enable —
    without an admin flag the default OFF applies."""
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", value)
    fxs.configure(_FlagPersister(None))
    assert fxs.env_kill_active() is False
    assert fxs.effective() == (False, "default")


def test_env_unset_is_not_a_kill(monkeypatch):
    monkeypatch.delenv("FEEDBACK_EXPLORATION_ENABLED", raising=False)
    assert fxs.env_kill_active() is False


# ── TTL cache ────────────────────────────────────────────────────────────────

def test_ttl_caches_then_refreshes(monkeypatch):
    """A7: reads inside the TTL hit the cache (one persister call); advancing
    the clock past 60 s re-reads and picks up a changed value."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(fxs, "_now", lambda: clock["t"])
    persister = _FlagPersister(True)
    fxs.configure(persister)

    assert fxs.exploration_enabled() is True
    assert fxs.exploration_enabled() is True
    assert persister.get_calls == 1          # second read served from cache

    persister.value = False                  # flips behind the cache
    clock["t"] += 59.0
    assert fxs.exploration_enabled() is True  # still cached
    assert persister.get_calls == 1

    clock["t"] += 2.0                         # past the 60 s TTL
    assert fxs.exploration_enabled() is False
    assert persister.get_calls == 2


def test_failed_read_is_cached_as_default_off(monkeypatch):
    """Boot-resilience: a raising persister read yields default OFF and the
    failure is cached for the TTL (no per-request SSM retry storm)."""
    clock = {"t": 0.0}
    monkeypatch.setattr(fxs, "_now", lambda: clock["t"])
    persister = _FlagPersister(read_error=RuntimeError("ssm down"))
    fxs.configure(persister)
    assert fxs.effective() == (False, "default")
    assert fxs.effective() == (False, "default")
    assert persister.get_calls == 1


# ── writes ───────────────────────────────────────────────────────────────────

def test_set_enabled_writes_through_and_updates_cache():
    persister = _FlagPersister(None)
    fxs.configure(persister)
    assert fxs.effective() == (False, "default")
    fxs.set_enabled(True)
    assert persister.set_calls == [True]
    assert fxs.effective() == (True, "admin-flag")  # cache updated, no re-read


def test_set_enabled_write_failure_propagates_and_state_unchanged():
    """A8: the persister exception PROPAGATES (fail-loud) and the cached
    effective state is untouched — no phantom toggle on a failed write."""
    persister = _FlagPersister(True, write_error=RuntimeError("ssm write failed"))
    fxs.configure(persister)
    assert fxs.exploration_enabled() is True
    with pytest.raises(RuntimeError, match="ssm write failed"):
        fxs.set_enabled(False)
    assert fxs.exploration_enabled() is True


# ── unconfigured (local mode) ───────────────────────────────────────────────

def test_unconfigured_defaults_off_no_crash():
    """A13: no persister (ADT_SETTINGS_STORE=none) ⇒ default OFF, no error."""
    assert fxs.effective() == (False, "default")
    assert fxs.exploration_enabled() is False


def test_set_enabled_without_persister_raises():
    """A13: an unpersistable write raises rather than pretending it took."""
    with pytest.raises(RuntimeError, match="no settings persister"):
        fxs.set_enabled(True)


def test_configure_drops_stale_cache():
    """configure() must invalidate a previous persister's cached value."""
    fxs.configure(_FlagPersister(True))
    assert fxs.exploration_enabled() is True
    fxs.configure(_FlagPersister(False))
    assert fxs.exploration_enabled() is False


# ── lazy production default (the context_interview.state._store precedent) ──

def test_default_persister_honors_settings_store_optout(monkeypatch):
    """A13: ADT_SETTINGS_STORE=none ⇒ NO persister — local mode never reads
    or writes a deployed account's parameter (server.lifespan's contract,
    resolved lazily here instead of in lifespan)."""
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    assert fxs._default_persister() is None


def test_default_persister_is_the_ssm_store_on_the_pinned_param(monkeypatch):
    """Deployed default: the SsmSettingsStore over the pinned parameter
    (construction is offline — the boto3 client is lazy inside the store)."""
    monkeypatch.delenv("ADT_SETTINGS_STORE", raising=False)
    store = fxs._default_persister()
    assert type(store).__name__ == "SsmSettingsStore"
    assert store.param_name == "/adt/feedback-exploration-enabled"


def test_unset_slot_resolves_lazily_once(monkeypatch):
    """Production path: an _UNSET slot resolves through _default_persister on
    first read and memoizes — killing the resolve leaves the toggle dead."""
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    calls = {"n": 0}
    real = fxs._default_persister

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(fxs, "_default_persister", counting)
    monkeypatch.setattr(fxs, "_persister", fxs._UNSET)
    assert fxs.effective() == (False, "default")
    assert fxs.effective() == (False, "default")
    assert calls["n"] == 1                     # memoized, not per-read
