# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Real botocore clients that never reach AWS.

``AwsRecorder.client(service)`` (and ``resource``) builds an ordinary boto3
client with fake credentials. Its ``before-send`` handler records every
request botocore was about to put on the wire and answers it from a canned
response instead of the network. Everything before sending runs as it does in
production: parameter validation, every handler before sending (the lease
write check among them, on every attempt), signing, the SDK's retries and
response parsing, errors included. So a request the lease check stopped is
never in :attr:`AwsRecorder.sent`.

``answer(operation, *answers)`` queues what an operation answers (the last one
repeats; an unanswered operation answers ``{}``): a dict body, an
:func:`error`, or a callable that gets the request's parsed body and returns
either. ``on_send[operation]`` runs when that operation is sent, before it is
answered (a test loses its lease there).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import boto3
from botocore.awsrequest import AWSResponse


@dataclass
class Sent:
    operation: str
    url: str
    body: dict


@dataclass
class Error:
    code: str
    status: int = 400
    message: str = ""


def error(code: str, status: int = 400, message: str = "") -> Error:
    """An error answer (``x-amzn-ErrorType`` and ``__type`` both set, so the
    JSON and the REST-JSON protocols both read it)."""
    return Error(code, status, message or code)


class _Raw:
    """The raw HTTP body botocore reads: whole (``stream``) or as a
    streaming payload (``read``)."""

    def __init__(self, data: bytes):
        self._data = data

    def stream(self, **_kwargs):
        yield self._data

    def read(self, amt=None, **_kwargs):
        data, self._data = self._data, b""
        return data


@dataclass
class AwsRecorder:
    sent: list = field(default_factory=list)
    on_send: dict = field(default_factory=dict)
    _answers: dict = field(default_factory=dict)

    def answer(self, operation: str, *answers) -> None:
        self._answers[operation] = list(answers)

    def ops(self, operation: "str | None" = None) -> list:
        """The operations sent, in order (only *operation*'s when given)."""
        return [s for s in self.sent if operation in (None, s.operation)]

    def names(self) -> list:
        return [s.operation for s in self.sent]

    @staticmethod
    def _session(region: str):
        return boto3.session.Session(aws_access_key_id="testing",
                                     aws_secret_access_key="testing",  # nosec B106 - fake
                                     aws_session_token="testing", region_name=region)

    def client(self, service: str, region_name: str = "us-east-1", config=None, **_kwargs):
        """A client for *service*; *config* (a ``botocore.config.Config``)
        applies as in production, its retry settings included."""
        client = self._session(region_name).client(service, region_name=region_name,
                                                   config=config)
        client.meta.events.register("before-send", self._send)
        return client

    def resource(self, service: str, region_name: str = "us-east-1", config=None, **_kwargs):
        resource = self._session(region_name).resource(service, region_name=region_name,
                                                       config=config)
        resource.meta.client.meta.events.register("before-send", self._send)
        return resource

    def attach(self, client):
        """Record and answer the requests of a client built elsewhere."""
        client.meta.events.register("before-send", self._send)
        return client

    def _next(self, operation: str):
        queue = self._answers.get(operation) or [{}]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def _send(self, request, event_name: str, **_kwargs):
        operation = event_name.rsplit(".", 1)[-1]
        raw = request.body or b"{}"
        raw = raw.decode() if isinstance(raw, bytes) else raw
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"raw": raw}
        sent = Sent(operation, request.url, body)
        self.sent.append(sent)
        hook = self.on_send.get(operation)
        if hook is not None:
            hook(sent)
        answer = self._next(operation)
        if callable(answer):
            answer = answer(sent)
        if isinstance(answer, Error):
            headers = {"x-amzn-ErrorType": answer.code, "x-amzn-RequestId": "test"}
            data = json.dumps({"__type": answer.code, "message": answer.message})
            return AWSResponse(request.url, answer.status, headers, _Raw(data.encode()))
        headers = {"x-amzn-RequestId": "test"}
        return AWSResponse(request.url, 200, headers, _Raw(json.dumps(answer).encode()))
