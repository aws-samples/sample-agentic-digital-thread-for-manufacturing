# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The join-path format check logs field names and counts, never source values."""

import logging

import agents.onboard as onboard


def test_format_check_log_has_no_sample_values(monkeypatch, caplog):
    rows = {"S1": {"rows": [["SECRET-ALPHA"], ["SECRET-BETA"]]},
            "S2": {"rows": [["SECRET-GAMMA"], ["SECRET-DELTA"]]}}
    monkeypatch.setattr(onboard, "pg", lambda schema, sql: rows[schema])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        ok, reason = onboard._validate_joinpath_formats("S1", "t1", "f1", "S2", "t2", "f2")
    assert (ok, reason) == (True, "format_compatible")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "JoinPath format check" in text
    assert "SECRET" not in text
