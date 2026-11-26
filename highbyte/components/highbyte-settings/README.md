<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# HighByte settings seed

`intelligencehub-settings.json` is the HighByte **application settings** file —
it controls which servers boot (MQTT broker, REST Data Server, and the MCP
server on 8885). HighByte reads it from `appData` on startup and, when absent,
falls back to defaults that leave the REST/MCP servers **disabled**.

`ADTHighByteOpcuaStack` runs a tiny **init container** that writes this file to the
HighByte EFS volume **only on first boot** (if it does not already exist), then
the HighByte container starts. On later boots the EFS copy is preserved, so
changes saved through the HighByte UI persist, with two exceptions the init
container enforces on the existing file every time: the REST/MCP server's
`allowAnonymous` is set to `false`, and MCP Configuration Tools are turned off
(`serversConfig.mcpServer.configTools`, the key the hub persists, and the
shipped `restDataServer.mcpConfigTools`). An existing file it cannot read
that way keeps the hub from starting.

This replaces the previous custom wrapper image: the HighByte container now runs
the unmodified public `highbyte/intelligence-hub` image from Docker Hub, with the
`ACCEPT_EULA=Y` environment variable set from the deployer's explicit acceptance
of the HighByte EULA (`deploy.sh --accept-highbyte-eula`).

> **REST Data Server (internet-facing):** the REST Data + MCP server on port
> 8885 is reachable from the internet through CloudFront (`/data/*`, `/mcp*`),
> so it ships `allowAnonymous: false` and keeps `authenticationModes:
> ["API_KEY"]` — an unauthenticated client cannot enumerate or read OT data,
> while the MCP server still works with its bearer key. The target groups'
> health check (`/data/doc`) accepts `200-499` (`healthy_http_codes`), so
> turning anonymous access off does not fail health checks: a local 4.4.2 hub
> answers an unauthenticated `/data/doc` with 303. When an unlicensed hub's
> trial expires it stops the HTTP server on 8885, so the check fails on the
> closed port whatever the code range, and ECS replaces the task.
>
> **MCP Configuration Tools (`mcpConfigTools`, off):** with them on, the MCP
> server also lists `config_*` tools that create and edit pipelines with the
> API key's user's rights, and ADT's key belongs to the administrator. The
> file ships them `disabled`, so the server lists only the project's
> API-triggered pipelines (`NameSpace_MCP`). The seed also turns them off on
> a settings file already on EFS. ADT additionally keeps only the read tool
> for the built-in `highbyte` system whatever this setting says
> (`agentic_digital_thread/agents/builtin_highbyte.py`).
>
> **MQTT broker (`hbMqttBroker`, DEMO ONLY):** the broker on ports 1883/1884 is
> NOT exposed through CloudFront or the ALB (no listener maps those ports), so
> it is not internet-reachable; it keeps `allowAnonymous: true` for the hub's
> own local `NameSpace_to_LocalBroker` pipeline. In production, set it to
> `false` and configure broker authentication before exposing it.
