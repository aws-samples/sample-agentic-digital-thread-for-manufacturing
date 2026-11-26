# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The settings-seed init container's script, run inside the REAL images it
ships on, the way ECS runs it (``/bin/sh -c`` with the stack's env).

The ordinary ``deploy.sh --highbyte`` deployment runs the seed on BusyBox,
which the customer's own account pulls from Amazon Elastic Container Registry
Public (Amazon ECR Public); the Workshop Studio package runs it on the
HighByte hub image itself and never carries BusyBox. The hub image runs as
uid 1000, BusyBox as root, and
the script must behave the same on both: seed on first boot, enforce
allowAnonymous=false on an existing file, and FAIL CLOSED (nonzero exit,
original untouched) on a file it does not recognise.

The appData directory is world-writable, as the EFS access point creates it
(owner 1000:1000, 0777); on EFS every file operation then runs as uid 1000
whatever the container user is (posix_user on the access point). That EFS
behaviour itself is NOT exercised here; a real account deploy is.

Uses only images already on this machine at their pinned digests
(``--pull never``, ``--network none``); skips when Docker or the image is
absent (the CI image has no container runtime).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv, no shell=True, internal inputs only
from pathlib import Path

import pytest

from stacks import highbyte_opcua_stack as hb

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_SETTINGS = (REPO_ROOT / "highbyte" / "components" / "highbyte-settings"
                 / "intelligencehub-settings.json")
#: Where the stack mounts the EFS volume and points SETTINGS_FILE.
APP_DATA = "/usr/local/highbyte/appData"
SETTINGS_FILE = f"{APP_DATA}/intelligencehub-settings.json"
#: The hub task's platform (packaging_deploy: the pinned hub tag is amd64 only).
PLATFORM = "linux/amd64"


def _pins() -> dict:
    """stacks/_external_images.EXTERNAL_IMAGES: both images the seed runs on
    (the workshop's own pins hold the hub alone)."""
    path = REPO_ROOT / "stacks" / "_external_images.py"
    spec = importlib.util.spec_from_file_location("adt_test_external_image_pins", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EXTERNAL_IMAGES


def _candidates(name: str) -> list[str]:
    """Local refs that ARE the pinned image: the pinned source repo (and, for
    the official busybox, its Docker Hub name, which carries the same index
    digest) at the pinned index digest."""
    pin = _pins()[name]
    repo = pin["source"].rsplit(":", 1)[0]
    refs = [f"{repo}@{pin['index_digest']}"]
    if repo == "public.ecr.aws/docker/library/busybox":
        refs.append(f"busybox@{pin['index_digest']}")
    return refs


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        pytest.skip("docker not on PATH")
    return path


def _local_image(name: str) -> str:
    """The first candidate ref present locally for PLATFORM, else skip."""
    docker = _docker()
    for ref in _candidates(name):
        result = subprocess.run(  # nosec B603 - fixed argv list, no shell
            [docker, "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", ref],
            capture_output=True, text=True, timeout=60, check=False)
        if result.returncode == 0 and result.stdout.strip() == PLATFORM:
            return ref
    pytest.skip(f"no local {PLATFORM} copy of the pinned {name} image ({_candidates(name)})")


@pytest.fixture(params=["highbyte-hub", "settings-seed"], ids=["hub-image", "busybox-image"])
def seed_image(request) -> tuple[str, str]:
    return request.param, _local_image(request.param)


def _run(image: str, app_data: Path, script: str = hb._SETTINGS_SEED_SCRIPT) -> subprocess.CompletedProcess:
    """Run *script* in *image* like the ECS seed container: entry point
    ``/bin/sh -c``, image default user, the stack's env, appData mounted."""
    env = {**os.environ,
           "SETTINGS_FILE": SETTINGS_FILE,
           "HB_SETTINGS_JSON": REPO_SETTINGS.read_text(encoding="utf-8"),
           "SETTINGS_PATCH_AWK": hb._SETTINGS_PATCH_AWK}
    return subprocess.run(  # nosec B603 - fixed argv list, no shell
        [_docker(), "run", "--rm", "--pull", "never", "--network", "none",
         "--platform", PLATFORM, "-v", f"{app_data}:{APP_DATA}",
         "-e", "SETTINGS_FILE", "-e", "HB_SETTINGS_JSON", "-e", "SETTINGS_PATCH_AWK",
         "--entrypoint", "/bin/sh", image, "-c", script],
        env=env, capture_output=True, text=True, timeout=120, check=False)


def _app_data(tmp_path: Path, content: str | None = None) -> Path:
    """An appData dir like the EFS access point root (world-writable), with
    an existing settings file when *content* is given."""
    app_data = tmp_path / "appData"
    app_data.mkdir()
    app_data.chmod(0o777)  # nosec B103 - mirrors the access point's create_acl 0777
    if content is not None:
        (app_data / "intelligencehub-settings.json").write_text(content, encoding="utf-8")
    return app_data


def _reconcile_module():
    """tests/test_highbyte_settings_reconcile.py: the fixtures the awk/script
    unit tests use (loaded by path, so the two test files share one copy)."""
    path = Path(__file__).with_name("test_highbyte_settings_reconcile.py")
    spec = importlib.util.spec_from_file_location("adt_test_settings_reconcile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _persisted_true() -> str:
    """The trimmed 4.4.2-persisted settings fixture the awk/script unit tests use."""
    return _reconcile_module().PERSISTED_TRUE


def test_the_hub_image_runs_the_seed_as_uid_1000(seed_image, tmp_path):
    """The hub image's default user is uid 1000 (busybox's is root), so the
    workshop's seed runs unprivileged: the cases below prove that is enough."""
    name, image = seed_image
    result = _run(image, _app_data(tmp_path), script="id -u")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("1000" if name == "highbyte-hub" else "0")


def test_first_boot_seeds_the_settings(seed_image, tmp_path):
    _, image = seed_image
    app_data = _app_data(tmp_path)
    result = _run(image, app_data)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "First boot" in result.stdout
    seeded = app_data / "intelligencehub-settings.json"
    assert seeded.read_text(encoding="utf-8") == REPO_SETTINGS.read_text(encoding="utf-8")
    assert json.loads(seeded.read_text(encoding="utf-8"))["settings"]["restDataServer"]["mcpConfigTools"] \
        == "disabled"
    assert (app_data / "license").is_dir()


def test_an_existing_file_gets_allow_anonymous_false(seed_image, tmp_path):
    """Existing EFS file (a hub that already booted): the REST/MCP flag is
    forced false, the MQTT flag and every other setting are kept, and the
    temp file is gone."""
    _, image = seed_image
    app_data = _app_data(tmp_path, _persisted_true())
    result = _run(image, app_data)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "allowAnonymous=false enforced" in result.stdout
    out = json.loads((app_data / "intelligencehub-settings.json").read_text(encoding="utf-8"))
    assert out["settings"]["httpServer"]["allowAnonymous"] is False
    assert out["settings"]["hbMqttBroker"]["allowAnonymous"] is True
    assert out["settings"]["httpServer"]["port"] == 8885
    assert not (app_data / "intelligencehub-settings.json.tmp").exists()


def test_an_unrecognised_file_fails_closed_and_is_left_untouched(seed_image, tmp_path):
    """Minified JSON is not the recognised format: nonzero exit (the hub's
    SUCCESS dependency keeps it down), the reason logged, the original file
    byte-for-byte unchanged and no temp file left."""
    _, image = seed_image
    minified = json.dumps(json.loads(_persisted_true()), separators=(",", ":"))
    app_data = _app_data(tmp_path, minified)
    result = _run(image, app_data)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "FAILED CLOSED" in result.stderr and "REFUSING" in result.stderr, result.stderr
    assert (app_data / "intelligencehub-settings.json").read_text(encoding="utf-8") == minified
    assert not (app_data / "intelligencehub-settings.json.tmp").exists()


def test_both_images_write_the_same_patched_bytes(tmp_path):
    """The two seed images (the workshop's hub image and the ordinary
    deploy's BusyBox) produce byte-identical files from the same existing
    settings."""
    rec = _reconcile_module()
    for label, text in [("trimmed", _persisted_true())] + [(v, rec.persisted(v)) for v in rec.present_versions()]:
        outputs = []
        for name in ("highbyte-hub", "settings-seed"):
            image = _local_image(name)
            target = tmp_path / label / name
            target.mkdir(parents=True)
            app_data = _app_data(target, text)
            result = _run(image, app_data)
            assert result.returncode == 0, (label, name, result.stderr)
            outputs.append((app_data / "intelligencehub-settings.json").read_bytes())
        assert outputs[0] == outputs[1], label


# ---- MCP Configuration Tools on an existing file ---------------------------- #

@pytest.fixture(params=["4.4.2", "4.5.3"], ids=["persisted-4.4.2", "persisted-4.5.3"])
def hub_persisted(request) -> str:
    return request.param


def test_an_upgraded_hub_gets_config_tools_off_and_every_other_byte_kept(seed_image, hub_persisted, tmp_path):
    """A hub that already booted keeps its persisted settings on EFS, so the
    shipped file's mcpConfigTools "disabled" never reaches it: a 4.4.2 hub
    persisted serversConfig.mcpServer.configTools "enabled", and 4.5.3 keeps it
    across the upgrade (local probe, 2026-10-11), listing the config_* write
    tools. The seed turns it off together with allowAnonymous, in the image it
    runs on, and every other byte (no final newline included) stays."""
    _, image = seed_image
    rec = _reconcile_module()
    original = rec.persisted(hub_persisted)
    app_data = _app_data(tmp_path, original)
    result = _run(image, app_data)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "MCP Configuration Tools disabled" in result.stdout
    installed = (app_data / "intelligencehub-settings.json").read_text(encoding="utf-8")
    assert installed == rec.reconciled(original)
    out = json.loads(installed)["settings"]
    assert out["serversConfig"]["mcpServer"]["configTools"] == "disabled"
    assert out["httpServer"]["allowAnonymous"] is False
    assert not (app_data / "intelligencehub-settings.json.tmp").exists()


def test_an_already_reconciled_hub_file_is_left_byte_identical(seed_image, hub_persisted, tmp_path):
    _, image = seed_image
    already = _reconcile_module().reconciled(_reconcile_module().persisted(hub_persisted))
    app_data = _app_data(tmp_path, already)
    result = _run(image, app_data)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (app_data / "intelligencehub-settings.json").read_text(encoding="utf-8") == already


def test_an_unrebooted_first_boot_file_is_turned_off_in_the_input_shape(seed_image, tmp_path):
    """A first-boot file the hub never re-serialized, written by a package
    whose settings had the tools "enabled": the input-shape key is turned off
    and the result is exactly the shipped file."""
    _, image = seed_image
    shipped = REPO_SETTINGS.read_text(encoding="utf-8")
    enabled = shipped.replace('"mcpConfigTools" : "disabled"', '"mcpConfigTools" : "enabled"')
    assert enabled != shipped
    app_data = _app_data(tmp_path, enabled)
    result = _run(image, app_data)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (app_data / "intelligencehub-settings.json").read_text(encoding="utf-8") == shipped


@pytest.mark.parametrize("case", ["neither-shape", "section-without-key", "unknown-value", "minified-hub-file"])
def test_an_unrecognised_config_tools_file_fails_closed(seed_image, tmp_path, case):
    """No Configuration Tools setting the seed can turn off: nonzero exit, the
    reason logged, the original byte-for-byte unchanged, no temp file."""
    _, image = seed_image
    rec = _reconcile_module()
    content = {
        "neither-shape": lambda: rec.NO_TOOLS_SECTION,
        "section-without-key": lambda: rec.persisted("4.5.3").replace('        "configTools" : "enabled",\n', ""),
        "unknown-value": lambda: rec.persisted("4.4.2").replace('"configTools" : "enabled"', '"configTools" : "on"'),
        "minified-hub-file": lambda: json.dumps(json.loads(rec.persisted("4.5.3")), separators=(",", ":")),
    }[case]()
    app_data = _app_data(tmp_path, content)
    result = _run(image, app_data)
    assert result.returncode != 0, (case, result.stdout, result.stderr)
    assert "FAILED CLOSED" in result.stderr and "REFUSING" in result.stderr, result.stderr
    assert (app_data / "intelligencehub-settings.json").read_text(encoding="utf-8") == content
    assert not (app_data / "intelligencehub-settings.json.tmp").exists()
