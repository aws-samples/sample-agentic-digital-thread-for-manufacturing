# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for local agent port allocation.

Root cause fixed: _allocate_port tracked only its own in-memory _used_ports set
and never verified OS-level availability, so it could hand out a port already
bound by another process (e.g. an orphaned agent subprocess from a prior/other
run) — the new agent's uvicorn would then fail to bind and the subprocess would
exit, surfacing as an opaque "failed to start".
"""
import socket

import pytest

from agents.agent_manager import AgentManager

# These tests bind real 127.0.0.1 sockets on purpose (OS-level port
# availability IS the behavior under test). enable_socket_scoped opts them
# out of the lane-wide --disable-socket guard; no external network involved.
pytestmark = pytest.mark.enable_socket


def test_port_is_free_detects_bound_and_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        assert AgentManager._port_is_free(busy) is False
    # Port is released when the socket closes; a fresh ephemeral port is free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
        s2.bind(("127.0.0.1", 0))
        free = s2.getsockname()[1]
    assert AgentManager._port_is_free(free) is True


def test_allocate_port_skips_os_occupied_port():
    mgr = AgentManager(mode="local")
    # Occupy the FIRST currently-free port in the manager's range at the OS level.
    # The manager did NOT spawn this, so it is absent from _used_ports (the exact
    # orphaned-subprocess condition that caused the real failure). Picking the
    # first free port keeps the test robust even when earlier ports in the range
    # are already held by unrelated local processes.
    occupied = next((p for p in range(mgr.PORT_MIN, mgr.PORT_MAX + 1)
                     if AgentManager._port_is_free(p)), None)
    assert occupied is not None, "no free port in range to run the test"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", occupied))
        s.listen(1)
        allocated = mgr._allocate_port()
    assert allocated != occupied, "must not hand out an OS-occupied port"
    assert mgr.PORT_MIN <= allocated <= mgr.PORT_MAX
    assert occupied not in mgr._used_ports  # occupied port never recorded as ours
    assert allocated in mgr._used_ports


def test_allocate_port_tracks_and_advances():
    mgr = AgentManager(mode="local")
    a = mgr._allocate_port()
    b = mgr._allocate_port()
    assert a != b
    assert {a, b} <= mgr._used_ports
