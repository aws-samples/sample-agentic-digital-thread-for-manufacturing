# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The orchestrator warmup-ping contract, shared by sender and receiver.

The app (``server._invoke_orchestrator_warmup``) pings the orchestrator runtime
every 180 s so the runtime answers. The runtime entrypoint
(``runtime_app._runtime_entrypoint``) recognizes the ping and answers it
WITHOUT a model turn.

Before this contract, the ping reached the full orchestrator and ran a Claude
Sonnet turn on every ping (~37,000 input tokens each, measured in CloudWatch
``AWS/Bedrock InputTokenCount``), about 480 turns a day with no users. Those
turns were also counted as orchestrator sessions and cost on the Agents tab.
"""
from __future__ import annotations

WARMUP_PROMPT = "ping"
WARMUP_USER_ID = "warmup"
#: The ping carries exactly these fields. A real turn's payload always carries
#: more (plant, writeMemory, rawPrompt...), so a caller who sends the warmup
#: user id with the prompt "ping" (``/api/ask/sync`` takes user_id from the
#: body) still gets a normal turn.
WARMUP_PAYLOAD_KEYS = frozenset({"prompt", "userId", "sessionId"})


def is_warmup_payload(payload) -> bool:
    """True only for the app's own warmup ping: its exact fields and values."""
    return (isinstance(payload, dict)
            and set(payload) == WARMUP_PAYLOAD_KEYS
            and payload.get("userId") == WARMUP_USER_ID
            and payload.get("prompt") == WARMUP_PROMPT)
