from __future__ import annotations

import json

from conftest import load_text

from cloudfit import GIB
from cloudfit.collect import (
    FakeRunner,
    gpu_nodes_from_sinfo,
    measure,
    observation_from_slurmwatch,
    partition_facts,
    placement,
    sbatch,
)


def test_real_cpu_snapshot_normalises_to_the_four_axes(cpu_overask_real):
    obs = observation_from_slurmwatch(cpu_overask_real)
    assert obs.job_id == "58107383"
    assert obs.workload == "build_reserve"
    assert obs.cores_allocated == 6
    assert obs.cores_used == 0.0
    assert obs.mem_limit_bytes == 52613349376
    assert obs.gpu_count == 0
    assert obs.gpu_hbm_percent is None  # no GPU is not 0% GPU
    assert obs.cores_basis == "peak"


def test_page_cache_is_excluded_when_the_cache_reading_explains_the_gap(arrow_pagecache):
    """52.2 GiB of measured cache against a 52.2 GiB gap: subtract it."""
    obs = observation_from_slurmwatch(arrow_pagecache)
    assert obs.mem_disagrees
    assert not obs.mem_peak_understates
    assert obs.mem_peak_trusted_bytes == int(9.3 * GIB)
    assert obs.mem_peak_basis == "working set"


def test_a_gap_page_cache_cannot_explain_is_not_subtracted(cpu_overask_real):
    """Real capture: 12.4 GiB watermark, 0.17 GiB working set, 0.46 GiB of cache.

    Cache carries under 4% of the gap, so the rest is an earlier phase's
    anonymous memory, since freed. A live sample's working set is one instant,
    not a peak -- sizing `--mem` to it would OOM the next run.
    """
    obs = observation_from_slurmwatch(cpu_overask_real)
    assert obs.mem_peak_bytes == 13303283712
    assert obs.mem_peak_working_set_bytes == 184025088
    assert obs.mem_peak_trusted_bytes == 13303283712
    assert obs.mem_peak_basis == "watermark"
    assert not obs.mem_disagrees
    assert obs.mem_peak_understates


def test_a_late_sample_does_not_size_below_an_earlier_phase(cpu_overask_real):
    """The phased-job regression: one late snapshot must not undercut the run.

    Sampled after the heavy phase frees its arrays, the working set reads
    ~1.4% of the watermark. The fit has to hold the watermark anyway.
    """
    late = observation_from_slurmwatch(cpu_overask_real)
    assert late.mem_peak_working_set_bytes < 0.02 * late.mem_peak_bytes
    assert late.mem_peak_trusted_bytes == late.mem_peak_bytes


def test_cache_and_lifetime_fields_are_carried_through(cpu_overask_real):
    obs = observation_from_slurmwatch(cpu_overask_real)
    assert obs.mem_cache_bytes == 493633536
    assert obs.mem_peak_is_lifetime


def test_fullest_card_drives_the_gpu_axes(gpu_pair_uneven):
    obs = observation_from_slurmwatch(gpu_pair_uneven)
    assert obs.gpu_count == 2
    assert obs.gpu_hbm_percent == 64.0
    assert obs.gpu_util_percent == 81.0


def test_gpu_nodes_are_detected_by_gres_not_by_hostname():
    assert gpu_nodes_from_sinfo(load_text("sinfo_compute_nodes_real.txt")) == set()
    gpu = gpu_nodes_from_sinfo(load_text("sinfo_gpu_nodes_real.txt"))
    assert "cn-0277" in gpu
    assert len(gpu) == 11
    mixed = "gn-bigmem1 (null)\ngn-0010 gpu:4\n"
    assert gpu_nodes_from_sinfo(mixed) == {"gn-0010"}


def test_measure_uses_the_login_snapshot_when_it_carries_gpus(gpu_hbm40):
    runner = FakeRunner().on("slurmwatch", stdout=json.dumps(gpu_hbm40))
    result = measure("59200001", runner)
    assert result.gpu_source == "login"
    assert result.observation.gpu_hbm_percent == 40.2
    assert len(runner.calls) == 1  # no hop needed


def test_measure_hops_to_the_compute_node_for_gpu_fields(gpu_hbm40, cpu_overask_real):
    blind = dict(cpu_overask_real, gpus=[], gpu_count_requested=1)
    runner = FakeRunner()
    runner.on("srun", "--overlap", stdout=json.dumps(gpu_hbm40))
    runner.on("slurmwatch", stdout=json.dumps(blind))
    result = measure("59200001", runner)
    assert result.gpu_source == "srun-overlap"
    assert result.observation.gpu_hbm_percent == 40.2
    hop = runner.argv_containing("srun")[0]
    assert hop[:4] == ["srun", "--jobid=59200001", "--overlap", "--ntasks=1"]


def test_missing_gpu_telemetry_is_unknown_not_zero(cpu_overask_real):
    blind = dict(cpu_overask_real, gpus=[], gpu_count_requested=2)
    runner = FakeRunner()
    runner.on("srun", returncode=1, stderr="Unable to create step: Requested GRES not available")
    runner.on("slurmwatch", stdout=json.dumps(blind))
    result = measure("59200001", runner)
    assert result.gpu_source == "none"
    assert result.observation.gpu_hbm_percent is None
    assert any("unknown, not zero" in w for w in result.warnings)


def test_measure_reports_a_missing_binary_rather_than_guessing():
    result = measure("1", FakeRunner().absent("slurmwatch"))
    assert result.observation is None
    assert "not on PATH" in result.warnings[0]


def test_measure_flags_demo_output_as_simulated(gpu_hbm40):
    runner = FakeRunner().on("slurmwatch", stdout=json.dumps(dict(gpu_hbm40, mock=True)))
    result = measure("1", runner)
    assert any("simulated" in w for w in result.warnings)


def test_no_live_telemetry_still_yields_the_shape(cpu_overask_real):
    runner = FakeRunner().on("slurmwatch", stdout=json.dumps(cpu_overask_real), returncode=1)
    result = measure("58107383", runner)
    assert result.observation is not None
    assert any("no live telemetry" in w for w in result.warnings)


def test_partition_facts_read_the_live_limits(compute_facts):
    assert compute_facts.exists
    assert compute_facts.state == "UP"
    assert compute_facts.max_time_seconds is None  # MaxTime=UNLIMITED
    assert compute_facts.node_cpus_max == 128
    assert compute_facts.total_cpus == 5120
    assert compute_facts.gpu_nodes_known
    assert compute_facts.gpu_nodes == []
    assert compute_facts.default_mem_per_cpu_bytes == 1750 * 1024**2


def test_gpu_partition_facts_list_every_gres_node(gpu_facts):
    assert len(gpu_facts.gpu_nodes) == 11
    assert gpu_facts.node_cpus_max == 48


def test_absent_partition_is_reported_as_absent():
    facts = partition_facts("nope", FakeRunner().on("scontrol", returncode=1,
                                                    stderr="Invalid partition name"))
    assert not facts.exists


def test_sbatch_parses_the_job_id():
    runner = FakeRunner().on("sbatch", stdout="59400001;cluster0\n")
    job_id, result = sbatch(["job.sbatch"], runner)
    assert job_id == "59400001"
    assert result.ok
    assert runner.calls[0][:2] == ["sbatch", "--parsable"]


def test_placement_reports_the_gres_of_the_node_it_landed_on():
    runner = (
        FakeRunner()
        .on("squeue", stdout="RUNNING|(None)|gn-0010\n")
        .on("scontrol", "show", "hostnames", stdout="gn-0010\n")
        .on("scontrol", "show", "node", stdout="NodeName=gn-0010 Gres=gpu:4 CPUTot=48\n")
    )
    out = placement("59400001", runner)
    assert out["nodes"] == ["gn-0010"]
    assert out["on_gpu_node"] == ["gn-0010"]
    assert out["verified"]


def test_placement_says_so_when_the_job_is_not_placed_yet():
    runner = FakeRunner().on("squeue", stdout="PENDING|(Priority)|\n")
    out = placement("59400001", runner)
    assert out["nodes"] == []
    assert "not yet placed" in out["note"]


def test_a_machine_with_no_slurm_gets_slurmwatchs_own_reason():
    """slurmwatch switches to a flat facts-only schema when Slurm is unreachable."""
    from conftest import load_json

    doc = load_json("slurmwatch_no_slurm_facts_real.json")
    assert doc["telemetry_available"] is False
    runner = FakeRunner().on("slurmwatch", stdout=json.dumps(doc))
    result = measure("58107383", runner)
    assert result.observation is None  # not an all-null observation
    assert "Slurm binary not found" in result.warnings[-1]
    assert result.raw is not None
    assert len(runner.calls) == 1  # no srun hop attempted


def test_the_flat_schema_is_never_parsed_as_telemetry():
    from conftest import load_json

    obs = observation_from_slurmwatch(load_json("slurmwatch_no_slurm_facts_real.json"))
    assert obs.cores_used is None
    assert obs.mem_peak_bytes is None
    assert obs.gpu_hbm_percent is None


# --------------------------------------------------------------- site facts


def cluster_runner(default_account: str = "pi-smith", partitions: str = "debug normal* gpu"):
    from cloudfit.collect import FakeRunner

    return (
        FakeRunner()
        .on("sinfo", "-h", "-o", "%P", stdout=partitions + "\n")
        .on("sacctmgr", "DefaultAccount", stdout=default_account + "\n")
        .on("sacctmgr", "assoc", stdout="pi-smith\npi-jones\n")
    )


def test_the_default_partition_is_read_off_the_cluster_not_guessed(record_home):
    from cloudfit.collect import site_facts

    facts = site_facts(cluster_runner())
    assert facts.default_partition == "normal"  # the one sinfo marked with *
    assert facts.partitions == ["debug", "normal", "gpu"]
    assert facts.user_default_account == "pi-smith"
    assert facts.accounts == ["pi-jones", "pi-smith"]
    assert facts.configured == {}  # discovery is not configuration


def test_a_cluster_that_answers_nothing_leaves_every_name_unset(record_home):
    from cloudfit.collect import FakeRunner, site_facts

    facts = site_facts(FakeRunner().absent("sinfo").absent("sacctmgr"))
    assert facts.default_partition is None
    assert facts.suggested_account is None
    assert facts.account_lookup_ok is False  # unreachable, not "you have no account"
    assert facts.discouraged_partitions == []


def test_env_overrides_the_cluster_and_the_caller_overrides_the_env(record_home, monkeypatch):
    from cloudfit.collect import site_facts

    monkeypatch.setenv("CLOUDFIT_DEFAULT_PARTITION", "bigmem")
    monkeypatch.setenv("CLOUDFIT_DEFAULT_ACCOUNT", "pi-jones")
    monkeypatch.setenv("CLOUDFIT_DISCOURAGED_PARTITIONS", "billed, gpu-preempt")

    from_env = site_facts(cluster_runner())
    assert from_env.default_partition == "bigmem"
    assert from_env.suggested_account == "pi-jones"
    assert from_env.discouraged_partitions == ["billed", "gpu-preempt"]
    assert from_env.source["default_partition"] == "env"

    on_the_fly = site_facts(cluster_runner(), {"default_partition": "gpu"})
    assert on_the_fly.default_partition == "gpu"
    assert on_the_fly.source["default_partition"] == "override"


def test_a_saved_profile_survives_into_the_next_session(record_home):
    from cloudfit.collect import load_site_profile, save_site_profile, site_facts

    save_site_profile({"default_partition": "normal", "suggested_account": "pi-smith",
                       "ignored": "not a site field"})
    assert load_site_profile() == {"default_partition": "normal",
                                   "suggested_account": "pi-smith"}
    facts = site_facts(cluster_runner(partitions="debug gpu*"))
    assert facts.default_partition == "normal"  # the profile beat the cluster's own default
    assert facts.source["suggested_account"] == "profile"
