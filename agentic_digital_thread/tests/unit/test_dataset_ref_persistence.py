# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 4 — cap-time persistence of the withheld series (system_agent hook).

The ADDITIVE ``_persist_withheld_series`` hook stores the raw VQT series
under the freshly-minted ``tel-<uuid12>`` handle at the moment
``_summarize_vqt_series`` withholds it. Pins:

- the hook fires once per capped series with the EXACT minted handle and the
  ORIGINAL series (the redemption route serves what the model never saw);
- failure-swallow HONESTY: a hook/store failure logs a warning and the tool
  payload is BYTE-IDENTICAL to the success case — ``_cap_history_payload``'s
  tool-facing contract is unchanged (its pinned tests in
  ``test_cap_history_payload.py`` stay un-edited and green);
- short series (at/below cap) are passed through untouched and NEVER
  persisted;
- CLIENT-ONLY grep pin (claim-check policy): the ONLY production
  reader of ``dataset_store.get_dataset`` is the admin-gated redemption
  route, and the ONLY production writer of ``put_dataset`` is the cap-time
  hook — the withheld series never re-enters an LLM token path and never
  rides the SSE stream.

Fully offline — the store call is monkeypatched; no boto3, no env.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import agents.dataset_store as dataset_store
import agents.system_agent as sa

_AGENTS_DIR = Path(sa.__file__).resolve().parent


def _vqt(minute: int, value):
    return {"timestamp": f"2026-04-30T07:{minute:02d}:00Z", "value": value,
            "quality": "GOOD"}


def _series(n: int, base: float = 10.0):
    return [_vqt(i, base + i * 0.5) for i in range(n)]


class _FixedUuid:
    hex = "0123456789abcdef" * 2


@pytest.fixture()
def recorder(monkeypatch):
    calls: list[tuple[str, list]] = []
    monkeypatch.setattr(dataset_store, "put_dataset",
                        lambda ref, series: calls.append((ref, series)) or True)
    return calls


# --------------------------------------------------------------------------- #
# Persistence at cap time
# --------------------------------------------------------------------------- #

def test_capped_series_is_persisted_under_the_minted_handle(recorder):
    raw = _series(40)
    out = sa._cap_history_payload(raw)
    assert len(recorder) == 1
    ref, stored = recorder[0]
    assert ref == out["dataset_ref"]
    assert re.fullmatch(r"tel-[0-9a-f]{12}", ref)
    assert stored == raw                      # the ORIGINAL series, verbatim
    # Tool contract unchanged: exactly the pinned claim-check shape.
    assert set(out) == {"_stats", "dataset_ref", "landmark_values"}


def test_each_capped_series_gets_its_own_write(recorder):
    payload = {
        "a": {"values": _series(30)},
        "b": {"values": _series(25)},
        "short": {"values": _series(5)},
    }
    out = sa._cap_history_payload(payload)
    refs = {ref for ref, _ in recorder}
    assert len(recorder) == 2                 # one write per capped series
    assert refs == {out["a"]["values"]["dataset_ref"],
                    out["b"]["values"]["dataset_ref"]}


def test_short_series_never_persisted(recorder):
    short = _series(8)                        # exactly at cap — untouched
    assert sa._cap_history_payload(short) == short
    assert recorder == []


# --------------------------------------------------------------------------- #
# Failure-swallow honesty — payload byte-identical, warning logged
# --------------------------------------------------------------------------- #

def test_store_failure_leaves_payload_byte_identical(monkeypatch, caplog):
    monkeypatch.setattr("uuid.uuid4", lambda: _FixedUuid())   # deterministic ref
    raw = _series(40)

    monkeypatch.setattr(dataset_store, "put_dataset", lambda ref, series: True)
    with_persistence = sa._cap_history_payload(raw)

    def boom(ref, series):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(dataset_store, "put_dataset", boom)
    with caplog.at_level(logging.WARNING, logger="agents.system_agent"):
        without_persistence = sa._cap_history_payload(raw)

    assert with_persistence == without_persistence            # byte-identical
    assert any("persistence hook failed" in r.getMessage()
               for r in caplog.records)                       # honest, not silent


def test_store_returning_false_is_also_silent_success_for_the_tool(recorder, monkeypatch):
    monkeypatch.setattr(dataset_store, "put_dataset", lambda ref, series: False)
    out = sa._cap_history_payload(_series(20))
    assert set(out) == {"_stats", "dataset_ref", "landmark_values"}


def test_cap_failure_still_returns_structured_error_and_persists_nothing(recorder, monkeypatch):
    """The pinned M6 safety net is untouched by the hook: a summarizer crash
    returns the structured error (never raw), and nothing is persisted."""
    def boom(node, cap):
        raise RuntimeError("summarizer exploded")

    monkeypatch.setattr(sa, "_summarize_vqt_series", boom)
    out = sa._cap_history_payload(_series(25))
    assert out["error"] == "telemetry_payload_cap_failed"
    assert recorder == []


# --------------------------------------------------------------------------- #
# CLIENT-ONLY pin (claim-check policy) — grep-gated reader/writer sets
# --------------------------------------------------------------------------- #

def _grep_agents(pattern: str) -> set[str]:
    regex = re.compile(pattern)
    hits: set[str] = set()
    for path in _AGENTS_DIR.rglob("*.py"):
        if regex.search(path.read_text(encoding="utf-8")):
            hits.add(path.relative_to(_AGENTS_DIR).as_posix())
    return hits


def test_get_dataset_has_exactly_one_production_reader():
    """The withheld series serves the CLIENT only: the sole ``get_dataset``
    caller in agents/ is the admin-gated trace route — never runtime_app,
    never chat_client, never an agent/tool module (no LLM token path, no
    SSE)."""
    assert _grep_agents(r"\bget_dataset\(") == {
        "dataset_store.py",           # the definition
        "agents_trace_routes.py",     # the ONE reader (admin-gated route)
    }


def test_put_dataset_has_exactly_one_production_writer():
    assert _grep_agents(r"\bput_dataset\(") == {
        "dataset_store.py",           # the definition
        "system_agent.py",            # the ONE writer (cap-time hook)
    }
