"""The invariants from the brief, checked against the source rather than trusted."""

from __future__ import annotations

import ast
import json
import re

from conftest import FIXTURES, ROOT

PACKAGE = ROOT / "cloudfit"
MODULES = sorted(PACKAGE.glob("*.py"))


def imports_of(path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_subprocess_lives_only_in_collect():
    offenders = [p.name for p in MODULES if "subprocess" in imports_of(p) and p.name != "collect.py"]
    assert offenders == []


def test_no_shell_is_ever_invoked():
    for path in MODULES:
        text = path.read_text()
        assert "shell=True" not in text, path.name
        assert "os.system" not in text, path.name
        assert "os.popen" not in text, path.name
        assert "subprocess.call" not in text, path.name
        assert "check_output" not in text, path.name


def test_every_subprocess_run_is_given_an_argument_list():
    tree = ast.parse((PACKAGE / "collect.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "run"]
    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call.args[0], ast.Name)  # argv, built as a list above
    assert not any(k.arg == "shell" for k in call.keywords)


def test_the_decision_logic_imports_nothing_that_shells_out():
    for name in ("decide.py", "guard.py"):
        assert imports_of(PACKAGE / name).isdisjoint({"subprocess", "os", "shutil", "socket"}), name
    assert "collect" not in (PACKAGE / "guard.py").read_text()


def test_nothing_writes_to_the_user_config():
    for path in MODULES:
        text = path.read_text()
        assert ".claude" not in text, path.name
        assert "expanduser" not in text, path.name


READ_ONLY_GCLOUD = {("config", "get-value"), ("services", "list"), ("compute", "project-info"),
                    ("compute", "firewall-rules"), ("quotas", "info"), ("projects", "get-iam-policy")}


def _gcloud_argv_literals(tree) -> list[tuple[list[str], bool]]:
    """Every `["gcloud", ...]` literal, and whether it is stored on a Finding as a fix."""
    inside_finding: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Finding":
            inside_finding.update(id(n) for n in ast.walk(node) if isinstance(n, ast.List))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        first = node.elts[0]
        if isinstance(first, ast.Constant) and first.value == "gcloud":
            words = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            out.append((words, id(node) in inside_finding))
    return out


def test_doctor_only_ever_runs_read_only_gcloud_verbs():
    """A mutating gcloud argv is allowed to exist only as a Finding's fix_argv."""
    literals = _gcloud_argv_literals(ast.parse((PACKAGE / "probe.py").read_text()))
    assert literals
    for words, is_fix in literals:
        if is_fix:
            continue
        verb = tuple(w for w in words[1:3] if not w.startswith("-"))
        assert verb in READ_ONLY_GCLOUD, words


def test_no_other_module_builds_a_gcloud_command():
    for path in MODULES:
        if path.name == "probe.py":
            continue
        assert not _gcloud_argv_literals(ast.parse(path.read_text())), path.name


def test_only_sbatch_submits_and_only_from_collect():
    for path in MODULES:
        tree = ast.parse(path.read_text())
        literals = [n for n in ast.walk(tree) if isinstance(n, ast.List) and n.elts
                    and isinstance(n.elts[0], ast.Constant)
                    and n.elts[0].value in {"sbatch", "scancel", "srun"}]
        assert not literals or path.name == "collect.py", path.name
    assert "scancel" not in (PACKAGE / "collect.py").read_text()


def test_every_fixture_declares_whether_it_is_real():
    for path in FIXTURES.iterdir():
        if path.name.startswith("_") or path.is_dir():
            continue
        assert re.search(r"_(real|synthetic)\.", path.name), path.name


def test_the_recorded_slurmwatch_fixtures_are_not_demo_output():
    for path in FIXTURES.glob("slurmwatch_*.json"):
        doc = json.loads(path.read_text())
        if doc.get("telemetry_available") is False:
            continue  # the no-Slurm facts view has no telemetry to be mock
        assert doc.get("mock") is False, path.name
        assert doc["cpu"]["source"] != "mock", path.name


def test_the_plugin_manifest_points_at_files_that_exist():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "cloudfit"
    assert isinstance(manifest["agents"], list)  # a directory is rejected by --strict
    for key in ("skills", "commands", "agents"):
        for entry in manifest[key]:
            assert (ROOT / entry).exists(), entry
    assert isinstance(manifest["mcpServers"], dict)  # see the entry-point test below
    # hooks/hooks.json is auto-loaded; declaring it too makes the plugin fail to
    # load as a duplicate, which `validate --strict` does not catch.
    assert "hooks" not in manifest
    assert (ROOT / "hooks" / "hooks.json").exists()


def test_the_hook_manifest_has_the_canonical_shape():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    assert list(hooks) == ["hooks"]
    entry = hooks["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    handler = entry["hooks"][0]
    assert handler["type"] == "command"
    assert "${CLAUDE_PLUGIN_ROOT}" in handler["command"]
    assert handler["command"].endswith("cloudfit/hook.py")


def test_the_marketplace_manifest_offers_this_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "cloudfit-plugin"
    assert [p["name"] for p in market["plugins"]] == ["cloudfit"]
    assert market["plugins"][0]["source"] == "./"
    assert market["plugins"][0]["description"]


def test_the_mcp_entry_point_resolves():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    servers = manifest["mcpServers"]
    assert list(servers) == ["cloudfit"]
    arg = servers["cloudfit"]["args"][0]
    assert arg.startswith("${CLAUDE_PLUGIN_ROOT}/")
    assert (ROOT / arg.replace("${CLAUDE_PLUGIN_ROOT}/", "")).exists()


def test_no_mcp_json_sits_at_the_repo_root():
    # A .mcp.json at the root has two identities at once: the plugin's server
    # definition, where ${CLAUDE_PLUGIN_ROOT} is set, AND an auto-discovered
    # project-scope server for anyone whose cwd is this repo, where it is NOT.
    # The second one expands to /cloudfit/server.py and fails, and the only thing
    # /mcp reports is `cloudfit  ✘ failed` next to a working copy of the same name.
    # Declaring the server inline in plugin.json leaves nothing to discover.
    assert not (ROOT / ".mcp.json").exists()


def test_the_declared_versions_agree():
    # plugin.json's `version` is a pin: Claude Code serves installed users whatever
    # content carried the string they already hold, so shipping a change without
    # bumping it is invisible -- `plugin update` reports "already at the latest".
    # Three places name the version, and drift between them is silent.
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    version = manifest["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version

    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(),
                          re.MULTILINE)
    assert pyproject and pyproject.group(1) == version, "pyproject.toml disagrees"

    # The marketplace entry must not also declare one: plugin.json wins without warning,
    # so a version there would be masked rather than applied.
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert "version" not in market["plugins"][0]

    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(),
                          re.MULTILINE)
    assert released, "CHANGELOG.md has no released version heading"
    assert released[0] == version, f"CHANGELOG.md newest entry is {released[0]}"


def test_the_mcp_dependency_excludes_the_two_line():
    # server.py imports mcp.server.fastmcp, which mcp 2.x renamed to MCPServer.
    # An unbounded `mcp>=1.28` resolves to 2.x and the import dies at startup --
    # and the only thing Claude Code reports is "Connection closed".
    pyproject = (ROOT / "pyproject.toml").read_text()
    spec = re.search(r'"(mcp[^"]*)"', pyproject)
    assert spec, "pyproject declares no mcp dependency"
    assert "<2" in spec.group(1), spec.group(1)
    assert "mcp.server.fastmcp" in (PACKAGE / "server.py").read_text()


def test_the_hook_never_exits_two():
    source = (PACKAGE / "hook.py").read_text()
    assert "return 2" not in source
    assert "exit(2)" not in source
    assert re.search(r"return 1\b", source)


def test_the_readme_install_uses_the_claude_code_path():
    readme = (ROOT / "README.md").read_text()
    assert "claude plugin marketplace add PursuitOfDataScience/cloudfit-plugin" in readme
    assert "claude plugin install cloudfit@cloudfit-plugin" in readme
    assert "git clone" not in readme


def test_the_tools_cloudfit_stands_on_are_credited():
    readme = (ROOT / "README.md").read_text()
    for tool in ("slurmwatch", "slurmpast"):
        assert f"https://github.com/PursuitOfDataScience/{tool}" in readme, tool
        assert f"https://pypi.org/project/{tool}/" in readme, tool


SITE_NAMES = ("rcc-staff", "caslake", "midway3", "beagle3", "amd", "pi-example", "alice")


def test_no_cluster_name_is_baked_into_the_package():
    """The bug this guards: one site's partition and account shipped as everyone's default."""
    for path in MODULES:
        text = path.read_text().lower()
        for name in SITE_NAMES:
            assert name not in text, f"{path.name} names {name!r}"


def test_the_site_specific_knobs_all_arrive_as_data():
    """guard decides; it never looks anything up. Site names reach it via SiteFacts."""
    from cloudfit.collect import SITE_FIELDS, SiteFacts

    assert set(SITE_FIELDS) <= set(SiteFacts().as_dict())
    text = (PACKAGE / "guard.py").read_text()
    assert "os.environ" not in text
