from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, load_text

from cloudfit.collect import FakeRunner, partition_facts
from cloudfit.hook import evaluate_command, main

CLEAN = """#!/bin/bash
#SBATCH --job-name=fit-me
#SBATCH --partition=amd
#SBATCH --account=rcc-staff
#SBATCH --cpus-per-task=4
#SBATCH --mem=5G
#SBATCH --time=00:10:00
python run.py
"""


def amd_runner() -> FakeRunner:
    return (
        FakeRunner()
        .on("scontrol", "show", "partition", "amd",
            stdout=load_text("scontrol_partition_amd_real.txt"))
        .on("sinfo", "-p", "amd", "%n %c %m", stdout=load_text("sinfo_amd_sizes_real.txt"))
        .on("sinfo", "-p", "amd", "%N %G", stdout=load_text("sinfo_amd_nodes_real.txt"))
    )


def evaluate(command: str, script: str = CLEAN):
    return evaluate_command(command, runner=amd_runner(), read_text=lambda _: script)


def verdict(result) -> str | None:
    return result and result["hookSpecificOutput"]["permissionDecision"]


def test_an_unrelated_command_is_left_alone():
    assert evaluate("ls -la") is None
    assert evaluate("squeue -u me") is None


def test_a_clean_submission_is_not_interfered_with():
    assert evaluate("sbatch job.sbatch") is None


def test_a_missing_account_is_denied_with_the_reason():
    result = evaluate("sbatch job.sbatch", CLEAN.replace("#SBATCH --account=rcc-staff\n", ""))
    assert verdict(result) == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "cloudfit refuses job.sbatch" in reason
    assert "Account is not specified" in reason


def test_policy_gaps_ask_rather_than_deny():
    result = evaluate("sbatch job.sbatch", CLEAN.replace("#SBATCH --time=00:10:00\n", ""))
    assert verdict(result) == "ask"
    assert "no objection" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_hook_and_the_tool_cannot_disagree():
    from cloudfit.guard import check_script, parse_script

    without = CLEAN.replace("#SBATCH --account=rcc-staff\n", "")
    facts = partition_facts("amd", amd_runner())
    refusals = check_script(without, facts).refusals
    reason = evaluate("sbatch job.sbatch", without)["hookSpecificOutput"]["permissionDecisionReason"]
    assert all(r in reason for r in refusals)
    assert parse_script(without).account is None


def test_a_launch_without_a_deadline_is_denied():
    result = evaluate("gcloud compute instances create fit-1 --zone=us-central1-a")
    assert verdict(result) == "deny"
    assert "--max-run-duration" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_launch_with_a_deadline_passes():
    assert evaluate("gcloud compute instances create fit-1 --max-run-duration=2h") is None


def test_an_unreadable_script_is_left_to_sbatch():
    assert evaluate_command("sbatch missing.sbatch", runner=amd_runner()) is None


def test_unbalanced_quotes_are_not_our_problem():
    assert evaluate("sbatch 'job.sbatch") is None


def test_a_wrapped_submission_has_no_script_to_read():
    assert evaluate("sbatch --wrap 'python run.py'") is None


def test_main_ignores_a_non_bash_tool():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
    sys.stdin = io.StringIO(json.dumps(payload))
    assert main() == 0


def test_main_exits_one_on_bad_input_never_two():
    sys.stdin = io.StringIO("not json")
    assert main() == 1


def test_the_hook_exits_zero_for_a_clean_command_in_a_real_process(tmp_path):
    """The exit status is the contract: 2 would block the tool it watches."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "cloudfit" / "hook.py")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_the_hook_denies_a_real_bad_script_end_to_end(tmp_path):
    script = tmp_path / "bad.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --partition=amd\n#SBATCH --mem=8G\n")
    payload = {"tool_name": "Bash", "tool_input": {"command": f"sbatch {script}"}}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "cloudfit" / "hook.py")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0
    emitted = json.loads(proc.stdout)
    assert emitted["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no --account" in emitted["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_py_is_runnable_as_a_script():
    assert Path(ROOT / "cloudfit" / "hook.py").is_file()
