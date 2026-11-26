# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


class TestProbeCrossAccountSession:
    """Live defect 2026-07-26: probes ran with the APP task's credentials
    after the assume-role preflight passed — probing the wrong account and
    reporting a false blocked verdict for healthy cross-account systems."""

    def _resolved(self):
        return {"connection_type": "Athena", "workgroup": "wg-x",
                "region": "us-east-1",
                "role_arn": "arn:aws:iam::111122223333:role/XRead",
                "external_id": "xid-1",
                "connection_config": {"workgroup": "wg-x"}}

    def test_athena_probe_uses_assumed_session(self, monkeypatch):
        from agents import connection_probes as cp
        monkeypatch.setattr(cp, "validate_connection_target",
                            lambda *a, **k: None)
        captured = {}

        class FakeClient:
            def get_work_group(self, WorkGroup):
                captured["wg"] = WorkGroup
                return {"WorkGroup": {"State": "ENABLED"}}

        class FakeSession:
            def client(self, service, region_name=None):
                captured["service"] = service
                return FakeClient()

        def fake_session_for(cfg, default_region=None, service="adt"):
            captured["cfg"] = dict(cfg or {})
            return FakeSession()

        import agents.aws_session as aws_session
        monkeypatch.setattr(aws_session, "session_for", fake_session_for)
        detail = cp._probe_athena(self._resolved())
        assert captured["cfg"].get("role_arn", "").endswith("role/XRead")
        assert captured["cfg"].get("external_id") == "xid-1"
        assert captured["wg"] == "wg-x"
        assert "wg-x" in detail

    def test_sitewise_probe_uses_assumed_session(self, monkeypatch):
        from agents import connection_probes as cp
        monkeypatch.setattr(cp, "validate_connection_target",
                            lambda *a, **k: None)
        captured = {}

        class FakeClient:
            def list_asset_models(self, maxResults):
                return {"assetModelSummaries": []}

        class FakeSession:
            def client(self, service, region_name=None):
                captured["service"] = service
                return FakeClient()

        import agents.aws_session as aws_session
        monkeypatch.setattr(aws_session, "session_for",
                            lambda cfg, default_region=None, service="adt":
                            (captured.__setitem__("cfg", dict(cfg or {})),
                             FakeSession())[1])
        resolved = {"connection_type": "SiteWise (Direct)",
                    "region": "us-east-1",
                    "role_arn": "arn:aws:iam::111122223333:role/XRead",
                    "external_id": "xid-1", "connection_config": {}}
        cp._probe_sitewise(resolved)
        assert captured["cfg"].get("role_arn", "").endswith("role/XRead")

    def test_ambient_when_no_role_arn(self, monkeypatch):
        from agents import connection_probes as cp
        monkeypatch.setattr(cp, "validate_connection_target",
                            lambda *a, **k: None)
        captured = {}

        class FakeClient:
            def get_work_group(self, WorkGroup):
                return {"WorkGroup": {"State": "ENABLED"}}

        class FakeSession:
            def client(self, service, region_name=None):
                return FakeClient()

        import agents.aws_session as aws_session
        monkeypatch.setattr(aws_session, "session_for",
                            lambda cfg, default_region=None, service="adt":
                            (captured.__setitem__("cfg", dict(cfg or {})),
                             FakeSession())[1])
        cp._probe_athena({"connection_type": "Athena", "workgroup": "wg-y",
                          "region": "us-east-1", "connection_config": {}})
        assert not captured["cfg"].get("role_arn")
