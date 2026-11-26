# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the SFC configuration generator
(``agents/demo/sfc_config.py``, ADR-0082).

The load-bearing test is model-resolution parity: the generator's
:func:`resolve_model` is a PORT of the ``getModel`` function embedded in
``highbyte/pipeline_sitewise.json``'s reshape stage, and it must agree with that
function for EVERY tag family on every site (ADR-0082 decision 3). Rather
than hand-copy the expected models (which would decay silently and would smuggle
demo literals into this file, tripping the ADR-0044 neutrality ratchet), the
oracle is derived at runtime two ways:

* the process NODES come from the real tag map (``generate_datasets.py``), so
  the cases are exactly the families that exist; and
* the expected MODEL comes from the ``getModel`` SOURCE STRING extracted from
  ``pipeline_sitewise.json`` and translated into the branch table the JS
  encodes — so a change to either file re-derives, and this test file itself
  carries no site/equipment/city literal.

Everything is loaded BY PATH with ``importlib`` (the ``app.py`` /
``test_generate_datasets_totals.py`` pattern): the ``highbyte`` tree is not an
installed package. Offline, pure — no AWS, no sockets.

Run:
  cd agentic_digital_thread && PYTHONPATH=. python3 -m pytest \\
    tests/unit/test_sfc_config.py -q --disable-socket --allow-unix-socket
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agents.demo import sfc_config

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PIPELINE_PATH = _REPO_ROOT / "highbyte" / "pipeline_sitewise.json"


# ---------------------------------------------------------------------------
# The getModel oracle, derived from pipeline_sitewise.json rather than copied.
# ---------------------------------------------------------------------------


def _get_model_source() -> str:
    """The ``getModel`` function body text, extracted from the pipeline JSON's
    reshape ``transformExpression`` (the authoritative source of the mapping)."""
    pipeline = json.loads(_PIPELINE_PATH.read_text())
    expr = None
    for stage in pipeline["project"]["pipelines"][0]["stages"]:
        cfg = stage.get("config", {})
        text = cfg.get("transformExpression", "")
        if "function getModel" in text:
            expr = text
            break
    assert expr, "getModel not found in pipeline_sitewise.json transformExpression"
    m = re.search(r"function getModel\([^)]*\)\{(.*?)\}var ns=", expr, re.S)
    assert m, "could not isolate the getModel body"
    return m.group(1)


def _oracle_from_source(src: str):
    """Build an independent getModel oracle parsed from the JS source string.

    The parser lifts the literal branch table out of the source rather than
    re-encoding the rule in Python, so the oracle is anchored to the file. It
    understands the exact clause shapes the transform uses.
    """
    # suffix === 'X' return hasPump ? 'A' : 'B'  ->  {'X': ('A', 'B')}
    pump_variants: dict[str, tuple[str, str]] = {
        s: (a, b)
        for s, a, b in re.findall(
            r"if\(suffix==='(\w+)'\)return hasPump\?'(\w+)':'(\w+)';", src
        )
    }
    # multi-suffix -> one model:  suffix==='A'||suffix==='B'... return 'M'
    grouped: dict[str, str] = {}
    for clause, model in re.findall(
        r"if\(((?:suffix==='\w+'(?:\|\|)?)+)\)return '(\w+)';", src
    ):
        for s in re.findall(r"suffix==='(\w+)'", clause):
            grouped[s] = model
    # Area-conditional clause: suffix===S then, if area contains any token,
    # model A else model B. Suffix name captured from source, not hardcoded.
    area_m = re.search(
        r"if\(suffix==='(\w+)'\)\{if\(area&&\((.*?)\)\)return '(\w+)';return '(\w+)';\}",
        src,
    )
    assert area_m, "area-conditional clause not found in getModel source"
    area_suffix = area_m.group(1)
    area_tokens = re.findall(r"indexOf\('(\w+)'\)>=0", area_m.group(2))
    area_hit, area_miss = area_m.group(3), area_m.group(4)
    # Attr-conditional clause: suffix===S then attr key contains token -> model
    # (in listed order), else a default. Distinguished from the area clause by
    # its ``keys[...]`` scan; suffix name captured from source, not hardcoded.
    attr_m = re.search(
        r"if\(suffix==='(\w+)'\)\{(var keys=Object\.keys\(attrs\);.*?)return '(\w+)';\}",
        src,
    )
    assert attr_m, "attr-conditional clause not found in getModel source"
    attr_suffix = attr_m.group(1)
    attr_rules = re.findall(r"keys\[j\]\.indexOf\('(\w+)'\)>=0\)return '(\w+)'", attr_m.group(2))
    attr_default = attr_m.group(3)

    def oracle(suffix: str, attr_keys: list[str], area: str) -> str:
        if suffix in pump_variants:
            has_pump = any("Pump" in k for k in attr_keys)
            a, b = pump_variants[suffix]
            return a if has_pump else b
        if suffix in grouped:
            return grouped[suffix]
        if suffix == area_suffix:
            if area and any(tok in area for tok in area_tokens):
                return area_hit
            return area_miss
        if suffix == attr_suffix:
            for k in attr_keys:
                for tok, model in attr_rules:
                    if tok in k:
                        return model
            return attr_default
        return suffix

    return oracle, {
        "pump_variants": pump_variants,
        "grouped": grouped,
        "area_suffix": area_suffix,
        "attr_suffix": attr_suffix,
        "attr_rules": attr_rules,
    }


@pytest.fixture(scope="module")
def oracle():
    return _oracle_from_source(_get_model_source())


@pytest.fixture(scope="module")
def datasets():
    """The tag-map generator module, loaded by path (module fixture)."""
    return sfc_config._load_by_path("gen_datasets_for_sfc_test", sfc_config._DATASETS_PATH)


def _every_process_node(datasets):
    """Yield ``(site, area, line, node, suffix, attr_keys)`` for EVERY process
    node the tag map produces across all sites — the full family set."""
    for site in sfc_config.SITES:
        for line, tags in site.line_tags(datasets):
            nodes = sfc_config._line_nodes(tags, line)
            for node, leaves in nodes.items():
                suffix = sfc_config._suffix(node, line)
                attr_keys = [sfc_config._channel_property(node, leaf) for leaf in leaves]
                yield site, site.area, line, node, suffix, attr_keys


# ---------------------------------------------------------------------------
# THE test that matters: resolve_model agrees with getModel for EVERY family.
# ---------------------------------------------------------------------------


def test_resolve_model_agrees_with_pipeline_get_model_for_every_family(oracle, datasets):
    get_model, _tables = oracle
    seen = 0
    families: set[tuple[str, str, bool]] = set()
    for _site, area, _line, _node, suffix, attr_keys in _every_process_node(datasets):
        expected = get_model(suffix, attr_keys, area)
        actual = sfc_config.resolve_model(suffix, attr_keys, area)
        assert actual == expected, (
            f"suffix={suffix!r} area={area!r} hasPump={any('Pump' in k for k in attr_keys)}: "
            f"resolve_model gave {actual!r}, pipeline getModel gives {expected!r}"
        )
        seen += 1
        families.add((suffix, area, any("Pump" in k for k in attr_keys)))
    # Guard the cases are actually derived, not an empty pass.
    assert seen >= 3 * len(sfc_config.LINES), "expected every site×line node covered"
    # Every distinct suffix the tag map defines must have been exercised.
    assert len({f[0] for f in families}) >= 15, "too few distinct tag families exercised"


def test_pump_variant_and_attr_conditional_branches_are_actually_hit(oracle, datasets):
    """The parity test passes vacuously if the hard branches never occur. Assert
    the pump-variant (both hasPump values) and the attr-conditional clause
    (yielding more than one model) are present in the derived corpus."""
    _get_model, tables = oracle
    attr_suffix = tables["attr_suffix"]
    got_pump_variant = got_pump_true = got_pump_false = False
    attr_models_seen: set[str] = set()
    for _site, area, _line, _node, suffix, attr_keys in _every_process_node(datasets):
        if suffix in tables["pump_variants"]:
            got_pump_variant = True
            if any("Pump" in k for k in attr_keys):
                got_pump_true = True
            else:
                got_pump_false = True
        if suffix == attr_suffix:
            attr_models_seen.add(sfc_config.resolve_model(suffix, attr_keys, area))
    assert got_pump_variant and got_pump_true, "no pump-bearing node in corpus"
    assert got_pump_false, "no non-pump node in corpus (both hasPump branches must occur)"
    assert len(attr_models_seen) >= 2, "attr-conditional clause must yield >1 model"


# ---------------------------------------------------------------------------
# Endpoint split (ADR-0082 decision 8).
# ---------------------------------------------------------------------------


def test_split_endpoint_matches_stack_shape():
    """Address keeps the scheme; Path carries no leading slash.

    Both are required by SFC: a schemeless Address fails with
    ``unsupported protocol: null``, and a leading slash yields ``…:26543//UA/…``
    because SFC joins the parts with a slash of its own. Verified by running the
    real binary against a generated config.
    """
    host = "opcua-nlb-abc123.elb.us-east-2.amazonaws.com"
    endpoint = f"opc.tcp://{host}:{sfc_config.DEFAULT_OPCUA_PORT}{sfc_config.DEFAULT_OPCUA_PATH}"
    address, port, path = sfc_config.split_endpoint(endpoint)
    assert address == f"opc.tcp://{host}"
    assert port == sfc_config.DEFAULT_OPCUA_PORT
    assert path == sfc_config.DEFAULT_OPCUA_PATH.lstrip("/")
    # The three parts must recompose into the endpoint SFC actually dials.
    assert f"{address}:{port}/{path}" == endpoint


def test_split_endpoint_rejects_non_opcua():
    with pytest.raises(ValueError):
        sfc_config.split_endpoint("https://example.invalid:443/x")


# ---------------------------------------------------------------------------
# Channel resolution: writability filter (ADR-0082 decision 2 / consequences).
# ---------------------------------------------------------------------------


def test_channels_only_target_modelled_properties(datasets):
    channels = sfc_config.resolve_channels(datasets)
    assert channels, "expected resolved channels"
    for ch in channels:
        allowed = sfc_config.MODEL_MEASUREMENTS[ch.model]
        assert ch.prop in allowed, (
            f"{ch.alias}: property {ch.prop!r} not on model {ch.model!r}"
        )
    # NodeId value equals the tag alias (STRING NodeId), and channel id is a
    # legal JSON/JMESPath key derived 1:1 from it.
    for ch in channels:
        assert "/" not in ch.channel_id
        assert ch.channel_id == ch.alias.replace("/", "_")


def test_line_metadata_channels_are_dropped(datasets):
    """Line-level nodes (suffix == the line name) resolve to a non-model and
    their channels (LineOutput/MaterialID/OrderID/Shift...) must be dropped —
    SFC never writes an unmodelled property (ADR-0082 consequences)."""
    channels = sfc_config.resolve_channels(datasets)
    assets = {ch.asset for ch in channels}
    for line in sfc_config.LINES:
        assert line not in assets, f"line-level node {line!r} leaked as a writable asset"


# ---------------------------------------------------------------------------
# Full config assembly (ADR-0082 decisions 2, 5, 6, 8).
# ---------------------------------------------------------------------------


#: The S3 Tables identity SFC's schema requires; in a deploy these come from
#: SfcS3TablesStack's exported attributes (ADR-0082 decision 4).
S3_IDENTITY = {
    "region": "us-east-2",
    "table_bucket": "sfc-telemetry",
    "namespace": "sfc",
    "table_name": "readings",
}


@pytest.fixture(scope="module")
def config(datasets):
    endpoints = {
        site.key: f"opc.tcp://host-{site.key}:{sfc_config.DEFAULT_OPCUA_PORT}{sfc_config.DEFAULT_OPCUA_PATH}"
        for site in sfc_config.SITES
    }
    return sfc_config.generate_sfc_config(endpoints, datasets=datasets, **S3_IDENTITY)


def test_one_opcua_server_per_site(config):
    servers = config["ProtocolAdapters"]["OPCUA"]["OpcuaServers"]
    assert set(servers) == {site.key for site in sfc_config.SITES}
    for site in sfc_config.SITES:
        s = servers[site.key]
        assert s["Address"] == f"opc.tcp://host-{site.key}"
        assert s["Port"] == sfc_config.DEFAULT_OPCUA_PORT
        assert s["Path"] == sfc_config.DEFAULT_OPCUA_PATH.lstrip("/")


def test_constant_columns_read_metadata_not_jmespath_literals(config):
    """SFC rejects a JMESPath literal, so every dimension column must be a path
    into declared Metadata — and that Metadata must actually exist."""
    table = config["Targets"]["s3tables-target"]["Tables"][0]
    for mapping in table["Mappings"]:
        for col in ("site", "area", "line", "asset", "channel", "quality"):
            vq = mapping[col]["ValueQuery"]
            assert ".metadata." in vq, f"{col} must read metadata, got {vq!r}"
            assert "`" not in vq and "'" not in vq, f"{col} uses a literal: {vq!r}"
    for source in config["Sources"].values():
        assert set(source["Metadata"]) == {"site", "area", "quality"}
        for chan in source["Channels"].values():
            assert set(chan["Metadata"]) == {"line", "asset", "channel"}


def test_literal_helper_refuses_to_be_used():
    """The old constant-literal helper must fail loudly, not silently regress."""
    with pytest.raises(NotImplementedError):
        sfc_config._literal("anything")


def test_channels_carry_string_nodeids(config):
    idx = sfc_config.DEFAULT_NAMESPACE_INDEX
    for source in config["Sources"].values():
        for cid, chan in source["Channels"].items():
            assert chan["NodeId"].startswith(f"ns={idx};s=")
            # The NodeId's s= value is the alias the channel id came from.
            alias = chan["NodeId"].split(";s=", 1)[1]
            assert alias.replace("/", "_") == cid.removesuffix(
                sfc_config.BOOL_NUMERIC_SUFFIX
            )
            # Unquoted JMESPath identifiers: the DataPaths address these ids.
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cid), cid


def test_sitewise_target_is_assets_mapping_mode(config):
    target = config["Targets"]["sitewise-target"]
    assert target["TargetType"] == "AWS-SITEWISE"
    assert "AssetCreation" not in target  # ADR-0082 decision 2: never AssetCreation
    assert target["Assets"], "expected SiteWise asset entries"
    for asset in target["Assets"]:
        # By external id, never by name: names repeat across sites (ADR-0082 d9).
        assert set(asset) == {"AssetExternalId", "Properties"}
        assert asset["Properties"]
        for prop in asset["Properties"]:
            assert re.fullmatch(
                r"@\.sources\.[\w-]+\.values\.[\w-]+\.value", prop["DataPath"]
            ), prop["DataPath"]


def test_s3tables_target_narrow_schema_and_one_mapping_per_channel(config):
    target = config["Targets"]["s3tables-target"]
    assert target["TargetType"] == "AWS-S3-TABLES"
    assert target["AutoCreate"] is False  # ADR-0082 decision 4
    # SFC's schema requires these at TARGET level, and the table config nested
    # under Tables[] — not Schema/Mappings/Partition on the target itself.
    assert target["TableBucket"] == S3_IDENTITY["table_bucket"]
    assert target["Namespace"] == S3_IDENTITY["namespace"]
    assert target["Region"] == S3_IDENTITY["region"]
    assert not {"Schema", "Mappings", "Partition"} & set(target)
    (table,) = target["Tables"]
    assert table["TableName"] == S3_IDENTITY["table_name"]
    names = [c["Name"] for c in table["Schema"]]
    assert names == ["event_time", "site", "area", "line", "asset", "channel", "value", "quality"]
    event_time = next(c for c in table["Schema"] if c["Name"] == "event_time")
    assert event_time["Type"] == "timestamptz"
    assert event_time["Optional"] is False
    assert table["Partition"] == {"day": "event_time"}


def test_s3tables_skips_mappings_whose_value_is_absent(config):
    """Every site's poll is evaluated against every site's mappings, and SFC
    writes a row for each mapping whose REQUIRED columns resolve. With only
    event_time required, 75% of the sandbox rows were all-null (2026-09-26).
    ``value`` must be required so an absent reading yields no row, and the
    expected skips must not each log a warning."""
    target = config["Targets"]["s3tables-target"]
    (table,) = target["Tables"]
    value = next(c for c in table["Schema"] if c["Name"] == "value")
    assert value["Optional"] is False
    assert target["WarnIfValueMissing"] is False


def test_s3tables_writes_are_batched_per_minute(config):
    # Each S3 Tables append is an Iceberg commit with a fixed cost of
    # 10-17 s measured live; SFC's ~100-record default kept two commits in
    # flight and fell behind. One commit per minute (or 5,000 records).
    target = config["Targets"]["s3tables-target"]
    assert target["Interval"] == sfc_config.S3_TABLES_INTERVAL_MS == 60_000
    assert target["BufferCount"] == sfc_config.S3_TABLES_BUFFER_COUNT
    assert target["BufferCount"] >= 1000


def test_sitewise_properties_do_not_warn_when_absent(config):
    # Every poll carries one site's values and SFC evaluates every asset's
    # DataPaths against it, so three sites' properties are absent on every
    # poll. A warning each was ~170 log lines/s live; keep them off.
    props = [p for a in config["Targets"]["sitewise-target"]["Assets"]
             for p in a["Properties"]]
    assert props
    assert all(p["WarnIfNotPresent"] is False for p in props)


def test_s3tables_quality_is_read_from_source_metadata_as_good(config):
    """SFC carries no OPC UA status to its targets, so a per-value quality path
    never resolves (every row was null on the 2026-09-26 sandbox run). Quality
    is a source-metadata constant, GOOD, read back by a real path."""
    (table,) = config["Targets"]["s3tables-target"]["Tables"]
    for source_id, source in config["Sources"].items():
        assert source["Metadata"]["quality"] == "GOOD"
    for mapping in table["Mappings"]:
        assert re.fullmatch(
            r"@\.sources\.[\w-]+\.metadata\.quality", mapping["quality"]["ValueQuery"]
        ), mapping["quality"]
    # One Mappings entry per NUMERIC reading: every non-STRING channel, a
    # BOOLEAN one through its BoolToNumber twin. The value column is a double.
    channels = _read_channels(config)
    numeric = [c for c in channels if channels[c]["dtype"] != "STRING"]
    assert len(table["Mappings"]) == len(numeric)
    value_paths = {m["value"]["ValueQuery"] for m in table["Mappings"]}
    for cid in numeric:
        src = channels[cid]["source"]
        table_ch = cid + (sfc_config.BOOL_NUMERIC_SUFFIX
                          if channels[cid]["dtype"] == "BOOLEAN" else "")
        assert f"@.sources.{src}.values.{table_ch}.value" in value_paths, cid
    # Every mapping value is a ColumnMappingConfiguration, never a bare string.
    for mapping in table["Mappings"]:
        for column, spec in mapping.items():
            assert isinstance(spec, dict), f"{column} must be a mapping config"
            assert "ValueQuery" in spec, column


def test_adapter_and_target_types_declare_factory_classes(config):
    """ProtocolAdapters references the OPCUA AdapterType, so it must exist."""
    assert config["ProtocolAdapters"]["OPCUA"]["AdapterType"] in config["AdapterTypes"]
    for block in (config["AdapterTypes"], config["TargetTypes"]):
        for name, spec in block.items():
            assert spec["JarFiles"], name
            assert spec["FactoryClassName"].startswith("com.amazonaws.sfc."), name


def test_unknown_endpoint_key_is_an_error(datasets):
    """A miskeyed endpoints map must not silently yield an empty config."""
    with pytest.raises(ValueError, match="unknown endpoint key"):
        sfc_config.generate_sfc_config(
            {"not-a-site-key": "opc.tcp://x:26543/UA/ManufacturingServer"},
            datasets=datasets,
            **S3_IDENTITY,
        )


def _read_channels(config):
    """``{channel_id: {"source", "dtype"}}`` for every OPC UA reading, i.e.
    every channel except the BoolToNumber twins, typed from the tag map."""
    types = {ch.channel_id: ch.dtype for ch in sfc_config.resolve_channels()}
    return {
        cid: {"source": src, "dtype": types[cid]}
        for src, s in config["Sources"].items()
        for cid in s["Channels"]
        if not cid.endswith(sfc_config.BOOL_NUMERIC_SUFFIX)
    }


def test_sitewise_property_count_equals_channel_count(config):
    total_props = sum(
        len(a["Properties"]) for a in config["Targets"]["sitewise-target"]["Assets"]
    )
    assert total_props == len(_read_channels(config))
    # SiteWise gets the RAW reading (a BOOLEAN property needs a boolean), never
    # the BoolToNumber twin.
    for a in config["Targets"]["sitewise-target"]["Assets"]:
        for p in a["Properties"]:
            assert sfc_config.BOOL_NUMERIC_SUFFIX not in p["DataPath"], p


def test_boolean_readings_reach_the_table_as_0_or_1(config):
    """P5: a BOOLEAN channel (BPFO_Alarm, Down, ...) is read twice — raw for
    SiteWise, through BoolToNumber for the double ``value`` column."""
    assert config["Transformations"] == {
        sfc_config.BOOL_TO_NUMBER: [{"Operator": "BoolToNumber"}]
    }
    channels = _read_channels(config)
    bools = [c for c in channels if channels[c]["dtype"] == "BOOLEAN"]
    assert any(c.endswith("_BPFO_Alarm") for c in bools)
    for cid in bools:
        src = config["Sources"][channels[cid]["source"]]["Channels"]
        twin = src[cid + sfc_config.BOOL_NUMERIC_SUFFIX]
        assert twin["NodeId"] == src[cid]["NodeId"]
        assert twin["Transformation"] == sfc_config.BOOL_TO_NUMBER
        assert "Transformation" not in src[cid]


def test_sites_match_the_opcua_servers():
    """Every OPC UA server the stack deploys is an SFC source, in stack order —
    the drift that left the fourth server out of SFC (ADR-0072) fails here."""
    stack_src = (Path(sfc_config._HIGHBYTE_DIR).parent / "stacks"
                 / "highbyte_opcua_stack.py").read_text()
    block = re.search(r"OPC_UA_SITES = \((.*?)\n    \)", stack_src, re.S).group(1)
    names = re.findall(r'\("(\w+)", "Opc\w+"\)', block)
    assert [s.key for s in sfc_config.SITES] == [n.lower() for n in names]


def test_story_site_carries_the_cmx441_story_asset(config):
    """The story mill is written to the SiteWise asset the rest of the workshop
    uses (condition_monitor.STORY_SITEWISE_ASSET), with its spectral bands."""
    datasets = sfc_config._load_by_path("gen_ds_cmx", sfc_config._DATASETS_PATH)
    story = datasets.CMX441_ASSET_NAME
    (site,) = [s.key for s in sfc_config.SITES if s.lines_fn]  # the ADR-0072 site
    (spec,) = [s for s in sfc_config.asset_tree() if s.name == story]
    assert spec.adopt_by_name  # reuses the asset the feed Lambda / seed create
    (asset,) = [a for a in config["Targets"]["sitewise-target"]["Assets"]
                if a["AssetExternalId"] == spec.external_id]
    props = {p["PropertyName"] for p in asset["Properties"]}
    assert {"Spectrum_1X", "Spectrum_BPFO", "Spectrum_BPFI", "CrestFactor",
            "Kurtosis", "BPFO_Alarm", "BPFO_Hz", "BPFI_Hz"} <= props
    assert all(f".sources.{site}." in p["DataPath"] for p in asset["Properties"])
    # No second asset for that mill on its line under its bare node name.
    assert [s for s in sfc_config.asset_tree()
            if s.parent == spec.parent and s.model == spec.model] == [spec]


def test_partial_endpoints_still_produce_valid_config(datasets):
    """A site absent from the endpoints map is skipped, not an error."""
    one = sfc_config.SITES[0].key
    endpoints = {one: f"opc.tcp://only:{sfc_config.DEFAULT_OPCUA_PORT}{sfc_config.DEFAULT_OPCUA_PATH}"}
    cfg = sfc_config.generate_sfc_config(endpoints, datasets=datasets, **S3_IDENTITY)
    assert set(cfg["Sources"]) == {one}
    assert set(cfg["ProtocolAdapters"]["OPCUA"]["OpcuaServers"]) == {one}


def test_config_is_json_serialisable(config):
    json.dumps(config)  # must not raise


# ---------------------------------------------------------------------------
# One-time history replay: a second source + "Channel"-timestamped schedule
# for the story mill's history nodes (the replay ADR). Off by default, so the
# live schedules and every other site are byte-identical.
# ---------------------------------------------------------------------------

STORY_SITE = sfc_config.STORY_SITE_KEY
HISTORY_SOURCE = sfc_config.HISTORY_SOURCE_ID
HISTORY_SCHEDULE = f"{HISTORY_SOURCE}-schedule"
LIVE_STORY_SCHEDULE = f"{STORY_SITE}-schedule"
_HISTORY_PATH = re.escape(f"@.sources.{HISTORY_SOURCE}.values.")


@pytest.fixture(scope="module")
def backfill_config(datasets):
    endpoints = {
        site.key: f"opc.tcp://host-{site.key}:{sfc_config.DEFAULT_OPCUA_PORT}{sfc_config.DEFAULT_OPCUA_PATH}"
        for site in sfc_config.SITES
    }
    return sfc_config.generate_sfc_config(endpoints, datasets=datasets, backfill=True, **S3_IDENTITY)


def _story_channels(datasets):
    """The story site's story-mill channels (node Line3CNC1 on Line3)."""
    return [ch for ch in sfc_config.resolve_channels(datasets)
            if ch.site == STORY_SITE and ch.node == "Line3CNC1"]


def test_backfill_off_is_byte_identical(config, backfill_config):
    # The default config has no history source, schedule, targets or mappings.
    assert HISTORY_SOURCE not in config["Sources"]
    assert all(s["Name"] != HISTORY_SCHEDULE for s in config["Schedules"])
    assert set(config["Targets"]) == {"sitewise-target", "s3tables-target"}
    assert HISTORY_SOURCE not in json.dumps(config)
    # And every live schedule keeps TimestampLevel unset (SFC default None).
    for sched in config["Schedules"]:
        assert "TimestampLevel" not in sched


def test_the_replay_adds_no_work_to_the_live_targets(config, backfill_config):
    """SFC's SiteWise writer walks every configured property, and its S3
    Tables writer every mapping, for each value it gets: the live targets,
    schedules and sources are exactly the replay-off ones."""
    for target in ("sitewise-target", "s3tables-target"):
        assert backfill_config["Targets"][target] == config["Targets"][target], target
    live = [s for s in backfill_config["Schedules"] if s["Name"] != HISTORY_SCHEDULE]
    assert live == config["Schedules"]
    assert {k: v for k, v in backfill_config["Sources"].items() if k != HISTORY_SOURCE} == config["Sources"]


def test_history_writes_only_through_its_own_targets(backfill_config):
    sched = next(s for s in backfill_config["Schedules"] if s["Name"] == HISTORY_SCHEDULE)
    assert sched["Targets"] == [sfc_config.SITEWISE_HISTORY_TARGET, sfc_config.S3TABLES_HISTORY_TARGET]
    for s in backfill_config["Schedules"]:
        if s["Name"] != HISTORY_SCHEDULE:
            assert not set(s["Targets"]) & {sfc_config.SITEWISE_HISTORY_TARGET, sfc_config.S3TABLES_HISTORY_TARGET}


def test_the_history_sitewise_target_sends_its_last_partial_batch(config, backfill_config):
    """SFC's SiteWise target sends every 10 reads, and on a timer only when
    Interval is set; the replay ends, so without a timer its last partial
    batch is never sent. The live target keeps SFC's default (unchanged)."""
    interval = backfill_config["Targets"][sfc_config.SITEWISE_HISTORY_TARGET]["Interval"]
    assert 10 <= interval <= 10_000
    assert "Interval" not in config["Targets"]["sitewise-target"]
    assert "Interval" not in backfill_config["Targets"]["sitewise-target"]


def test_history_source_reads_the_history_nodes_on_the_same_server(backfill_config, datasets):
    src = backfill_config["Sources"][HISTORY_SOURCE]
    assert src["AdapterOpcuaServer"] == STORY_SITE   # same server, second source
    story = _story_channels(datasets)
    assert story, "expected story-mill channels"
    # Every history channel addresses "<history root>/<a story-mill leaf alias>",
    # a root folder outside the site's branch (a branch reader never sees it).
    live_aliases = {ch.alias for ch in story}
    prefix = f"ns={sfc_config.DEFAULT_NAMESPACE_INDEX};s={sfc_config.HISTORY_ROOT}/"
    for cid, chan in src["Channels"].items():
        node_id = chan["NodeId"]
        assert node_id.startswith(prefix), node_id
        assert node_id[len(prefix):] in live_aliases, node_id
        assert not node_id[len(prefix):].startswith(sfc_config.HISTORY_ROOT)
    # One history channel per story channel (plus BoolToNumber twins for S3).
    base = [c for c in src["Channels"] if not c.endswith(sfc_config.BOOL_NUMERIC_SUFFIX)]
    assert len(base) == len(story)


def test_history_schedule_is_channel_timestamped_and_fast(backfill_config):
    sched = next(s for s in backfill_config["Schedules"] if s["Name"] == HISTORY_SCHEDULE)
    assert sched["Interval"] == 1000                      # faster than the 5 s live schedule
    assert sched["TimestampLevel"] == "Channel"           # each point lands at its SourceTimestamp
    assert set(sched["Sources"]) == {HISTORY_SOURCE}
    # the story site's live schedule is untouched (no TimestampLevel)
    live = next(s for s in backfill_config["Schedules"] if s["Name"] == LIVE_STORY_SCHEDULE)
    assert "TimestampLevel" not in live


def test_history_sitewise_twins_write_the_same_asset_at_history_timestamps(config, backfill_config, datasets):
    story = _story_channels(datasets)
    (ext_id,) = {ch.asset_external_id for ch in story}    # one story-mill asset
    live = next(a for a in config["Targets"]["sitewise-target"]["Assets"] if a["AssetExternalId"] == ext_id)
    live_props = {p["PropertyName"] for p in live["Properties"]}
    (asset,) = backfill_config["Targets"][sfc_config.SITEWISE_HISTORY_TARGET]["Assets"]
    assert asset["AssetExternalId"] == ext_id, "the history target writes the story mill only"
    hist_props = asset["Properties"]
    assert {p["PropertyName"] for p in hist_props} == live_props   # the same properties as the live feed
    for p in hist_props:
        assert re.fullmatch(_HISTORY_PATH + r"[\w-]+\.value", p["DataPath"])
        # an explicit TimestampPath to the channel's historical time
        assert p["TimestampPath"] == p["DataPath"].rsplit(".value", 1)[0] + ".timestamp"


def test_history_s3_rows_go_to_the_replay_table_with_their_ingest_time(backfill_config):
    target = backfill_config["Targets"][sfc_config.S3TABLES_HISTORY_TARGET]
    assert target["TargetType"] == "AWS-S3-TABLES" and target["AutoCreate"] is False
    (table,) = target["Tables"]
    assert table["TableName"] == sfc_config.REPLAY_TABLE_NAME == "readings_replay"
    assert table["Schema"] == [*sfc_config.S3_TABLE_SCHEMA,
                               {"Name": "ingest_time", "Type": "timestamptz", "Optional": False}]
    assert table["Partition"] == sfc_config.S3_TABLE_PARTITION
    assert table["Mappings"], "expected history S3 mappings"
    for m in table["Mappings"]:
        assert f".sources.{HISTORY_SOURCE}." in m["value"]["ValueQuery"]
        # event_time is the channel's historical timestamp; ingest_time SFC's clock
        assert re.fullmatch(_HISTORY_PATH + r"[\w-]+\.timestamp", m["event_time"]["ValueQuery"]), m
        assert m["ingest_time"] == {"ValueQuery": "@.timestamp"}


def test_sfc_is_still_the_only_sitewise_writer_with_backfill(backfill_config):
    # the live pair plus the history pair; no new writer type sneaks in.
    assert set(backfill_config["Targets"]) == {"sitewise-target", "s3tables-target",
                                               sfc_config.SITEWISE_HISTORY_TARGET,
                                               sfc_config.S3TABLES_HISTORY_TARGET}
    types = {t["TargetType"] for t in backfill_config["Targets"].values()}
    assert types == {"AWS-SITEWISE", "AWS-S3-TABLES"}
    assert set(backfill_config["TargetTypes"]) == types



# ---------------------------------------------------------------------------
# The replay table's contract: a replay that runs again (an SFC restart) or a
# Reset and a new placement leaves copies in S3 Tables (append-only); the
# documented query gives one reading per key, and the same one SiteWise keeps
# (the last write of an equal timestamp wins there).
# ---------------------------------------------------------------------------

_KEY = ("site", "area", "line", "asset", "channel", "event_time")


def _era_rows(anchor: int, frm: int, to: int, ingest_from: int, channels=("Spectrum_BPFO", "BPFO_Alarm")):
    """The rows one replay pass writes: each grid epoch of [frm, to) on the
    anchor's grid, a value that depends on the era, ingested in send order."""
    grid = 300
    epochs = range(frm + (anchor - frm) % grid, to, grid)
    return [(e, STORY_SITE, "Machining", "Line3", "CMX-441", ch, float((anchor // grid + e // grid) % 97),
             "GOOD", ingest_from + 2 * k) for k, e in enumerate(epochs) for ch in channels]


def _with_table(template: str, table: str) -> str:
    """Put the fixed replay table name in a test statement's ``{table}`` slot."""
    return template.format(table=table)


def _replay_readings(rows):
    import sqlite3
    table = sfc_config.REPLAY_TABLE_NAME
    db = sqlite3.connect(":memory:")
    db.execute(_with_table("CREATE TABLE {table} "
                           "(event_time, site, area, line, asset, channel, value, quality, ingest_time)", table))
    db.executemany(_with_table("INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?)", table), rows)
    out = db.execute(_with_table(sfc_config.REPLAY_READINGS_SQL, table)).fetchall()
    db.close()
    return out


def _sitewise_after(rows):
    """What SiteWise holds after the same writes: the last value per timestamp."""
    held = {}
    for row in sorted(rows, key=lambda r: r[-1]):
        held[(row[5], row[0])] = row[6]
    return held


def test_a_restarted_replay_leaves_one_reading_per_key():
    a = 1_790_000_010
    first = _era_rows(a, a - 3 * 86400, a - 600, ingest_from=a + 100)
    again = _era_rows(a, a - 3 * 86400, a - 600, ingest_from=a + 900)     # SFC restarted: all again
    readings = _replay_readings(first + first[:40] + again)
    keys = [r[:6] for r in readings]
    assert len(keys) == len(set(keys)) == len(first)
    assert {(r[5], r[0]): r[6] for r in readings} == _sitewise_after(first + again)


def test_a_reset_and_a_new_placement_leave_the_new_eras_readings():
    a = 1_790_000_010
    old = _era_rows(a, a - 3 * 86400, a - 600, ingest_from=a + 100)
    b = a + 86400 + 3000                                                  # the next era, a day later
    new = _era_rows(b, b - 3 * 86400, b - 600, ingest_from=b + 100)
    readings = _replay_readings(old + new)
    by_key = {(r[5], r[0]): r[6] for r in readings}
    assert len(readings) == len(by_key) == len({(r[5], r[0]) for r in old + new})
    assert by_key == _sitewise_after(old + new), "the S3 readings agree with what SiteWise kept"
    newest = {(r[5], r[0]): r[6] for r in new}
    assert all(by_key[k] == v for k, v in newest.items()), "the new era wins where both replayed"
