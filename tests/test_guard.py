from __future__ import annotations

from cloudfit import GIB
from cloudfit.guard import (
    check_script,
    clamp,
    exceeds_partition_limit,
    exclusion_ineffective,
    landed_on_gpu_node,
    launch_without_max_run_duration,
    missing_account,
    observed_peaks,
    parse_script,
    partition_ceilings,
    policy_warnings,
    script_argument,
    unguarded_gpu_nodes,
)

CPU_SCRIPT = """#!/bin/bash
#SBATCH --job-name=caai-quickstart
#SBATCH --partition=amd
#SBATCH --account=rcc-staff
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --time=01:00:00
python run.py
"""

GPU_SCRIPT = """#!/bin/bash
#SBATCH -J train-a5
#SBATCH -p gpu
#SBATCH -A rcc-staff
#SBATCH --gres=gpu:1
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH -t 04:00:00
"""


def test_short_flags_and_equals_forms_parse_the_same():
    request = parse_script(GPU_SCRIPT)
    assert request.job_name == "train-a5"
    assert request.partition == "gpu"
    assert request.account == "rcc-staff"
    assert request.cpus == 8
    assert request.gpus == 1
    assert request.mem_bytes == 64 * GIB
    assert request.time_seconds == 14400


def test_directives_after_the_first_command_are_ignored_as_slurm_does():
    request = parse_script(CPU_SCRIPT + "#SBATCH --mem=999G\n")
    assert request.mem_bytes == 48 * GIB


def test_trailing_comments_do_not_become_flags():
    request = parse_script("#SBATCH --mem=16G   # padded to be safe\n")
    assert request.mem_bytes == 16 * GIB
    assert not request.has("#")


def test_mem_per_cpu_is_multiplied_out():
    assert parse_script("#SBATCH -c 8\n#SBATCH --mem-per-cpu=2G\n").mem_bytes == 16 * GIB


def test_every_gpu_spelling_is_counted():
    for line in ("--gres=gpu:2", "--gpus=2", "--gpus-per-node=2", "--gpus-per-task=2"):
        assert parse_script(f"#SBATCH {line}\n").gpus == 2
    assert parse_script("#SBATCH --gres=gpu:a100:4\n").gpus == 4


def test_a_missing_account_is_refused_with_the_cryptic_error_named():
    assert missing_account(parse_script(CPU_SCRIPT)) is None
    reason = missing_account(parse_script("#SBATCH -p amd\n"))
    assert "Account is not specified" in reason
    assert "--account=rcc-staff" in reason


def test_check_refuses_a_script_with_no_account(amd_facts):
    without = CPU_SCRIPT.replace("#SBATCH --account=rcc-staff\n", "")
    result = check_script(without, amd_facts)
    assert not result.ok
    assert any("no --account" in r for r in result.refusals)


def test_check_passes_a_well_formed_cpu_script(amd_facts):
    result = check_script(CPU_SCRIPT, amd_facts)
    assert result.ok
    assert result.refusals == []


def test_check_refuses_an_account_the_partition_disallows(gpu_facts):
    gpu_facts.allowed_accounts = ["rcc-staff", "pi-other"]  # the live partition allows ALL
    result = check_script(GPU_SCRIPT.replace("-A rcc-staff", "-A someone-else"), gpu_facts)
    assert not result.ok
    assert any("AllowAccounts" in r for r in result.refusals)


def test_check_refuses_a_request_no_node_can_satisfy(amd_facts):
    over = CPU_SCRIPT.replace("--cpus-per-task=4", "--cpus-per-task=256")
    reasons = exceeds_partition_limit(parse_script(over), amd_facts)
    assert any("PENDING forever" in r for r in reasons)
    fat = CPU_SCRIPT.replace("--mem=48G", "--mem=400G")
    assert any("PENDING forever" in r for r in exceeds_partition_limit(parse_script(fat), amd_facts))


def test_check_refuses_gpus_on_a_partition_with_none(amd_facts):
    reasons = exceeds_partition_limit(parse_script(CPU_SCRIPT + "#SBATCH --gres=gpu:1\n"), amd_facts)
    assert reasons == []  # the directive is after a command line, so it does not count
    script = CPU_SCRIPT.replace("#SBATCH --mem=48G", "#SBATCH --gres=gpu:1")
    assert any("no node with a GRES" in r
               for r in exceeds_partition_limit(parse_script(script), amd_facts))


def test_check_refuses_a_time_over_the_partition_maxtime(gpu_facts):
    gpu_facts.max_time_seconds = 36 * 3600
    reasons = exceeds_partition_limit(parse_script(GPU_SCRIPT.replace("-t 04:00:00", "-t 5-00:00:00")),
                                      gpu_facts)
    assert any("MaxTime" in r for r in reasons)


def test_a_cpu_job_is_refused_while_gpu_nodes_are_reachable(gpu_facts):
    cpu_on_gpu_partition = CPU_SCRIPT.replace("--partition=amd", "--partition=gpu")
    reason = unguarded_gpu_nodes(parse_script(cpu_on_gpu_partition), gpu_facts)
    assert "squats a card" in reason
    assert "11 node(s)" in reason


def test_a_gpu_job_is_not_asked_to_exclude_gpu_nodes(gpu_facts):
    assert unguarded_gpu_nodes(parse_script(GPU_SCRIPT), gpu_facts) is None


def test_an_already_excluded_partition_is_satisfied(gpu_facts):
    script = CPU_SCRIPT.replace(
        "--partition=amd",
        f"--partition=gpu\n#SBATCH --exclude={','.join(gpu_facts.gpu_nodes)}")
    assert unguarded_gpu_nodes(parse_script(script), gpu_facts) is None


def test_a_cpu_only_partition_needs_no_exclusion(amd_facts):
    assert unguarded_gpu_nodes(parse_script(CPU_SCRIPT), amd_facts) is None
    assert exclusion_ineffective([], amd_facts) is None


def test_an_empty_exclusion_is_called_a_no_op(gpu_facts):
    assert "no-op" in exclusion_ineffective([], gpu_facts)
    assert exclusion_ineffective(["midway3-0277"], gpu_facts) is None


def test_landing_on_a_gpu_node_is_refused_after_the_fact():
    placement = {"job_id": "9", "on_gpu_node": ["beagle3-0010"], "gres": {"beagle3-0010": "gpu:4"}}
    reason = landed_on_gpu_node(parse_script(CPU_SCRIPT), placement)
    assert "do not let it ride" in reason
    assert landed_on_gpu_node(parse_script(GPU_SCRIPT), placement) is None


def test_policy_is_warned_about_never_refused():
    warnings = policy_warnings(parse_script("#SBATCH -p caslake\n#SBATCH -A rcc-staff\n"))
    assert any("caslake" in w for w in warnings)
    assert any("no --time" in w for w in warnings)
    assert any("no --mem" in w for w in warnings)
    assert check_script("#SBATCH -p caslake\n#SBATCH -A rcc-staff\n").ok


def test_a_script_with_no_directives_is_refused():
    assert not check_script("#!/bin/bash\necho hi\n").ok


def test_a_vm_may_not_outlive_its_work():
    create = ["gcloud", "compute", "instances", "create", "fit-1", "--zone=us-central1-a"]
    assert "outlive" in launch_without_max_run_duration(create)
    assert launch_without_max_run_duration([*create, "--max-run-duration=2h"]) is None
    assert launch_without_max_run_duration(["gcloud", "compute", "instances", "list"]) is None


def test_the_script_argument_is_found_past_the_flags():
    assert script_argument(["sbatch", "--mem=8G", "-p", "amd", "runs/job.sbatch"]) == "runs/job.sbatch"
    assert script_argument(["/usr/bin/sbatch", "job.sh"]) == "job.sh"
    assert script_argument(["sbatch", "--wrap", "echo hi"]) is None


def test_ceilings_and_clamping(amd_facts):
    ceilings = partition_ceilings(amd_facts)
    assert ceilings["cores"] == 128
    assert ceilings["time_seconds"] is None
    assert clamp(200, 128) == (128, True)
    assert clamp(10, 128) == (10, False)
    assert clamp(10, None) == (10, False)


def test_observed_peaks_are_the_floors():
    from cloudfit import Observation

    obs = [Observation(source="record", cores_used=3.0, mem_peak_bytes=2 * GIB, elapsed_seconds=10),
           Observation(source="record", cores_used=5.0, mem_peak_bytes=4 * GIB, elapsed_seconds=20)]
    peaks = observed_peaks(obs)
    assert peaks == {"cores": 5.0, "mem_bytes": 4 * GIB, "elapsed_seconds": 20}


def test_an_unreachable_scheduler_warns_rather_than_refusing():
    from cloudfit.collect import FakeRunner, partition_facts

    facts = partition_facts("amd", FakeRunner().absent("scontrol").absent("sinfo"))
    assert not facts.queried
    assert not facts.exists
    result = check_script(CPU_SCRIPT, facts)
    assert result.ok  # "I could not check" is not "this is wrong"
    assert any("could not reach scontrol" in w for w in result.warnings)
