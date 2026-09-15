from __future__ import annotations

import json

from conftest import load_json, load_text

from cloudfit import GIB, Observation
from cloudfit.collect import FakeRunner
from cloudfit.history import (
    from_sacct,
    from_slurmpast,
    history,
    observations_from_sacct,
    read_record,
    record,
    workload_from_script,
    workload_key,
)

SLURMPAST = json.dumps(load_json("slurmpast_sizing_real.json"))
SACCT = load_text("sacct_steps_quickstart_real.psv")


def test_workload_key_matches_slurmpast_name_collapsing():
    assert workload_key("caai-p56a") == "caai-p#a"
    assert workload_key("exp-a5-n33") == "exp-a#-n#"
    assert workload_key("sixdeg") == "sixdeg"


def test_workload_comes_from_the_job_name_then_the_filename():
    assert workload_from_script("#SBATCH -J tokenize\n") == "tokenize"
    assert workload_from_script("#SBATCH --mem=8G\n", "runs/train-a5.sbatch") == "train-a5"
    assert workload_from_script("echo hi\n") is None


# ------------------------------------------------------------------ slurmpast


def test_slurmpast_counts_n_per_axis_not_per_workload():
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    result = from_slurmpast("software", runner, partition="test")
    assert result.source == "slurmpast"
    assert result.n == 100  # the workload's run count
    by_axis = {
        "mem": next(o for o in result.observations if o.mem_peak_bytes),
        "cores": next(o for o in result.observations if o.cores_used),
        "time": next(o for o in result.observations if o.elapsed_seconds),
    }
    assert by_axis["mem"].runs == 13  # only 13 runs carried a memory reading
    assert by_axis["cores"].runs == 99
    assert by_axis["mem"].mem_peak_bytes == int(60.6 * GIB)
    assert by_axis["cores"].cores_used == 3.0
    assert all(o.kind == "rollup" for o in result.observations)


def test_slurmpast_oom_peak_is_reported_as_a_floor():
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    result = from_slurmpast("build_reserve", runner)
    assert any("OOM kill" in n for n in result.notes)
    assert any("no completed run to time" in n for n in result.notes)


def test_slurmpast_says_why_it_cannot_answer():
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    assert "no workload matching" in from_slurmpast("nothing-like-this", runner)
    empty = FakeRunner().on("slurmpast", stdout='{"workloads": []}')
    assert "no workload matching" in from_slurmpast("x", empty)
    absent = FakeRunner().absent("slurmpast")
    assert from_slurmpast("x", absent) == "slurmpast is not installed"


def test_a_workload_slurmpast_knows_but_cannot_size_falls_through():
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    outcome = from_slurmpast("amd_reserve", runner, partition="amd")
    assert isinstance(outcome, str)
    assert "none carry a usable resource reading" in outcome


# ---------------------------------------------------------------------- sacct


def test_sacct_folds_step_rows_in_to_recover_maxrss():
    observations = observations_from_sacct(SACCT)
    quickstart = [o for o in observations if o.workload == "caai-quickstart"]
    assert len(quickstart) == 10
    # MaxRSS is blank on every job row; it only exists on .batch
    assert all(o.mem_peak_bytes for o in quickstart)
    assert max(o.mem_peak_bytes for o in quickstart) == 3690348 * 1024
    assert all(o.cores_basis == "average" for o in quickstart)
    assert all(o.kind == "final" for o in quickstart)


def test_sacct_reads_gpu_count_from_alloctres():
    row = ("53420701|exp-a5|test|COMPLETED|4|32Gn||00:00:00|00:14:25|00:20:00|"
           "billing=4,cpu=4,gres/gpu=1,mem=32G,node=1")
    assert observations_from_sacct(row)[0].gpu_count == 1


def test_sacct_with_accounting_off_is_reported_not_used():
    blank = "59100001|t|amd|COMPLETED|4|16Gn|||||billing=4,cpu=4,mem=16G,node=1"
    runner = FakeRunner().on("sacct", stdout=blank)
    outcome = from_sacct("t", runner)
    assert isinstance(outcome, str)
    assert "JobAcctGatherType is off" in outcome


def test_sacct_notes_that_its_core_figure_is_an_average():
    runner = FakeRunner().on("sacct", stdout=SACCT)
    result = from_sacct("caai-quickstart", runner)
    assert result.source == "sacct"
    assert any("run average" in n for n in result.notes)


# ------------------------------------------------------------- the own record


def test_the_record_round_trips_and_filters_by_workload(record_home):
    record(Observation(source="slurmwatch", workload="alpha", job_id="1", cores_used=2.0))
    record(Observation(source="slurmwatch", workload="beta", job_id="2", cores_used=3.0))
    assert (record_home / "history.jsonl").exists()
    assert [o.job_id for o in read_record("alpha")] == ["1"]
    assert len(read_record(None)) == 2


def test_the_record_ignores_a_corrupt_line(record_home):
    record(Observation(source="slurmwatch", workload="alpha", job_id="1"))
    (record_home / "history.jsonl").open("a").write("not json\n")
    assert len(read_record("alpha")) == 1


# ------------------------------------------------------------------ the chain


def test_the_chain_prefers_slurmpast_and_says_so():
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST).on("sacct", stdout=SACCT)
    result = history("software", runner=runner, partition="test")
    assert result.source == "slurmpast"
    assert result.tried == []


def test_the_chain_falls_back_to_sacct_naming_the_reason():
    runner = FakeRunner().absent("slurmpast").on("sacct", stdout=SACCT)
    result = history("caai-quickstart", runner=runner)
    assert result.source == "sacct"
    assert result.n == 10
    assert result.tried == ["slurmpast is not installed"]


def test_the_chain_falls_back_to_the_own_record_on_a_cluster_with_no_accounting(record_home):
    record(Observation(source="slurmwatch", workload="mine", job_id="7",
                       mem_peak_bytes=3 * GIB, elapsed_seconds=60))
    runner = FakeRunner().absent("slurmpast").on("sacct", stdout="")
    result = history("mine", runner=runner)
    assert result.source == "record"
    assert result.n == 1
    assert len(result.tried) == 2
    assert "no run named" in result.tried[1]
    assert any("own record" in n for n in result.notes)


def test_the_chain_reports_n_zero_rather_than_pretending(record_home):
    runner = FakeRunner().absent("slurmpast").absent("sacct")
    result = history("unknown-workload", runner=runner)
    assert result.source == "none"
    assert result.n == 0
    assert len(result.tried) == 3


def test_slurmpast_is_scoped_to_the_caller_not_the_cluster():
    """An Operator account can see every user; a fit must still only see its own runs."""
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    from_slurmpast("software", runner, user="youzhi")
    argv = runner.calls[0]
    assert "-u" in argv
    assert argv[argv.index("-u") + 1] == "youzhi"


def test_the_chain_passes_the_user_through_to_both_slurm_sources():
    runner = FakeRunner().on("slurmpast", stdout='{"workloads": []}').on("sacct", stdout="")
    history("nope", runner=runner, user="someone-else")
    assert all("someone-else" in " ".join(c) for c in runner.calls)
