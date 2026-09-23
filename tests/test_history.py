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
    outcome = from_slurmpast("compute_reserve", runner, partition="compute")
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
    blank = "59100001|t|compute|COMPLETED|4|16Gn|||||billing=4,cpu=4,mem=16G,node=1"
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
    from_slurmpast("software", runner, user="alice")
    argv = runner.calls[0]
    assert "-u" in argv
    assert argv[argv.index("-u") + 1] == "alice"


def test_the_chain_passes_the_user_through_to_both_slurm_sources():
    runner = FakeRunner().on("slurmpast", stdout='{"workloads": []}').on("sacct", stdout="")
    history("nope", runner=runner, user="someone-else")
    assert all("someone-else" in " ".join(c) for c in runner.calls)


def test_a_legacy_cgroup_row_is_not_sized_to_a_sampled_instant(tmp_path):
    """Records written before 0.1.2 carry no `mem_peak_is_lifetime`.

    Without the migration such a row sizes `--mem` to the working set at the
    moment of sampling, the very reading 0.1.2 stopped trusting.
    """
    row = {
        "source": "slurmwatch", "kind": "sample", "workload": "linalg",
        "mem_peak_bytes": 2040109465, "mem_peak_working_set_bytes": 322122547,
        "mem_cache_bytes": 21474836, "mem_cache_measured": True,
        "mem_peak_source": "cgroup",
    }
    (tmp_path / "history.jsonl").write_text(json.dumps(row) + "\n")
    (obs,) = read_record("linalg", directory=tmp_path)
    assert obs.mem_peak_is_lifetime
    assert obs.mem_peak_trusted_bytes == 2040109465
    assert obs.mem_peak_basis == "watermark"


def test_a_legacy_sacct_row_keeps_its_own_reading(tmp_path):
    """Only cgroup rows get the inference: MaxRSS is not a kernel watermark."""
    row = {
        "source": "sacct", "kind": "final", "workload": "linalg",
        "mem_peak_bytes": 4000000000, "mem_peak_source": "sacct MaxRSS",
    }
    (tmp_path / "history.jsonl").write_text(json.dumps(row) + "\n")
    (obs,) = read_record("linalg", directory=tmp_path)
    assert not obs.mem_peak_is_lifetime
    assert obs.mem_peak_trusted_bytes == 4000000000


def test_a_running_job_in_sacct_is_live_not_finished():
    """Its elapsed so far is a floor; read as finished it cut a 12h job's --time to 01:15:00."""
    rows = ("59500001|longjob|compute|RUNNING|8|32Gn||01:10:00|01:00:00|12:00:00|"
            "billing=8,cpu=8,mem=32G,node=1\n"
            "59500002|longjob|compute|COMPLETED|8|32Gn||09:00:00|10:00:00|12:00:00|"
            "billing=8,cpu=8,mem=32G,node=2\n")
    running, done = observations_from_sacct(rows)
    assert (running.kind, done.kind) == ("sample", "final")
    assert done.node_count == 2
    assert done.cores_scope == "job"  # CPU-seconds cover every task on every node


def test_repeated_snapshots_of_one_job_are_one_run(record_home):
    """Measured four times, one job read as four agreeing runs: a recommendation from one job."""
    for cores, elapsed in ((2.0, 60), (7.5, 120), (3.0, 180), (4.0, 240)):
        record(Observation(source="slurmwatch", workload="alpha", job_id="1",
                           cores_used=cores, elapsed_seconds=elapsed))
    record(Observation(source="slurmwatch", workload="alpha", job_id="2", cores_used=1.0))
    first, second = read_record("alpha")
    assert first.job_id == "1"
    assert first.cores_used == 7.5  # the busiest snapshot, not the last one
    assert first.elapsed_seconds == 240
    assert second.job_id == "2"


def test_one_job_seen_by_two_sources_is_still_one_run():
    from cloudfit.decide import one_per_run

    live = Observation(source="slurmwatch", job_id="5", cores_used=3.0)
    same = Observation(source="sacct", job_id="5", cores_used=2.5, cores_basis="average")
    rollup = Observation(source="slurmpast", kind="rollup", runs=40, cores_used=1.0)
    assert one_per_run([live, same, rollup]) == [live, rollup]


def test_slurmpast_times_the_work_from_completed_runs_only():
    """14 runs of the gpu entry, 4 of them completed: the walltime axis is n=4."""
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    result = from_slurmpast("software", runner, partition="gpu")
    timed = next(o for o in result.observations if o.elapsed_seconds)
    assert timed.runs == 4
    cores = next(o for o in result.observations if o.cores_used)
    assert cores.cores_scope == "task"  # slurmpast reports cores per task


def test_slurmpast_cautions_travel_with_the_answer():
    runner = FakeRunner().on("slurmpast", stdout=SLURMPAST)
    result = from_slurmpast("software", runner, partition="gpu")
    assert any("this workload runs 2" in n for n in result.notes)
    assert any("may be low" in n for n in result.notes)


def test_sacct_and_the_record_read_the_partition_the_script_runs_on(record_home):
    rows = ("1|train|gpu|COMPLETED|8|64Gn||80:00:00|10:00:00|12:00:00|cpu=8,gres/gpu=1,node=1\n"
            "2|train|cpu|COMPLETED|8|64Gn||01:00:00|00:50:00|12:00:00|cpu=8,node=1\n")
    runner = FakeRunner().absent("slurmpast").on("sacct", stdout=rows)
    assert [o.job_id for o in history("train", runner=runner, partition="gpu").observations] == ["1"]
    # A partition this workload never ran on narrows to nothing, so it falls back to every run.
    assert len(history("train", runner=runner, partition="bigmem").observations) == 2

    record(Observation(source="slurmwatch", workload="own", job_id="3", partition="gpu"))
    record(Observation(source="slurmwatch", workload="own", job_id="4", partition="cpu"))
    empty = FakeRunner().absent("slurmpast").absent("sacct")
    assert [o.job_id for o in history("own", runner=empty, partition="cpu").observations] == ["4"]
