# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The AgentCore runtime's span exporter must retry a failed credential lookup.

Live failure (run 4, 2026-11-02, adt_orchestrator): AgentCore pre-warmed
orchestrator sessions sat idle for 341-859 s, then the first span export's
credential lookup returned ``None`` (botocore's instance-metadata provider
swallows a timeout and returns ``None``). aws-opentelemetry-distro 0.19.0's
``AwsAuthSession`` cached that ``None`` for the process lifetime, so every
span batch in the session was sent unsigned and X-Ray answered
``Failed to export span batch code: 403, reason: Forbidden``.

0.22.0 only latches a non-``None`` result, so the next export retries the
lookup. These tests drive the INSTALLED ADOT ``AwsAuthSession`` and run in
the CI ``runtime-lane`` job, which installs the runtime image's hash-pinned
lock; a missing ADOT is an import error there, not a skip. No network:
``requests.Session.request`` is replaced, so nothing is sent.
"""

import datetime
from unittest import mock

from amazon.opentelemetry.distro.exporter.otlp.aws.common.aws_auth_session import AwsAuthSession
from botocore.credentials import Credentials, RefreshableCredentials

_URL = "https://xray.us-east-1.amazonaws.com/v1/traces"


def _send(session):
    """Call AwsAuthSession.request and return the headers it would send."""
    sent = {}

    def _fake_request(_self, method=None, url=None, *args, headers=None, **kwargs):
        sent["headers"] = dict(headers or {})
        return mock.Mock(ok=True, status_code=200)

    with mock.patch("requests.Session.request", _fake_request):
        session.request("POST", _URL, data=b"spans")
    return sent["headers"]


def _credential_scope(headers):
    auth = headers.get("Authorization", "")
    assert auth.startswith("AWS4-HMAC-SHA256 Credential="), auth
    return auth.split("Credential=", 1)[1].split("/", 1)[0]


def test_a_none_credential_lookup_is_retried_on_the_next_export():
    botocore_session = mock.Mock()
    botocore_session.get_credentials.side_effect = [
        None,  # transient instance-metadata miss after an idle wake
        Credentials("AKIDRETRY", "secret", "token"),
    ]
    session = AwsAuthSession(aws_region="us-east-1", service="xray", session=botocore_session)

    first = _send(session)
    assert "Authorization" not in first  # nothing to sign with yet

    second = _send(session)
    assert botocore_session.get_credentials.call_count == 2, (
        "AwsAuthSession cached the None lookup instead of retrying it "
        "(aws-opentelemetry-distro < 0.22.0); spans would stay unsigned (403)"
    )
    assert _credential_scope(second) == "AKIDRETRY"
    assert "/us-east-1/xray/aws4_request" in second["Authorization"]


def test_cached_refreshable_credentials_rotate_before_a_later_signature():
    now = [datetime.datetime(2026, 11, 2, 19, 0, tzinfo=datetime.timezone.utc)]

    def _refresh():
        return {
            "access_key": "AKIDROTATED",
            "secret_key": "secret-2",
            "token": "token-2",
            "expiry_time": (now[0] + datetime.timedelta(hours=6)).isoformat(),
        }

    creds = RefreshableCredentials(
        "AKIDFIRST",
        "secret-1",
        "token-1",
        now[0] + datetime.timedelta(hours=1),
        _refresh,
        "test",
        time_fetcher=lambda: now[0],
    )
    botocore_session = mock.Mock()
    botocore_session.get_credentials.return_value = creds
    session = AwsAuthSession(aws_region="us-east-1", service="xray", session=botocore_session)

    assert _credential_scope(_send(session)) == "AKIDFIRST"
    now[0] += datetime.timedelta(hours=2)  # past the first credentials' expiry
    assert _credential_scope(_send(session)) == "AKIDROTATED"
    assert botocore_session.get_credentials.call_count == 1  # cached object, not re-resolved
