"""The decision logic, against recorded telemetry. No subprocess anywhere in here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURES

from cloudfit import GIB, Observation
from cloudfit.collect import observation_from_slurmwatch
from cloudfit.decide import fit, sample_confidence
from cloudfit.guard import parse_script
from cloudfit.history import observations_from_sacct, read_record


def axis(result, name):
    return next(d for d in result.directives if d.axis == name)


def record(name) -> list[Observation]:
    return read_record(None, directory=_staged(name))


def _staged(name: str) -> Path:
    return FIXTURES / "_staged" / name


@pytest.fixture(scope="module", autouse=True)
def _stage_records(tmp_path_factory):
    """The record reader expects `<dir>/history.jsonl`; the fixtures are named per case."""
    base = FIXTURES / "_staged"
    for source in FIXTURES.glob("record_*.jsonl"):
        target = base / source.stem
        target.mkdir(parents=True, exist_ok=True)
        (target / "history.jsonl").write_text(source.read_text())
    yield
    for path in base.rglob("history.jsonl"):
        path.unlink()
    for path in sorted(base.glob("*"), reverse=True):
        path.rmdir()
    base.rmdir()


# ------------------------------------------------------------------ confidence


def test_n_zero_refuses_instead_of_inventing():
    result = fit([])
    assert result.refused
    assert "submit it once first" in result.refusals[0]
    assert result.sbatch_block == ""


def test_one_observation_is_stated_as_a_guess(cpu_overask_real):
    result = fit([observation_from_slurmwatch(cpu_overask_real)])
    assert result.confidence.level == "low"
    assert result.confidence.n == 1
    assert "a guess" in result.confidence.statement


def test_four_agreeing_runs_are_a_recommendation():
    result = fit(record("record_four_runs_agreeing_synthetic"))
    assert result.confidence.level == "high"
    assert result.confidence.n == 4
    assert "a recommendation" in result.confidence.statement
    assert axis(result, "memory").confidence.level == "high"


def test_four_disagreeing_runs_are_not_a_recommendation():
    result = fit(record("record_four_runs_spread_synthetic"))
    assert result.confidence.level == "medium"
    assert result.confidence.spread_percent > 10
    assert "not settled" in result.confidence.statement


def test_a_single_recorded_run_stays_low_confidence():
    result = fit(record("record_single_run_synthetic"))
    assert result.confidence.level == "low"
    assert axis(result, "memory").confidence.level == "low"
    assert result.sbatch_block  # low confidence still answers, it just says so


def test_confidence_is_counted_per_axis_not_per_fit(gpu_hbm40, arrow_pagecache):
    with_gpu = observation_from_slurmwatch(gpu_hbm40)
    without = [observation_from_slurmwatch(arrow_pagecache)] * 3
    result = fit([with_gpu, *without])
    assert result.n == 4
    assert axis(result, "gpu_memory").confidence.n == 1
    assert axis(result, "gpu_memory").confidence.level == "low"


def test_a_rollup_cannot_claim_agreement():
    assert sample_confidence(2687, 2.0, rollup=True).level == "medium"
    assert sample_confidence(2687, 2.0, rollup=False).level == "high"


# ----------------------------------------------------------------- directions


def test_memory_comes_down_on_a_real_over_ask(cpu_overask_real):
    """49 GiB asked, 12.4 GiB watermark: cut hard, but not below the watermark."""
    result = fit([observation_from_slurmwatch(cpu_overask_real)])
    memory = axis(result, "memory")
    assert memory.direction == "down"
    assert memory.current == "49.0 GiB"
    assert memory.recommended == "18G"  # ceil(1.4 x 12.4 GiB)
    assert any("does not account for the gap" in w for w in result.warnings)


def test_page_cache_still_comes_off_when_the_cache_reading_carries_the_gap(arrow_pagecache):
    """The Arrow case the exclusion exists for: 96 GiB asked, 9.3 GiB anonymous."""
    result = fit([observation_from_slurmwatch(arrow_pagecache)])
    memory = axis(result, "memory")
    assert memory.direction == "down"
    assert memory.recommended == "14G"  # ceil(1.4 x 9.3 GiB)
    assert any("reclaimable page cache" in w for w in result.warnings)


def test_gpu_memory_goes_up_at_forty_percent_hbm(gpu_hbm40):
    result = fit([observation_from_slurmwatch(gpu_hbm40)])
    gpu = axis(result, "gpu_memory")
    assert gpu.direction == "up"
    assert "2.2x" in gpu.reason
    assert "KV-cache" in gpu.reason
    assert gpu.flag is None  # a script knob, not an #SBATCH flag
    assert axis(result, "memory").direction == "down"  # two-sided, in one pass


def test_gpu_memory_holds_at_ninety_one_percent(gpu_hbm91):
    gpu = axis(fit([observation_from_slurmwatch(gpu_hbm91)]), "gpu_memory")
    assert gpu.direction == "hold"
    assert "Leave it alone" in gpu.reason


def test_gpu_memory_trims_above_the_oom_threshold(gpu_hbm91):
    doc = json.loads(json.dumps(gpu_hbm91))
    doc["gpus"][0]["memory_utilization_percent"] = 97.0
    gpu = axis(fit([observation_from_slurmwatch(doc)]), "gpu_memory")
    assert gpu.direction == "down"
    assert "OOMs" in gpu.reason


def test_a_full_but_idle_card_is_called_a_pipeline_stall(gpu_starved):
    compute = axis(fit([observation_from_slurmwatch(gpu_starved)]), "gpu_compute")
    assert compute.direction == "flag"
    assert "data-pipeline stall" in compute.reason
    assert "will not fix it" in compute.reason


def test_an_empty_idle_card_is_told_to_raise_the_work(gpu_hbm40):
    doc = json.loads(json.dumps(gpu_hbm40))
    doc["gpus"][0]["utilization_percent"] = 22.0
    doc["gpus"][0]["process_utilization_percent"] = 22.0
    compute = axis(fit([observation_from_slurmwatch(doc)]), "gpu_compute")
    assert compute.direction == "flag"
    assert "both low" in compute.reason


def test_a_busy_card_is_left_alone(gpu_hbm91):
    assert axis(fit([observation_from_slurmwatch(gpu_hbm91)]), "gpu_compute").direction == "hold"


def test_gpus_requested_with_no_telemetry_is_unknown(cpu_overask_real):
    request = parse_script("#SBATCH --gres=gpu:2\n#SBATCH --account=pi-example\n")
    gpu = axis(fit([observation_from_slurmwatch(cpu_overask_real)], request=request), "gpu_memory")
    assert gpu.direction == "unknown"
    assert "unknown, not zero" in gpu.reason


def test_zero_cpu_use_is_flagged_rather_than_cut(cpu_overask_real):
    cores = axis(fit([observation_from_slurmwatch(cpu_overask_real)]), "cores")
    assert cores.direction == "flag"
    assert cores.recommended is None
    assert "reservation/idle" in cores.reason


def test_cores_come_down_with_the_sublinear_note():
    result = fit(record("record_four_runs_agreeing_synthetic"))
    cores = axis(result, "cores")
    assert cores.recommended == "16"
    assert "ceil(1.3 x 12.0) = 16" in cores.reason


def test_walltime_from_a_live_job_is_a_floor_not_a_fit(cpu_overask_real):
    result = fit([observation_from_slurmwatch(cpu_overask_real)])
    walltime = axis(result, "walltime")
    assert walltime.direction == "unknown"
    assert "a floor, not a fit" in walltime.reason
    assert any("still running" in w for w in result.warnings)


def test_walltime_comes_down_on_finished_runs():
    walltime = axis(fit(record("record_four_runs_agreeing_synthetic")), "walltime")
    assert walltime.direction == "down"
    assert walltime.recommended == "01:02:00"


def test_a_timeout_run_pushes_walltime_up():
    obs = [Observation(source="sacct", kind="final", job_id="1", state="TIMEOUT",
                       elapsed_seconds=3600, timelimit_seconds=3600),
           Observation(source="sacct", kind="final", job_id="2", state="COMPLETED",
                       elapsed_seconds=3500, timelimit_seconds=3600)]
    walltime = axis(fit(obs), "walltime")
    assert walltime.direction == "up"
    assert "floor, not a fit" in walltime.reason


def test_sacct_core_averages_are_labelled_and_never_high_confidence():
    from conftest import load_text

    obs = [o for o in observations_from_sacct(load_text("sacct_steps_quickstart_real.psv"))
           if o.workload == "caai-quickstart"]
    cores = axis(fit(obs), "cores")
    assert cores.confidence.level == "medium"
    assert "averaged over the run" in cores.reason
    assert "hides bursts" in cores.reason


# ----------------------------------------------------------------- guardrails


def test_nothing_is_recommended_above_a_partition_limit(compute_facts):
    from cloudfit.guard import partition_ceilings

    obs = [Observation(source="record", kind="final", cores_used=140.0,
                       mem_peak_bytes=300 * GIB, mem_cache_measured=False,
                       elapsed_seconds=600, cores_allocated=192)]
    ceilings = partition_ceilings(compute_facts)
    result = fit(obs, ceilings=ceilings)
    assert axis(result, "cores").recommended == "128"
    assert "clamped" in axis(result, "cores").reason
    assert axis(result, "memory").recommended == "244G"


def test_the_block_only_carries_flags_a_fit_changed():
    result = fit(record("record_four_runs_agreeing_synthetic"))
    lines = result.sbatch_block.splitlines()
    assert lines == ["#SBATCH --cpus-per-task=16", "#SBATCH --mem=14G", "#SBATCH --time=01:02:00"]


def test_the_block_supplies_an_account_only_when_the_site_named_one():
    request = parse_script("#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --mem=96G\n")
    runs = record("record_four_runs_agreeing_synthetic")

    told = fit(runs, request=request, account="pi-example")
    assert "#SBATCH --account=pi-example" in told.sbatch_block
    assert any("no --account" in n for n in told.notes)

    # Told nothing, cloudfit invents nothing: no account line, and it says why.
    silent = fit(runs, request=request)
    assert "--account" not in silent.sbatch_block
    assert any("default association" in n for n in silent.notes)
