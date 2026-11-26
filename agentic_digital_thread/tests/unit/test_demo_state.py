# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the demo seam runtime override + precedence (S4.4b).

The demo seam (``agents.demo``) gains a persisted runtime *override* that wins
over the ``ADT_DEMO`` env default; precedence is ``override (if set) → ADT_DEMO env
→ off``. These tests exercise that precedence, ``set_override`` (including the
``None`` revert-to-env case) and ``load_persisted`` with a duck-typed fake
persister — no AWS, no boto3, no network.
"""

import pytest

from agents import demo


@pytest.fixture(autouse=True)
def _reset_demo_state():
    """Reset the module-level override/persister around every test.

    ``set_override``/``configure``/``load_persisted`` mutate module globals; a
    hard reset before and after keeps tests order-independent.
    """
    demo._override = None
    demo._persister = None
    yield
    demo._override = None
    demo._persister = None


class _FakePersister:
    """Duck-typed stand-in (``get_flag``/``set_flag``); records writes."""

    def __init__(self, value=None):
        self._value = value
        self.set_calls = []

    def get_flag(self):
        return self._value

    def set_flag(self, value):
        self.set_calls.append(value)


# ---------------------------------------------------------------------------
# Precedence: override > ADT_DEMO env > off
# ---------------------------------------------------------------------------

def test_disabled_when_no_override_and_env_unset(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    assert demo.is_enabled() is False


def test_enabled_by_env_when_no_override(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    assert demo.is_enabled() is True


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "On"])
def test_env_truthy_values_enable(monkeypatch, truthy):
    monkeypatch.setenv("ADT_DEMO", truthy)
    assert demo.is_enabled() is True


def test_override_true_wins_over_unset_env(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    demo.set_override(True)
    assert demo.is_enabled() is True


def test_override_false_wins_over_truthy_env(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    demo.set_override(False)
    assert demo.is_enabled() is False


def test_override_none_reverts_to_env(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    demo.set_override(False)
    assert demo.is_enabled() is False
    # Clearing the override reverts to the env default.
    demo.set_override(None)
    assert demo.is_enabled() is True

    monkeypatch.delenv("ADT_DEMO", raising=False)
    assert demo.is_enabled() is False


# ---------------------------------------------------------------------------
# set_override write-through to the configured persister
# ---------------------------------------------------------------------------

def test_set_override_writes_through_to_persister():
    persister = _FakePersister()
    demo.configure(persister)
    demo.set_override(True)
    assert persister.set_calls == [True]
    demo.set_override(False)
    assert persister.set_calls == [True, False]


def test_set_override_none_does_not_write_through():
    persister = _FakePersister()
    demo.configure(persister)
    demo.set_override(None)
    # Clearing the override must not persist a bogus value.
    assert persister.set_calls == []


def test_set_override_without_persister_is_safe(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    # No persister configured (offline) — must not raise.
    demo.set_override(True)
    assert demo.is_enabled() is True


# ---------------------------------------------------------------------------
# load_persisted with a fake persister returning True / False / None
# ---------------------------------------------------------------------------

def test_load_persisted_true_sets_override(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    demo.configure(_FakePersister(value=True))
    demo.load_persisted()
    assert demo._override is True
    assert demo.is_enabled() is True


def test_load_persisted_false_sets_override(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    demo.configure(_FakePersister(value=False))
    demo.load_persisted()
    assert demo._override is False
    # Persisted False wins over the truthy env default.
    assert demo.is_enabled() is False


def test_load_persisted_none_leaves_override_unset(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    demo.configure(_FakePersister(value=None))
    demo.load_persisted()
    # No persisted value → no override → env default applies.
    assert demo._override is None
    assert demo.is_enabled() is True


def test_load_persisted_without_persister_is_noop(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    # No persister configured — load_persisted must be a safe no-op.
    demo.load_persisted()
    assert demo._override is None
    assert demo.is_enabled() is False
