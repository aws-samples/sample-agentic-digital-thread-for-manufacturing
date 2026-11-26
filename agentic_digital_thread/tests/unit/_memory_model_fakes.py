# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fakes for the memory extraction model tests: an AgentCore Memory
shaped like the live GetMemory answer (adt-demo, 2026-10-27), a control-plane
fake for GetMemory/UpdateMemory, and a fake clock."""
from __future__ import annotations

import copy

OLD = "global.anthropic.claude-haiku-5-5"
NEW = "global.anthropic.claude-sonnet-5-5"
MEMORY_ID = "adt_memory-AbCdEf1234"
LANG = "Write every extracted record ... in ENGLISH."


def override(model_id: str = OLD, prompt: str = LANG) -> dict:
    return {"appendToPrompt": prompt, "modelId": model_id}


def strategies(model_id: str = OLD) -> list[dict]:
    return [
        {"strategyId": "facts-b7K2OdBQoY", "name": "facts", "type": "CUSTOM", "status": "ACTIVE",
         "namespaces": ["/facts/{actorId}/"],
         "configuration": {
             "type": "SEMANTIC_OVERRIDE",
             "extraction": {"customExtractionConfiguration": {
                 "semanticExtractionOverride": override(model_id, LANG + " facts-x")}},
             "consolidation": {"customConsolidationConfiguration": {
                 "semanticConsolidationOverride": override(model_id, LANG + " facts-c")}}}},
        {"strategyId": "summaries-Q1w2E3r4T5", "name": "summaries", "type": "CUSTOM",
         "status": "ACTIVE", "namespaces": ["/summaries/{actorId}/{sessionId}/"],
         "configuration": {
             "type": "SUMMARY_OVERRIDE",
             "consolidation": {"customConsolidationConfiguration": {
                 "summaryConsolidationOverride": override(model_id, LANG + " summaries")}}}},
        {"strategyId": "preferences-Z9x8C7v6B5", "name": "preferences", "type": "CUSTOM",
         "status": "ACTIVE", "namespaces": ["/preferences/{actorId}/"],
         "configuration": {
             "type": "USER_PREFERENCE_OVERRIDE",
             "extraction": {"customExtractionConfiguration": {
                 "userPreferenceExtractionOverride": override(model_id, LANG + " prefs-x")}},
             "consolidation": {"customConsolidationConfiguration": {
                 "userPreferenceConsolidationOverride": override(model_id, LANG + " prefs-c")}}}},
    ]


def memory_record(model_id: str = OLD, status: str = "ACTIVE") -> dict:
    return {"id": MEMORY_ID, "name": "adt_memory", "status": status,
            "eventExpiryDuration": 7, "strategies": strategies(model_id)}


def _first_model_id(configuration: dict) -> str:
    part = configuration.get("extraction") or configuration["consolidation"]
    (union,) = part.values()
    (override_config,) = union.values()
    return override_config["modelId"]


class FakeControl:
    """GetMemory/UpdateMemory. After an update the memory reads UPDATING for
    ``busy_reads`` GetMemory calls, then ``final_status`` on the new model ids
    (unless ``apply`` is False, which keeps the old ids: an update that did not
    take)."""

    def __init__(self, memory: dict | None = None, *, busy_reads: int = 2,
                 apply: bool = True, final_status: str = "ACTIVE"):
        self.memory = memory or memory_record()
        self.busy_reads = busy_reads
        self.apply = apply
        self.final_status = final_status
        self.updates: list[dict] = []
        self.gets = 0
        self._busy = 0

    def get_memory(self, memoryId):  # noqa: N803 - boto3 casing
        assert memoryId == MEMORY_ID
        self.gets += 1
        read = copy.deepcopy(self.memory)
        if self._busy:
            self._busy -= 1
            read["status"] = "UPDATING"
            read["strategies"][0]["status"] = "CREATING"
        elif self.updates:
            read["status"] = self.final_status
        return {"memory": read}

    def update_memory(self, **kwargs):
        self.updates.append(copy.deepcopy(kwargs))
        self._busy = self.busy_reads
        if self.apply:
            first = kwargs["memoryStrategies"]["modifyMemoryStrategies"][0]["configuration"]
            self.memory = memory_record(_first_model_id(first))
        return {"memory": {"id": MEMORY_ID}}


class Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeParameters:
    """SSM ``get_parameters`` as boto3 answers it: a missing name is listed
    in ``InvalidParameters`` (no exception). ``fail`` raises on every read."""

    def __init__(self, store: dict | None = None, *, fail: bool = False):
        self.store = dict(store or {})
        self.fail = fail
        self.reads: list[list[str]] = []

    def get_parameters(self, Names):  # noqa: N803 - boto3 casing
        self.reads.append(list(Names))
        if self.fail:
            raise RuntimeError("AccessDenied arn:aws:ssm:us-east-1:111122223333:parameter/x")
        return {"Parameters": [{"Name": n, "Value": self.store[n]} for n in Names
                               if n in self.store],
                "InvalidParameters": [n for n in Names if n not in self.store]}
