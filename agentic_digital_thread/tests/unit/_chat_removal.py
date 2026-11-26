# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's removal runs as a background job: helpers to wait for it."""
from __future__ import annotations

import threading


def removal_threads(system_id: str = "") -> list:
    """The chat removal jobs' threads (of ``system_id``, or all)."""
    prefix = f"chat-removal-{system_id}" if system_id else "chat-removal-"
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def wait_for_chat_removals(system_id: str = "", timeout: float = 60.0) -> None:
    """Join the chat removal jobs (of ``system_id``, or all)."""
    for t in removal_threads(system_id):
        t.join(timeout)
        assert not t.is_alive(), f"the removal job {t.name} did not end"


def removed_by_the_chat(reset_system, system_id: str) -> dict:
    """``reset_system(system_id)`` as the chat calls it and, when that
    started a removal, the answer the chat's next check gives once the
    removal job has ended (``onboard.chat_removal_done_answer`` or
    ``onboard.chat_removal_failure_answer``)."""
    from agents import onboard
    started = reset_system(system_id)
    if started.get("status") != "removing":
        return started
    wait_for_chat_removals(system_id)
    return (onboard.chat_removal_done_answer(system_id)
            or onboard.chat_removal_failure_answer(system_id) or started)
