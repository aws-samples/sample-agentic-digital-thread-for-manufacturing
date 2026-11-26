# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The OPC UA simulator server ships a node-opcua outside GHSA-xggf-m53x-jc3f.

The advisory (namespace permission fallback lets a session Write nodes and
Call methods) covers node-opcua, node-opcua-server and node-opcua-address-space
2.42.0 to 2.179.x and is fixed in 2.180.0. The lock check is the advisory
gate: it reads ``package-lock.json`` only, so it runs everywhere.

The second test is a compatibility smoke for the pinned release, not a
regression test for the advisory: it starts ``server.mjs`` bound to 127.0.0.1
(``OPC_UA_HOST``) with a one-tag dataset and connects as an anonymous client with no security, as SFC
does. A Read is Good, and a Write is refused because the server creates its
tags CurrentRead only, on any node-opcua version. It needs the server's
``node_modules`` (``npm ci`` in the server folder) and skips without them.
"""
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = ROOT / "highbyte" / "components" / "opc-server"
PINNED = (2, 187, 32)                  # the pinned release; the advisory's fix is in 2.180.0
ADVISORY_PACKAGES = ("node-opcua", "node-opcua-server", "node-opcua-address-space")


def _version(text: str) -> tuple:
    return tuple(int(p) for p in text.split("-")[0].split("."))


def test_the_lock_resolves_every_node_opcua_package_outside_the_advisory():
    # The exact package.json pin is checked in
    # tests/test_python_dependency_reproducibility.py.
    lock = json.loads((SERVER_DIR / "package-lock.json").read_text())
    pkgs = {path.rsplit("node_modules/", 1)[-1]: meta["version"]
            for path, meta in lock["packages"].items()
            if path.rsplit("node_modules/", 1)[-1].startswith("node-opcua")}
    for name in ADVISORY_PACKAGES:
        assert _version(pkgs[name]) >= PINNED, (name, pkgs[name])
    # No node-opcua 2.x package resolves into the advisory's range (to
    # 2.179.x), so a scanner reading the lock matches none. Some utility
    # packages have no 2.187.32 release (npm latest is older), so the floor
    # for them is the fixed release. node-opcua-crypto and -pki are versioned
    # separately (5.x to 7.x) and are outside the advisory.
    old = {n: v for n, v in pkgs.items() if _version(v)[0] == 2 and _version(v) < (2, 180, 0)}
    assert old == {}, old


CLIENT = r"""
import * as opc from "node-opcua";
const [url, alias] = process.argv.slice(1);
const client = opc.OPCUAClient.create({
    endpointMustExist: false, connectionStrategy: { maxRetry: 0 },
    securityMode: opc.MessageSecurityMode.None, securityPolicy: opc.SecurityPolicy.None,
});
await client.connect(url);
const session = await client.createSession();   // anonymous
const ns = (await session.readNamespaceArray()).indexOf("ManufacturingPlatform");
const nodeId = `ns=${ns};s=${alias}`;
const read = await session.read({ nodeId, attributeId: opc.AttributeIds.Value });
const write = await session.write({
    nodeId, attributeId: opc.AttributeIds.Value,
    value: { value: { dataType: opc.DataType.Double, value: -1 } },
});
const after = await session.read({ nodeId, attributeId: opc.AttributeIds.Value });
console.log(JSON.stringify({
    read: read.statusCode.name, value: read.value.value,
    write: write.name, after: after.value.value,
}));
await session.close();
await client.disconnect();
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.enable_socket   # a real 127.0.0.1 server, no external network
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(not (SERVER_DIR / "node_modules" / "node-opcua").is_dir(),
                    reason="run npm ci in highbyte/components/opc-server first")
def test_the_pinned_server_serves_reads_and_refuses_an_anonymous_write(tmp_path):
    alias = "Example/TestSite/Area/Line1/Machine1/Machine1_Temperature"
    (tmp_path / "dataset.csv").write_text(
        "alias,type,a,b,c,value\n"
        + "".join(f"{alias},DOUBLE,,,,{v}\n" for v in (21.5, 22.5)))
    # shortcut: the port is free when picked and could be taken before node
    # binds it; a clash fails the start with the bind error in the log.
    port = _free_port()
    log = tmp_path / "server.log"
    out_file = log.open("w")
    server = subprocess.Popen(
        ["node", str(SERVER_DIR / "server.mjs"), "dataset.csv"], cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "OPC_UA_PORT": str(port), "OPC_UA_HOST": "127.0.0.1"},
        stdout=out_file, stderr=subprocess.STDOUT)
    try:
        # Poll the log file, so a server that stalls or exits cannot block the wait.
        deadline = time.monotonic() + 60
        while "listening on port" not in log.read_text():
            assert server.poll() is None and time.monotonic() < deadline, log.read_text()
            time.sleep(0.2)
        out = subprocess.run(
            ["node", "--input-type=module", "-e", CLIENT, f"opc.tcp://127.0.0.1:{port}/UA/ManufacturingServer", alias],
            cwd=SERVER_DIR, capture_output=True, text=True, timeout=60, check=True)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        listeners = []
        if shutil.which("ss"):
            listeners = subprocess.run(["ss", "-ltnH", f"sport = :{port}"],
                                       capture_output=True, text=True).stdout.split()
        result["listen"] = [w for w in listeners if w.endswith(f":{port}")]
        print(result)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        out_file.close()
    assert result["read"] == "Good" and result["value"] in (21.5, 22.5), result
    assert result["write"] in ("BadNotWritable", "BadUserAccessDenied"), result
    assert result["after"] in (21.5, 22.5), result
    if shutil.which("ss"):
        assert result["listen"] == [f"127.0.0.1:{port}"], result
