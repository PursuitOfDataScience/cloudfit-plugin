"""The manifests are the install surface: a broken path here is a plugin that
never loads, and the only symptom Claude Code shows is "Connection closed".

These live in the suite rather than in CI yaml so they fail on your machine first.
"""

from __future__ import annotations

import json
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
    args = PLUGIN["mcpServers"]["cloudfit"]["args"]
    assert len(args) == 1
    rel = args[0].replace("${CLAUDE_PLUGIN_ROOT}/", "")
    assert (ROOT / rel).is_file()


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
