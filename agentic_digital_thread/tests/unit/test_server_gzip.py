# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The app gzips large JSON but never buffers/compresses the SSE stream."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

import agents.server as server


def test_production_app_registers_gzip_outermost():
    classes = [m.cls for m in server.app.user_middleware]
    assert classes[0] is GZipMiddleware


def _bare_app() -> TestClient:
    app = FastAPI()

    @app.get("/big")
    def big():
        return {"rows": ["x" * 50] * 200}

    @app.get("/sse")
    def sse():
        def gen():
            yield ": keepalive\n\n"
            yield "event: token\ndata: {\"text\": \"" + "y" * 2000 + "\"}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    app.add_middleware(GZipMiddleware, minimum_size=server.GZIP_MINIMUM_SIZE)
    return TestClient(app)


def test_large_json_is_gzipped():
    r = _bare_app().get("/big", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_sse_stream_is_never_compressed_and_keepalive_is_verbatim():
    r = _bare_app().get("/sse", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers
    assert r.text.startswith(": keepalive\n\n")
