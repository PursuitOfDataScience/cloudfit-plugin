"""The MCP surface, and the submit path, against a fake and never a real queue."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import load_json, load_text

from cloudfit.collect import FakeRunner, partition_facts
from cloudfit.guard import parse_script
from cloudfit.server import mcp, submit_with

CPU_SCRIPT = """#!/bin/bash
#SBATCH --job-name=caai-quickstart
#SBATCH --partition=compute
#SBATCH --account=pi-example
#SBATCH --cpus-per-task=4
#SBATCH --mem=5G
#SBATCH --time=00:02:00
python run.py
"""


def submit_runner(node: str, gres: str) -> FakeRunner:
    return (
        FakeRunner()
        .on("sbatch", stdout="59400001;cluster0\n")
        .on("squeue", stdout=f"RUNNING|(None)|{node}\n")
        .on("scontrol", "show", "hostnames", stdout=f"{node}\n")
        .on("scontrol", "show", "node", stdout=f"NodeName={node} Gres={gres}\n")
    )


def facts_for(partition: str) -> object:
    runner = (
        FakeRunner()
        .on("scontrol", "show", "partition", partition,
            stdout=load_text(f"scontrol_partition_{partition}_real.txt"))
        .on("sinfo", "-p", partition, "%n %c %m",
            stdout=load_text(f"sinfo_{partition}_sizes_real.txt"))
        .on("sinfo", "-p", partition, "%N %G",
            stdout=load_text(f"sinfo_{partition}_nodes_real.txt"))
    )
    return partition_facts(partition, runner)


def test_every_phase_three_tool_is_exposed():
    names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert names == ["site", "capabilities", "measure", "history", "fit", "check", "submit",
                     "doctor"]


def test_the_tool_schemas_describe_their_arguments():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert tools["measure"].inputSchema["required"] == ["job_id"]
    assert set(tools["fit"].inputSchema["properties"]) == {"job_id", "script_path", "script", "since"}
    assert tools["fit"].inputSchema.get("required", []) == []
    assert all(t.description for t in tools.values())


def test_submit_refuses_before_reaching_sbatch():
    from cloudfit.collect import SiteFacts

    runner = submit_runner("cn-0501", "(null)")
    without = CPU_SCRIPT.replace("#SBATCH --account=pi-example\n", "")
    result = submit_with(without, "job.sbatch", parse_script(without), facts_for("compute"),
                         runner=runner, site=SiteFacts(account_lookup_ok=True))
    assert not result["submitted"]
    assert any("no --account" in r for r in result["refusals"])
    assert runner.calls == []  # nothing was submitted


def test_submit_on_a_cpu_only_partition_needs_no_exclusion():
    runner = submit_runner("cn-0501", "(null)")
    result = submit_with(CPU_SCRIPT, "job.sbatch", parse_script(CPU_SCRIPT), facts_for("compute"),
                         runner=runner)
    assert result["submitted"]
    assert result["job_id"] == "59400001"
    assert result["excluded"] == []
    assert result["argv"] == ["sbatch", "--parsable", "job.sbatch"]
    assert result["exclusion_verified"] == "verified"
    assert result["refusals"] == []


def test_submit_generates_the_exclusion_from_the_live_partition():
    script = CPU_SCRIPT.replace("--partition=compute", "--partition=gpu")
    runner = submit_runner("cn-0290", "(null)")
    result = submit_with(script, "job.sbatch", parse_script(script), facts_for("gpu"),
                         runner=runner)
    assert result["submitted"]
    assert len(result["excluded"]) == 11
    assert result["argv"][2].startswith("--exclude=cn-0277,")
    assert runner.argv_containing("sbatch")[0][2].startswith("--exclude=")


def test_a_no_gres_job_that_lands_on_a_gpu_node_is_reported_not_ignored():
    script = CPU_SCRIPT.replace("--partition=compute", "--partition=gpu")
    runner = submit_runner("cn-0277", "gpu:v100:4")
    result = submit_with(script, "job.sbatch", parse_script(script), facts_for("gpu"),
                         runner=runner)
    assert result["submitted"]
    assert any("landed on cn-0277" in r for r in result["refusals"])
    assert any("scancel 59400001" in w for w in result["warnings"])


def test_a_gpu_job_is_submitted_without_an_exclusion():
    script = CPU_SCRIPT.replace("--partition=compute", "--partition=gpu").replace(
        "#SBATCH --mem=5G", "#SBATCH --gres=gpu:1")
    runner = submit_runner("cn-0277", "gpu:v100:4")
    result = submit_with(script, "job.sbatch", parse_script(script), facts_for("gpu"),
                         runner=runner)
    assert result["submitted"]
    assert result["excluded"] == []
    assert result["refusals"] == []


def test_dry_run_shows_the_argv_without_submitting():
    runner = submit_runner("cn-0501", "(null)")
    result = submit_with(CPU_SCRIPT, "job.sbatch", parse_script(CPU_SCRIPT), facts_for("compute"),
                         dry_run=True, runner=runner)
    assert not result["submitted"]
    assert result["argv"] == ["sbatch", "--parsable", "job.sbatch"]
    assert any("nothing was submitted" in w for w in result["warnings"])
    assert runner.calls == []


def test_an_sbatch_failure_is_surfaced_verbatim():
    runner = FakeRunner().on("sbatch", returncode=1, stderr="sbatch: error: QOSMaxSubmitJobPerUser")
    result = submit_with(CPU_SCRIPT, "job.sbatch", parse_script(CPU_SCRIPT), facts_for("compute"),
                         runner=runner)
    assert not result["submitted"]
    assert "QOSMaxSubmitJobPerUser" in result["refusals"][0]


def test_the_check_report_travels_with_the_submission():
    runner = submit_runner("cn-0501", "(null)")
    result = submit_with(CPU_SCRIPT, "job.sbatch", parse_script(CPU_SCRIPT), facts_for("compute"),
                         runner=runner)
    assert result["check"]["ok"]
    assert result["check"]["request"]["cpus"] == 4


@pytest.mark.parametrize("tool", ["check", "fit"])
def test_tools_require_a_script_or_a_job(tool):
    from cloudfit import server

    with pytest.raises(ValueError, match="pass either|pass a job_id"):
        getattr(server, tool)()


def test_history_tool_falls_back_to_the_filename_as_the_workload(tmp_path, monkeypatch):
    from cloudfit import server

    path = tmp_path / "tokenize-shards.sbatch"
    path.write_text("#SBATCH --mem=8G\n")  # no --job-name
    runner = FakeRunner().absent("slurmpast").absent("sacct")
    monkeypatch.setattr("cloudfit.collect.default_runner", lambda: runner)
    monkeypatch.setenv("CLOUDFIT_HOME", str(tmp_path / "record"))
    assert server.history(script_path=str(path))["workload"] == "tokenize-shards"
    # The fake answered, not the real cluster: history resolves the runner through collect.
    assert runner.argv_containing("slurmpast") and runner.argv_containing("sacct")
    with pytest.raises(ValueError, match="pass either"):
        server.history()


def test_measure_records_what_it_measured(record_home, monkeypatch, cpu_overask_real):
    from cloudfit import server

    runner = FakeRunner().on("slurmwatch", stdout=json.dumps(cpu_overask_real))
    monkeypatch.setattr("cloudfit.collect.default_runner", lambda: runner)
    payload = server.measure("58107383")
    assert payload["observation"]["job_id"] == "58107383"
    assert payload["recorded_to"].endswith("history.jsonl")
    assert (record_home / "history.jsonl").exists()


def _offline(runner: FakeRunner) -> FakeRunner:
    """No scheduler to ask about the site or a partition: the fit runs on telemetry alone."""
    return runner.absent("sinfo").absent("sacctmgr").absent("scontrol")


def test_fitting_a_running_job_again_does_not_count_it_again(record_home, monkeypatch, gpu_hbm40):
    """Recorded, then read back from the record: one snapshot became n=2, then 3, then 4."""
    from cloudfit import server

    runner = _offline(FakeRunner().on("slurmwatch", stdout=json.dumps(gpu_hbm40))
                      .absent("slurmpast").absent("sacct"))
    monkeypatch.setattr("cloudfit.collect.default_runner", lambda: runner)
    for _ in range(4):
        payload = server.fit(job_id=str(gpu_hbm40["job_id"]))
        assert payload["n"] == 1
        assert payload["confidence"]["level"] == "low"
    assert len((record_home / "history.jsonl").read_text().splitlines()) == 4  # still recorded


def test_fit_reads_history_from_the_partition_the_script_runs_on(record_home, monkeypatch):
    """`software` runs 1h on test and 12h on gpu; sizing the gpu script from test cut it to 1h14m."""
    from cloudfit import server

    runner = _offline(FakeRunner().on("slurmpast",
                                      stdout=json.dumps(load_json("slurmpast_sizing_real.json"))))
    monkeypatch.setattr("cloudfit.collect.default_runner", lambda: runner)
    script = ("#!/bin/bash\n#SBATCH --job-name=software\n#SBATCH --partition=gpu\n"
              "#SBATCH --gres=gpu:1\n#SBATCH -c 4\n#SBATCH --mem=65G\n#SBATCH --time=12:00:00\n")
    walltime = next(d for d in server.fit(script=script)["directives"] if d["axis"] == "walltime")
    assert walltime["observed"] == "11:59:00 longest"
    assert walltime["confidence"]["n"] == 4
