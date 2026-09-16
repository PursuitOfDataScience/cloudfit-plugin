"""The manifests are the install surface: a broken path here is a plugin that
never loads, and the only symptom Claude Code shows is "Connection closed".

These live in the suite rather than in CI yaml so they fail on your machine first.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
MARKET = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
PYPROJECT_TEXT = (ROOT / "pyproject.toml").read_text()


def _pyproject(field: str) -> str:
    """One `key = "value"` out of pyproject.toml.

    Deliberately not `tomllib`: that is 3.11+, and this plugin supports 3.10.
    """
    m = re.search(rf'^{field}\s*=\s*"([^"]+)"', PYPROJECT_TEXT, re.M)
    assert m, f"pyproject.toml has no {field}"
    return m.group(1)


def test_plugin_and_pyproject_agree_on_the_version():
    assert PLUGIN["version"] == _pyproject("version")


def test_the_marketplace_offers_this_plugin_by_name():
    assert [p["name"] for p in MARKET["plugins"]] == [PLUGIN["name"]]


@pytest.mark.parametrize("key", ["skills", "commands", "agents"])
def test_every_referenced_path_exists(key):
    for ref in PLUGIN.get(key, []):
        target = ROOT / ref.removeprefix("./")
        assert target.exists(), f"{key}: {ref} does not exist"


def test_the_server_entry_point_exists():
    command = PLUGIN["mcpServers"]["cloudfit"]["command"]
    rel = command.replace("${CLAUDE_PLUGIN_ROOT}/", "")
    assert (ROOT / rel).is_file()
    for ref in PLUGIN["mcpServers"]["cloudfit"].get("args", []):
        assert (ROOT / ref.replace("${CLAUDE_PLUGIN_ROOT}/", "")).exists()


HOOKS_TEXT = (ROOT / "hooks" / "hooks.json").read_text()


def test_nothing_in_the_manifests_points_outside_the_plugin():
    """The bug this guards: the manifest named one machine's interpreter.

    `/software/.../envs/AI/bin/python` exists on exactly one cluster. Everywhere
    else the server never starts and the hook never runs, and the only symptom
    is "Connection closed".
    """
    for name, text in (("plugin.json", json.dumps(PLUGIN)), ("hooks.json", HOOKS_TEXT)):
        absolute = re.findall(r'"[^"]*?(?<!\$\{CLAUDE_PLUGIN_ROOT\})(/(?:usr|opt|home|software|'
                              r'Users|var|project|scratch)/[^"]*)"', text)
        assert not absolute, f"{name} names a host path: {absolute}"


@pytest.mark.parametrize("launcher", ["bin/cloudfit-server", "bin/cloudfit-hook"])
def test_the_launchers_are_executable_and_quiet_on_stdout(launcher):
    """stdout is the MCP channel and the hook's JSON. Diagnostics go to stderr."""
    path = ROOT / launcher
    assert path.is_file() and os.access(path, os.X_OK), f"{launcher} is not executable"
    body = path.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    for line in body.splitlines():
        stripped = line.strip()
        if re.match(r"^[A-Z_]+=\(", stripped):
            continue  # an array definition, not a command that can print
        if stripped.startswith(("echo ", "printf ", '"${PIP[@]}"')) or " pip install" in stripped:
            assert ">&2" in stripped, f"{launcher} writes to stdout: {stripped}"


def test_both_entry_points_go_through_a_launcher():
    assert PLUGIN["mcpServers"]["cloudfit"]["command"].endswith("bin/cloudfit-server")
    assert "bin/cloudfit-hook" in HOOKS_TEXT


def test_the_launcher_refuses_a_python_older_than_the_package_supports():
    floor = re.search(r'requires-python\s*=\s*"([^"]+)"', PYPROJECT_TEXT).group(1)
    major, minor = re.search(r"(\d+)\.(\d+)", floor).groups()
    for launcher in ("bin/cloudfit-server", "bin/cloudfit-hook"):
        body = (ROOT / launcher).read_text()
        assert f"({major}, {minor})" in body, f"{launcher} checks the wrong Python floor"


def test_mcp_is_held_below_2x():
    """server.py imports `mcp.server.fastmcp.FastMCP`, which mcp 2.x renamed.

    Without the ceiling the import fails at load and Claude Code reports only
    "Connection closed", which says nothing about the cause.
    """
    m = re.search(r'^dependencies\s*=\s*\[([^\]]*)\]', PYPROJECT_TEXT, re.M)
    assert m, "pyproject.toml has no dependencies"
    pin = next(d for d in re.findall(r'"([^"]+)"', m.group(1)) if d.startswith("mcp"))
    assert "<2" in pin, f"mcp must be held below 2.x, got {pin!r}"


def test_hooks_json_is_valid_and_names_a_real_script():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    found = json.dumps(hooks)
    for rel in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", found):
        assert (ROOT / rel).exists(), f"hooks.json points at a missing {rel}"


def test_the_changelog_covers_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert PLUGIN["version"] in text
