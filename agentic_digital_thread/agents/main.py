# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CLI entry point for the agent system."""

import sys

from .orchestrator import get_orchestrator
from .branding import COMPANY
from . import demo


def _load_demo_questions():
    """Source CLI demo questions from the demo addon seam (ADR 0019).

    Demo content is an optional addon gated behind ``ADT_DEMO``. When the demo is
    enabled, return the curated CLI demo questions from ``agents.demo``; when
    disabled (the default), the core carries no demo data and this returns an
    empty list (FLAG-D1). No data is fabricated and the CLI never crashes in a
    minimal environment.
    """
    if demo.is_enabled():
        return demo.demo_questions()
    return []


DEMO_QUESTIONS = _load_demo_questions()


def main():
    orchestrator = get_orchestrator()

    if "--interactive" in sys.argv:
        print(f"{COMPANY} — Interactive Mode")
        print("Type 'quit' to exit.\n")
        while True:
            question = input("Question: ").strip()
            if question.lower() in ("quit", "exit", "q"):
                break
            if question:
                result = orchestrator(question)
                print(f"\n{result}\n")
        return

    if len(sys.argv) > 1 and sys.argv[1] != "--demo":
        questions = [" ".join(sys.argv[1:])]
    else:
        questions = DEMO_QUESTIONS

    for q in questions:
        print(f"\n{'='*60}")
        print(f"Question: {q}")
        print("=" * 60)
        result = orchestrator(q)
        print(f"\n{result}\n")


if __name__ == "__main__":
    main()
