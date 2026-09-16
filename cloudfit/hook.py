"""PreToolUse handlers, sharing `decide` and `guard` with the MCP tools.

The dialog and the tool cannot disagree because they run the same predicates.

Exit status matters: a broken hook must exit 1, never 2 — exit 2 blocks the very
tool it was watching, so a bug here would stop the user submitting anything.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloudfit import collect as _collect  # noqa: E402
from cloudfit import guard as _guard  # noqa: E402

FACTS_TIMEOUT = 20.0


def decision(verdict: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": verdict,
            "permissionDecisionReason": reason,
        }
    }


def _bullets(title: str, items: list[str]) -> str:
    return title + "\n" + "\n".join(f"  - {i}" for i in items)


def evaluate_command(command: str, *, runner=None,
                     read_text=lambda p: Path(p).read_text(encoding="utf-8")) -> dict | None:
    """The whole hook decision, pure enough to test: command in, hook JSON or None out."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # unbalanced quotes are the shell's problem, not ours
    if not tokens:
        return None

    launch = _guard.launch_without_max_run_duration(tokens)
    if launch:
        return decision("deny", f"cloudfit: {launch}")

    if not any(t.endswith("sbatch") for t in tokens):
        return None
    script_path = _guard.script_argument(tokens)
    if not script_path:
        return None
    try:
        text = read_text(script_path)
    except OSError:
        return None  # sbatch will report a missing script better than we can

    request = _guard.parse_script(text)
    site = _site_facts(runner)
    facts = None
    name = request.partition or site.default_partition
    if name and (runner is not None or request.partition):
        facts = _partition_facts(name, runner)
    checked = _guard.check_script(text, facts, site)

    if checked.refusals:
        return decision("deny", _bullets(
            f"cloudfit refuses {script_path}:", checked.refusals))
    if checked.warnings:
        return decision("ask", _bullets(
            f"cloudfit has no objection to {script_path}, but:", checked.warnings))
    return None


def _site_facts(runner) -> _collect.SiteFacts:
    try:
        return _collect.site_facts(runner)
    except Exception:  # noqa: BLE001 -- the hook must never block on its own lookup
        return _collect.SiteFacts()


def _partition_facts(name: str, runner) -> _collect.PartitionFacts | None:
    try:
        return _collect.partition_facts(name, runner)
    except Exception:  # noqa: BLE001 -- a slow or absent scheduler must not block the tool
        return None


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError) as exc:
        print(f"cloudfit hook: unreadable payload: {exc}", file=sys.stderr)
        return 1
    try:
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        result = evaluate_command(command, runner=_collect.default_runner())
        if result is not None:
            json.dump(result, sys.stdout)
            sys.stdout.write("\n")
        return 0
    except Exception as exc:  # noqa: BLE001 -- exit 1, never 2
        print(f"cloudfit hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
