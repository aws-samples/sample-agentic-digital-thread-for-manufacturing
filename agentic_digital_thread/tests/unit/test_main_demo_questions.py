# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the CLI demo-questions reroute through the demo seam.

The core carries no demo content: with ``ADT_DEMO`` unset, ``_load_demo_questions``
returns an empty list (FLAG-D1) and ``agents.main`` imports cleanly. With
``ADT_DEMO`` enabled the questions come verbatim from the ``agents.demo`` seam.
"""

import importlib

from agents import demo, main


def test_demo_off_returns_empty_list(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    assert main._load_demo_questions() == []


def test_demo_off_import_succeeds(monkeypatch):
    monkeypatch.delenv("ADT_DEMO", raising=False)
    reloaded = importlib.reload(main)
    assert reloaded.DEMO_QUESTIONS == []


def test_demo_on_returns_seam_questions(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    questions = main._load_demo_questions()
    assert questions == demo.demo_questions()
    assert len(questions) > 0


def test_demo_on_module_attr_matches_seam(monkeypatch):
    monkeypatch.setenv("ADT_DEMO", "1")
    reloaded = importlib.reload(main)
    assert reloaded.DEMO_QUESTIONS == demo.demo_questions()
    assert len(reloaded.DEMO_QUESTIONS) > 0
