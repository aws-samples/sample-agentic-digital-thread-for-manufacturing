<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# HighByte OT / UNS Layer (`highbyte/`)

This directory holds the assets for the optional OT/UNS layer: OPC UA dataset
generation (`generate_datasets.py`), AWS IoT SiteWise model deployment
(`DeploySitewiseModels.py`, `deploy_sitewise.sh`), cleanup
(`cleanup_sitewise.py`), the pipeline transform (`pipeline_sitewise.json`), and
the OPC UA server components.

> HighByte is licensed third-party software. The container image is the public
> `highbyte/intelligence-hub` image from
> [Docker Hub](https://hub.docker.com/r/highbyte/intelligence-hub); this repository
> does **not** redistribute the HighByte image or include a HighByte license file.
> The container runs only with `ACCEPT_EULA=Y`, and setting it agrees to the
> [HighByte Intelligence Hub End User License Agreement](https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf),
> so the first HighByte deploy needs your explicit `--accept-highbyte-eula`
> (recorded once; a HighByte deploy without it stops with the link). You are
> responsible for obtaining any required HighByte license. An administrator
> uploads the license through the HighByte UI after startup (the hub boots in
> trial mode without one). See
> [`../docs/deployment.md`](../docs/deployment.md#adding-highbyte-intelligence-hub).
>
> The bundled `config/highbyte/intelligencehub-configuration.json` (staged
> into the `highbyte-setup` Lambda asset at synth by `stacks/_staging.py`) is an
> **AWS-authored demo project configuration** exported in the HighByte project
> file format — it is not a redistributed HighByte artifact (the `company` and
> `version` fields it carries are format metadata written by the export).
> Likewise `pipeline_sitewise.json` contains only first-party-authored pipeline
> configuration plus a machine-generated `productInfo` block that HighByte
> Intelligence Hub writes on export.

## Documentation

The full reference for this layer — what `ADTHighByteOpcuaStack` deploys, ports,
stack outputs, the MCP server and Unified Namespace, the AWS IoT SiteWise
pipeline, and troubleshooting — lives in
[`../docs/highbyte.md`](../docs/highbyte.md).

- Deploy it: [`../docs/deployment.md`](../docs/deployment.md#adding-highbyte-intelligence-hub)
- Configure it (MCP token, SiteWise pipeline): [`../docs/post-deploy.md`](../docs/post-deploy.md)
- Operate it (teardown, SiteWise reset, quotas): [`../docs/operations.md`](../docs/operations.md)
