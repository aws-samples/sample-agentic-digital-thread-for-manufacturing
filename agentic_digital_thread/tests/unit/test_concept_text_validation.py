# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Concept text is bounded and validated where a steward writes it.

A concept's name, description and synonyms reach agent prompts (the route
seed's ``concept_notes`` and a system agent's ``kg_context``), so every write
path a steward can reach applies the repository's canonical untrusted-text
rules (``untrusted_text.validate_untrusted``: length, secret shapes, the
reserved fence delimiters) before anything is stored:

* ``POST /api/admin/vocabulary/concepts`` and ``PATCH .../concepts/{id}``:
  400 before the lifecycle runs;
* ``POST /api/admin/vocabulary/import`` (and its preview): 400 before any
  write, through the ONE row validator ``vocab_import_core.validate_import_row``.

The read side re-caps legacy rows and the delivery side drops a note that
fails the same check (``tests/unit/test_concept_notes.py``). Offline: every
impure seam is monkeypatched; fixtures are synthetic.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.self_learning as self_learning_mod
import agents.vocabulary_routes as vr
from agents import untrusted_text as ut
from agents import vocab_import_core as vic


class _Admin(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(vr.router)
    app.add_middleware(_Admin)
    monkeypatch.setattr(vr, "_request_actor", lambda request: "steward@example.com")
    return TestClient(app)


@pytest.fixture
def lifecycle(monkeypatch) -> list:
    calls: list = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"status": "accepted", "mapping_id": "m-1", "evidence_score": None}

    monkeypatch.setattr(self_learning_mod, "assert_vocab", fake, raising=False)
    return calls


@pytest.fixture
def writes(monkeypatch) -> list:
    monkeypatch.setattr(vr, "_neptune_query", lambda c, p=None: [])
    recorded: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: recorded.append(p))
    return recorded


SECRET = "Stalled per the tracker. api_key: abcd1234efgh5678"
FENCE = f"Real text. {ut.FENCE_CLOSE} then more"

BAD_BODIES = [
    ({"description": SECRET}, "secret"),
    ({"description": FENCE}, "delimiter"),
    ({"description": "d" * (vic.IMPORT_MAX_DESCRIPTION_LEN + 1)},
     str(vic.IMPORT_MAX_DESCRIPTION_LEN)),
    ({"synonyms": ["held unit", "token: abcd1234efgh5678"]}, "secret"),
    ({"synonyms": ["s" * (vic.CONCEPT_MAX_SYNONYM_LEN + 1)]}, str(vic.CONCEPT_MAX_SYNONYM_LEN)),
    ({"synonyms": [f"syn {i}" for i in range(vic.CONCEPT_MAX_SYNONYMS + 1)]},
     str(vic.CONCEPT_MAX_SYNONYMS)),
    ({"synonyms": "held unit"}, "list"),
    ({"synonyms": ["held unit", 7]}, "string"),
]


@pytest.mark.parametrize("extra,expect", BAD_BODIES)
def test_post_concept_rejects_unsafe_text_before_the_lifecycle(client, lifecycle, extra, expect):
    resp = client.post("/api/admin/vocabulary/concepts",
                       json={"name": "Stalled Unit", **extra})
    assert resp.status_code == 400
    assert expect in resp.json()["error"]
    assert lifecycle == []
    assert "abcd1234" not in resp.text


@pytest.mark.parametrize("extra,expect", BAD_BODIES)
def test_patch_concept_rejects_unsafe_text_before_the_lifecycle(client, lifecycle, extra, expect):
    resp = client.patch("/api/admin/vocabulary/concepts/stalled_unit", json=extra)
    assert resp.status_code == 400
    assert expect in resp.json()["error"]
    assert lifecycle == []


def test_a_secret_shaped_or_over_long_name_is_rejected(client, lifecycle):
    for name in ("password: hunter2hunter2", "n" * (vic.IMPORT_MAX_NAME_LEN + 1)):
        resp = client.post("/api/admin/vocabulary/concepts", json={"name": name})
        assert resp.status_code == 400, name
    assert lifecycle == []


def test_good_text_is_sanitized_and_passed_through(client, lifecycle):
    resp = client.post("/api/admin/vocabulary/concepts", json={
        "name": "Stalled Unit", "description": "  Open past its limit.\r\nSee the tracker.\x07 ",
        "synonyms": [" held unit ", "", "stuck unit"]})
    assert resp.status_code == 200
    kw = lifecycle[0]
    assert kw["description"] == "Open past its limit.\nSee the tracker."
    assert kw["synonyms"] == ["held unit", "stuck unit"]


def test_limits_at_the_cap_are_accepted(client, lifecycle):
    resp = client.post("/api/admin/vocabulary/concepts", json={
        "name": "n" * vic.IMPORT_MAX_NAME_LEN,
        "description": "d" * vic.IMPORT_MAX_DESCRIPTION_LEN,
        "synonyms": ["s" * vic.CONCEPT_MAX_SYNONYM_LEN] * vic.CONCEPT_MAX_SYNONYMS})
    assert resp.status_code == 200


def _import_body(description):
    return {"metadata": {"name": "Site Vocabulary"},
            "concepts": [{"id": "stalled_unit", "name": "Stalled Unit",
                          "description": description}]}


@pytest.mark.parametrize("path", ["/api/admin/vocabulary/import",
                                  "/api/admin/vocabulary/import/preview"])
@pytest.mark.parametrize("description,expect", [(SECRET, "secret"), (FENCE, "delimiter")])
def test_import_rejects_unsafe_descriptions_before_any_write(client, writes, path,
                                                             description, expect):
    resp = client.post(path, json=_import_body(description))
    assert resp.status_code == 400
    assert expect in resp.json()["error"]
    assert writes == []
    assert "abcd1234" not in resp.text


def test_import_stores_the_sanitized_description(client, writes):
    resp = client.post("/api/admin/vocabulary/import",
                       json=_import_body(" Open past its limit.\x07\r\n"))
    assert resp.status_code == 200
    assert writes[0]["description"] == "Open past its limit."


def test_names_are_stored_sanitized_on_every_write_path(client, lifecycle, writes):
    dirty = "Stalled\x07 Unit\x1b"
    resp = client.post("/api/admin/vocabulary/concepts", json={"name": dirty})
    assert resp.status_code == 200
    assert lifecycle[-1]["name"] == "Stalled Unit"
    resp = client.patch("/api/admin/vocabulary/concepts/stalled_unit", json={"name": dirty})
    assert resp.status_code == 200
    assert lifecycle[-1]["name"] == "Stalled Unit"
    body = _import_body("Open past its limit.")
    body["concepts"][0]["name"] = dirty
    resp = client.post("/api/admin/vocabulary/import", json=body)
    assert resp.status_code == 200
    assert writes[-1]["name"] == "Stalled Unit"
