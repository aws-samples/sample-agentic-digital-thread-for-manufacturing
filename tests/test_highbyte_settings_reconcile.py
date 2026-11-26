# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the settings-seed init container's reconcile of an existing
settings file (stacks/highbyte_opcua_stack: _SETTINGS_PATCH_AWK and the
container script _SETTINGS_SEED_SCRIPT).

On an UPDATE of an existing EFS-backed hub the settings file already exists.
HighByte 4.4.2 has no supported settings API, so the init container
reconciles the two security-critical settings -- the REST/MCP server's
allowAnonymous and MCP Configuration Tools -- and must FAIL CLOSED: accept an
existing false / "disabled", replace a true / "enabled", keep every other
byte, and exit nonzero (leaving the original file untouched, so the hub's
SUCCESS dependency keeps it from starting) whenever a section, key or format
is not recognised. These tests run the exact awk program and the exact shell
script with the system ``awk``/``sh`` (POSIX features only; the busybox and
4.5.3 hub images run them in test_highbyte_settings_seed_images.py).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess  # nosec B404 - fixed argv, no shell=True, internal inputs only
from pathlib import Path

import pytest

from stacks import highbyte_opcua_stack as hb

AWK = hb._SETTINGS_PATCH_AWK
REPO_SETTINGS = (Path(__file__).resolve().parents[1] / "highbyte" / "components"
                 / "highbyte-settings" / "intelligencehub-settings.json")
#: Settings files exactly as a local hub persisted them (2026-10-11), the bytes
#: unchanged (neither ends with a newline): 4.4.2 after a boot seeded with
#: restDataServer.allowAnonymous=true and mcpConfigTools=enabled, and the same
#: appData after the 4.5.3 upgrade migrated it (settings version 2,
#: toolRefreshPeriod). Both carry httpServer.allowAnonymous=true and
#: serversConfig.mcpServer.configTools="enabled".
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "highbyte_settings"
PERSISTED = {"4.4.2": FIXTURES / "persisted-4.4.2.json", "4.5.3": FIXTURES / "persisted-4.5.3.json"}

#: The two lines the reconcile changes in a hub-persisted file (the REST/MCP
#: server's allowAnonymous, located by the key after it, and the persisted
#: Configuration Tools key), and what it changes them to.
_ANON_TRUE = '      "allowAnonymous" : true,\n      "authenticationModes"'
_ANON_FALSE = '      "allowAnonymous" : false,\n      "authenticationModes"'
_TOOLS_ON = '"configTools" : "enabled"'
_TOOLS_OFF = '"configTools" : "disabled"'


def persisted(version: str) -> str:
    """The *version* hub-persisted settings file, as text. The fixtures are
    files a HighByte hub wrote, so ``.gitattributes`` keeps them out of an
    exported archive; where one is absent, the calling test skips."""
    path = PERSISTED[version]
    if not path.is_file():
        pytest.skip(f"{path.name} is not in this tree: it is a file a HighByte hub wrote, "
                    "left out of exported archives by .gitattributes export-ignore")
    return path.read_text(encoding="utf-8")


def present_versions() -> list[str]:
    """The versions whose hub-persisted fixture is in this tree, sorted."""
    return sorted(v for v, path in PERSISTED.items() if path.is_file())


def reconciled(text: str) -> str:
    """*text* with exactly the two reconciled lines changed and every other
    byte kept: what the seed must install for a hub-persisted file."""
    assert text.count(_ANON_TRUE) == 1 and text.count(_TOOLS_ON) == 1, "not a hub-persisted fixture"
    return text.replace(_ANON_TRUE, _ANON_FALSE).replace(_TOOLS_ON, _TOOLS_OFF)


#: How 4.4.2 persists the settings after a boot seeded with
#: restDataServer.allowAnonymous=true (verified on a local container), trimmed:
#: the REST/MCP flag under a top-level httpServer block, the MQTT flag under
#: hbMqttBroker, serversConfig.mcpServer with its configTools setting, and
#: serversConfig.restDataServer with no allowAnonymous.
PERSISTED_TRUE = """{
  "settings" : {
    "version" : 1,
    "hbMqttBroker" : {
      "enabled" : true,
      "allowAnonymous" : true,
      "endpoints" : [ {
        "hostname" : "0.0.0.0",
        "port" : 1883
      }, {
        "hostname" : "0.0.0.0",
        "port" : 1884
      } ]
    },
    "httpServer" : {
      "hostname" : "0.0.0.0",
      "port" : 8885,
      "allowAnonymous" : true,
      "authenticationModes" : [ "API_KEY" ]
    },
    "serversConfig" : {
      "mcpServer" : {
        "enabled" : true,
        "configTools" : "enabled"
      },
      "restDataServer" : {
        "enabled" : true
      }
    }
  }
}"""
#: PERSISTED_TRUE without its serversConfig.mcpServer block: Configuration
#: Tools in neither shape.
_MCP_SERVER_BLOCK = ('      "mcpServer" : {\n        "enabled" : true,\n'
                     '        "configTools" : "enabled"\n      },\n')
NO_TOOLS_SECTION = PERSISTED_TRUE.replace(_MCP_SERVER_BLOCK, "")


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} not on PATH")
    return path


def _awk(text: str, final_nl: int | None = None) -> subprocess.CompletedProcess:
    args = [] if final_nl is None else ["-v", f"final_nl={final_nl}"]
    return subprocess.run(  # nosec B603 - fixed argv list, no shell
        [_tool("awk"), *args, AWK], input=text, capture_output=True, text=True, timeout=30, check=False)


def _ok(text: str) -> dict:
    result = _awk(text)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _refused(text: str) -> str:
    result = _awk(text)
    assert result.returncode != 0, "the patch accepted an unrecognised settings file"
    assert "REFUSING" in result.stderr, result.stderr
    return result.stderr


# ---- the awk program ------------------------------------------------------- #

def test_http_server_anonymous_true_is_replaced_and_mqtt_is_untouched():
    out = _ok(PERSISTED_TRUE)["settings"]
    assert out["httpServer"]["allowAnonymous"] is False
    assert out["hbMqttBroker"]["allowAnonymous"] is True
    assert out["httpServer"]["port"] == 8885
    assert out["httpServer"]["authenticationModes"] == ["API_KEY"]
    assert out["serversConfig"]["restDataServer"]["enabled"] is True


def test_an_existing_false_is_accepted_unchanged():
    already = PERSISTED_TRUE.replace('"port" : 8885,\n      "allowAnonymous" : true',
                                     '"port" : 8885,\n      "allowAnonymous" : false')
    already = already.replace(_TOOLS_ON, _TOOLS_OFF)
    result = _awk(already)
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == already.rstrip("\n")


@pytest.mark.parametrize("indent", ["\t", " "], ids=["tabs", "one-space"])
def test_alternate_indentation_is_still_enforced(indent):
    """The section is tracked by brace depth, not by a fixed indent, so a
    re-indented file is still patched (true -> false)."""
    text = "\n".join(indent * (len(l) - len(l.lstrip(" ")) // 2) + l.lstrip(" ")
                     for l in PERSISTED_TRUE.splitlines())
    assert _ok(text)["settings"]["httpServer"]["allowAnonymous"] is False


def test_the_repo_seed_format_is_accepted():
    """A hub that never re-serialized its first-boot seed (our restDataServer
    input schema, 2-space indent, already false) passes unchanged."""
    text = REPO_SETTINGS.read_text(encoding="utf-8")
    out = _ok(text)
    assert out["settings"]["restDataServer"]["allowAnonymous"] is False


def test_rest_data_server_section_true_is_replaced():
    text = '{\n  "settings" : {\n    "restDataServer" : {\n      "port" : 8885,\n' \
           '      "allowAnonymous" : true,\n      "mcpConfigTools" : "disabled"\n    }\n  }\n}'
    assert _ok(text)["settings"]["restDataServer"]["allowAnonymous"] is False


def test_minified_json_is_refused():
    """One-line JSON is valid but not the recognised one-key-per-line format:
    the old patch left allowAnonymous=true and exited 0 (fail open)."""
    minified = json.dumps(json.loads(PERSISTED_TRUE), separators=(",", ":"))
    _refused(minified)


def test_missing_allow_anonymous_key_is_refused():
    missing = PERSISTED_TRUE.replace('      "allowAnonymous" : true,\n      "authenticationModes"',
                                     '      "authenticationModes"')
    assert '"httpServer"' in missing and missing.count("allowAnonymous") == 1
    assert "httpServer has no allowAnonymous" in _refused(missing)


@pytest.mark.parametrize("value_line", [
    '      "schemeCertificate" : "allowAnonymous",',
    '      "label" : "an \\"allowAnonymous\\" note",',
], ids=["string-value", "escaped-string"])
def test_allow_anonymous_as_a_string_value_is_not_the_key(value_line):
    """The word "allowAnonymous" inside a string VALUE of a target
    section is not the property; only the property name is matched, so the
    file is accepted (true -> false on the real key) instead of being refused."""
    text = PERSISTED_TRUE.replace('      "hostname" : "0.0.0.0",\n      "port" : 8885,',
                                  '      "hostname" : "0.0.0.0",\n' + value_line + '\n      "port" : 8885,')
    assert value_line in text
    out = _ok(text)["settings"]["httpServer"]
    assert out["allowAnonymous"] is False
    assert "allowAnonymous" in json.dumps(out["schemeCertificate"] if "schemeCertificate" in out else out["label"])


def test_unrecognised_value_is_refused():
    odd = PERSISTED_TRUE.replace('"port" : 8885,\n      "allowAnonymous" : true',
                                 '"port" : 8885,\n      "allowAnonymous" : "yes"')
    _refused(odd)


def test_no_target_section_is_refused():
    _refused('{\n  "settings" : {\n    "hbMqttBroker" : {\n      "allowAnonymous" : true\n    }\n  }\n}')


# ---- MCP Configuration Tools ------------------------------------------------ #

@pytest.mark.parametrize("version", sorted(PERSISTED))
def test_a_hub_persisted_file_gets_config_tools_off_and_keeps_every_other_byte(version):
    """The file a hub wrote (4.4.2, or 4.5.3 after the upgrade): the persisted
    serversConfig.mcpServer.configTools "enabled" becomes "disabled" and the
    REST/MCP allowAnonymous true becomes false, in ONE run, and nothing else
    changes, down to the missing final newline."""
    text = persisted(version)
    assert not text.endswith("\n")
    result = _awk(text, final_nl=0)
    assert result.returncode == 0, result.stderr
    assert result.stdout == reconciled(text)
    out = json.loads(result.stdout)["settings"]
    assert out["serversConfig"]["mcpServer"]["configTools"] == "disabled"
    assert out["httpServer"]["allowAnonymous"] is False
    assert out["hbMqttBroker"]["allowAnonymous"] is True


@pytest.mark.parametrize("version", sorted(PERSISTED))
def test_an_already_reconciled_file_comes_out_byte_identical(version):
    already = reconciled(persisted(version))
    result = _awk(already, final_nl=0)
    assert result.returncode == 0, result.stderr
    assert result.stdout == already


def test_the_shipped_file_is_byte_identical_and_its_enabled_form_is_turned_off():
    """The input shape (settings.restDataServer.mcpConfigTools), as a first
    boot writes it, on a hub that never re-serialized it: the shipped file
    (already off, ends with a newline) passes byte-identical, and the same
    file with the tools "enabled" comes out as the shipped file."""
    shipped = REPO_SETTINGS.read_text(encoding="utf-8")
    assert shipped.endswith("\n") and '"mcpConfigTools" : "disabled"' in shipped
    assert _awk(shipped, final_nl=1).stdout == shipped
    enabled = shipped.replace('"mcpConfigTools" : "disabled"', '"mcpConfigTools" : "enabled"')
    result = _awk(enabled, final_nl=1)
    assert result.returncode == 0, result.stderr
    assert result.stdout == shipped


def test_config_tools_in_neither_shape_is_refused():
    assert "configTools" not in NO_TOOLS_SECTION
    assert "settings.serversConfig.mcpServer section" in _refused(NO_TOOLS_SECTION)


def test_a_config_tools_section_without_its_key_is_refused():
    missing = persisted("4.5.3").replace('        "configTools" : "enabled",\n', "")
    assert '"mcpServer"' in missing and "configTools" not in missing
    assert "settings.serversConfig.mcpServer has no configTools key" in _refused(missing)


def test_the_input_shape_section_without_its_key_is_refused():
    text = '{\n  "settings" : {\n    "restDataServer" : {\n      "allowAnonymous" : false\n    }\n  }\n}'
    assert "settings.restDataServer has no mcpConfigTools key" in _refused(text)


@pytest.mark.parametrize("value", ['"readOnly"', '"Enabled"', "true", '"enabled" "x"', '{ }'])
def test_an_unrecognised_config_tools_value_is_refused(value):
    odd = PERSISTED_TRUE.replace(_TOOLS_ON, f'"configTools" : {value}')
    assert "unrecognised configTools line" in _refused(odd)


def test_config_tools_as_a_string_value_is_not_the_key():
    """The word in a string VALUE inside the section is not the property:
    the file is accepted, the real key is turned off and the value is kept."""
    text = PERSISTED_TRUE.replace('        "enabled" : true,\n        "configTools"',
                                  '        "enabled" : true,\n        "label" : "configTools",\n'
                                  '        "configTools"')
    out = _ok(text)["settings"]["serversConfig"]["mcpServer"]
    assert out == {"enabled": True, "label": "configTools", "configTools": "disabled"}


def test_a_config_tools_key_at_another_path_is_not_touched():
    """Located by structure: a configTools key outside
    settings.serversConfig.mcpServer is not the hub's setting and is kept."""
    text = PERSISTED_TRUE.replace('      "restDataServer" : {\n        "enabled" : true\n      }',
                                  '      "restDataServer" : {\n        "enabled" : true,\n'
                                  '        "configTools" : "enabled"\n      }')
    out = _ok(text)["settings"]["serversConfig"]
    assert out["restDataServer"]["configTools"] == "enabled"
    assert out["mcpServer"]["configTools"] == "disabled"


def test_every_occurrence_of_the_key_is_turned_off():
    """A repeated key (whichever one the hub's parser keeps) cannot survive."""
    text = PERSISTED_TRUE.replace(f"        {_TOOLS_ON}\n",
                                  f"        {_TOOLS_ON},\n        {_TOOLS_ON}\n")
    result = _awk(text)
    assert result.returncode == 0, result.stderr
    assert _TOOLS_ON not in result.stdout and result.stdout.count(_TOOLS_OFF) == 2


def test_both_shapes_in_one_file_are_both_turned_off():
    text = PERSISTED_TRUE.replace(
        '    "httpServer" : {',
        '    "restDataServer" : {\n      "allowAnonymous" : true,\n'
        '      "mcpConfigTools" : "enabled"\n    },\n    "httpServer" : {')
    out = _ok(text)["settings"]
    assert out["restDataServer"] == {"allowAnonymous": False, "mcpConfigTools": "disabled"}
    assert out["serversConfig"]["mcpServer"]["configTools"] == "disabled"


def test_the_minified_hub_file_is_refused():
    _refused(json.dumps(json.loads(persisted("4.5.3")), separators=(",", ":")))


# ---- the container script (install / refuse) ------------------------------- #

def _shim(dirpath: Path, name: str) -> None:
    path = dirpath / name
    path.write_text("#!/bin/sh\necho forced failure >&2\nexit 2\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_script(tmp_path: Path, content: str | None, shim: str | None = None):
    settings = tmp_path / "appData" / "intelligencehub-settings.json"
    settings.parent.mkdir(parents=True)
    if content is not None:
        settings.write_text(content, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "SETTINGS_FILE": str(settings),
           "HB_SETTINGS_JSON": REPO_SETTINGS.read_text(encoding="utf-8"),
           "SETTINGS_PATCH_AWK": AWK}
    if shim:
        shims = tmp_path / "shims"
        shims.mkdir()
        _shim(shims, shim)
        env["PATH"] = f"{shims}:{env['PATH']}"
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell=True
        [_tool("sh"), "-c", hb._SETTINGS_SEED_SCRIPT], env=env,
        capture_output=True, text=True, timeout=30, check=False)
    return result, settings


def test_script_installs_the_patched_file(tmp_path):
    result, settings = _run_script(tmp_path, PERSISTED_TRUE)
    assert result.returncode == 0, result.stderr
    out = json.loads(settings.read_text())["settings"]
    assert out["httpServer"]["allowAnonymous"] is False
    assert out["serversConfig"]["mcpServer"]["configTools"] == "disabled"
    assert not Path(str(settings) + ".tmp").exists()


@pytest.mark.parametrize("version", sorted(PERSISTED))
def test_script_installs_exactly_the_reconciled_bytes(tmp_path, version):
    """The script tells the awk whether the file ends with a newline, so a
    hub-written file (none) keeps every unrelated byte, and a second run over
    the installed file changes nothing."""
    result, settings = _run_script(tmp_path, persisted(version))
    assert result.returncode == 0, result.stderr
    assert "MCP Configuration Tools disabled" in result.stdout
    assert settings.read_text(encoding="utf-8") == reconciled(persisted(version))
    again = subprocess.run(  # nosec B603 - fixed argv list, no shell=True
        [_tool("sh"), "-c", hb._SETTINGS_SEED_SCRIPT],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SETTINGS_FILE": str(settings),
             "HB_SETTINGS_JSON": REPO_SETTINGS.read_text(encoding="utf-8"), "SETTINGS_PATCH_AWK": AWK},
        capture_output=True, text=True, timeout=30, check=False)
    assert again.returncode == 0, again.stderr
    assert settings.read_text(encoding="utf-8") == reconciled(persisted(version))


def test_script_keeps_the_shipped_file_byte_identical(tmp_path):
    shipped = REPO_SETTINGS.read_text(encoding="utf-8")
    result, settings = _run_script(tmp_path, shipped)
    assert result.returncode == 0, result.stderr
    assert settings.read_text(encoding="utf-8") == shipped


def test_script_seeds_on_first_boot(tmp_path):
    result, settings = _run_script(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert settings.read_text() == REPO_SETTINGS.read_text(encoding="utf-8")
    assert (settings.parent / "license").is_dir()


@pytest.mark.parametrize("case", ["minified", "missing-key", "no-config-tools", "config-tools-value",
                                  "awk-fails", "mv-fails"])
def test_script_fails_closed_and_leaves_the_original_untouched(tmp_path, case):
    """Every refusal exits nonzero (the hub container's SUCCESS dependency then
    keeps the hub down), logs why, removes the temp file, and leaves the
    original settings file byte-for-byte as it was."""
    content = {
        "minified": json.dumps(json.loads(PERSISTED_TRUE), separators=(",", ":")),
        "missing-key": PERSISTED_TRUE.replace(
            '      "allowAnonymous" : true,\n      "authenticationModes"', '      "authenticationModes"'),
        "no-config-tools": NO_TOOLS_SECTION,
        "config-tools-value": PERSISTED_TRUE.replace(_TOOLS_ON, '"configTools" : "readOnly"'),
    }.get(case, PERSISTED_TRUE)
    shim = {"awk-fails": "awk", "mv-fails": "mv"}.get(case)
    result, settings = _run_script(tmp_path, content, shim=shim)
    assert result.returncode != 0, (case, result.stdout, result.stderr)
    assert "FAILED CLOSED" in result.stderr, result.stderr
    assert settings.read_text() == content, "the original settings file was modified"
    assert not Path(str(settings) + ".tmp").exists()


# ---- the hub-written fixtures stay out of exported archives ----------------- #

def test_the_hub_written_fixtures_are_export_ignored():
    """The two persisted-*.json files are files a HighByte hub wrote, so a
    ``git archive`` export of the repository leaves them out
    (``.gitattributes`` export-ignore) and the tests that read them skip there."""
    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout: export attributes are not readable here")
    paths = [str(p.relative_to(repo)) for p in PERSISTED.values()]
    out = subprocess.run(  # nosec B603 B607 - fixed argv list, no shell=True
        ["git", "check-attr", "export-ignore", "--", *paths], cwd=repo,
        capture_output=True, text=True, timeout=30, check=True).stdout
    assert out.splitlines() == [f"{p}: export-ignore: set" for p in paths], out


def test_a_missing_hub_written_fixture_skips_with_its_reason(monkeypatch, tmp_path):
    """In an exported tree the fixture is absent: a test that reads it skips
    and says why, instead of failing on a missing file."""
    monkeypatch.setitem(PERSISTED, "4.4.2", tmp_path / "persisted-4.4.2.json")
    (tmp_path / "persisted-4.5.3.json").write_text("{}", encoding="utf-8")
    monkeypatch.setitem(PERSISTED, "4.5.3", tmp_path / "persisted-4.5.3.json")
    with pytest.raises(pytest.skip.Exception, match="persisted-4.4.2.json.*export-ignore"):
        persisted("4.4.2")
    assert present_versions() == ["4.5.3"]
    assert persisted("4.5.3") == "{}"
