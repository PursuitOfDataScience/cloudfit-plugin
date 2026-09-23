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
#SBATCH --partition=compute
#SBATCH --account=pi-example
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --time=01:00:00
python run.py
"""

GPU_SCRIPT = """#!/bin/bash
#SBATCH -J train-a5
#SBATCH -p gpu
#SBATCH -A pi-example
#SBATCH --gres=gpu:1
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH -t 04:00:00
"""


def test_short_flags_and_equals_forms_parse_the_same():
    request = parse_script(GPU_SCRIPT)
    assert request.job_name == "train-a5"
    assert request.partition == "gpu"
    assert request.account == "pi-example"
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


def test_a_missing_account_is_refused_only_when_the_cluster_says_it_has_none():
    from cloudfit.collect import SiteFacts

    script = parse_script("#SBATCH -p compute\n")
    # No lookup, or a lookup that found a default association: Slurm fills it in.
    assert missing_account(script) is None
    assert missing_account(script, SiteFacts()) is None
    assert missing_account(script, SiteFacts(account_lookup_ok=True,
                                             user_default_account="pi-example")) is None
    # sacctmgr answered, and the answer was empty. Now it really will fail.
    reason = missing_account(script, SiteFacts(account_lookup_ok=True))
    assert "Account is not specified" in reason
    assert "sacctmgr" in reason  # how to find yours, since cloudfit knows no account names
    named = missing_account(script, SiteFacts(account_lookup_ok=True,
                                              suggested_account="pi-example"))
    assert "--account=pi-example" in named


def test_a_script_that_names_its_account_is_never_questioned():
    from cloudfit.collect import SiteFacts

    assert missing_account(parse_script(CPU_SCRIPT), SiteFacts(account_lookup_ok=True)) is None


def test_check_leaves_a_missing_account_alone_on_a_cluster_that_fills_it_in(compute_facts):
    """The bug this replaces: every script without --account was refused, everywhere."""
    from cloudfit.collect import SiteFacts

    without = CPU_SCRIPT.replace("#SBATCH --account=pi-example\n", "")
    assert check_script(without, compute_facts).ok
    assert check_script(without, compute_facts,
                        SiteFacts(account_lookup_ok=True,
                                  user_default_account="pi-example")).ok
    refused = check_script(without, compute_facts, SiteFacts(account_lookup_ok=True))
    assert not refused.ok
    assert any("no --account" in r for r in refused.refusals)


def test_check_passes_a_well_formed_cpu_script(compute_facts):
    result = check_script(CPU_SCRIPT, compute_facts)
    assert result.ok
    assert result.refusals == []


def test_check_refuses_an_account_the_partition_disallows(gpu_facts):
    gpu_facts.allowed_accounts = ["pi-example", "pi-other"]  # the live partition allows ALL
    result = check_script(GPU_SCRIPT.replace("-A pi-example", "-A someone-else"), gpu_facts)
    assert not result.ok
    assert any("AllowAccounts" in r for r in result.refusals)


def test_check_refuses_a_request_no_node_can_satisfy(compute_facts):
    over = CPU_SCRIPT.replace("--cpus-per-task=4", "--cpus-per-task=256")
    reasons = exceeds_partition_limit(parse_script(over), compute_facts)
    assert any("PENDING forever" in r for r in reasons)
    fat = CPU_SCRIPT.replace("--mem=48G", "--mem=400G")
    assert any("PENDING forever" in r for r in exceeds_partition_limit(parse_script(fat), compute_facts))


def test_check_refuses_gpus_on_a_partition_with_none(compute_facts):
    reasons = exceeds_partition_limit(parse_script(CPU_SCRIPT + "#SBATCH --gres=gpu:1\n"), compute_facts)
    assert reasons == []  # the directive is after a command line, so it does not count
    script = CPU_SCRIPT.replace("#SBATCH --mem=48G", "#SBATCH --gres=gpu:1")
    assert any("no node with a GRES" in r
               for r in exceeds_partition_limit(parse_script(script), compute_facts))


def test_check_refuses_a_time_over_the_partition_maxtime(gpu_facts):
    gpu_facts.max_time_seconds = 36 * 3600
    reasons = exceeds_partition_limit(parse_script(GPU_SCRIPT.replace("-t 04:00:00", "-t 5-00:00:00")),
                                      gpu_facts)
    assert any("MaxTime" in r for r in reasons)


def test_a_cpu_job_is_refused_while_gpu_nodes_are_reachable(gpu_facts):
    cpu_on_gpu_partition = CPU_SCRIPT.replace("--partition=compute", "--partition=gpu")
    reason = unguarded_gpu_nodes(parse_script(cpu_on_gpu_partition), gpu_facts)
    assert "squats a card" in reason
    assert "11 node(s)" in reason


def test_a_gpu_job_is_not_asked_to_exclude_gpu_nodes(gpu_facts):
    assert unguarded_gpu_nodes(parse_script(GPU_SCRIPT), gpu_facts) is None


def test_an_already_excluded_partition_is_satisfied(gpu_facts):
    script = CPU_SCRIPT.replace(
        "--partition=compute",
        f"--partition=gpu\n#SBATCH --exclude={','.join(gpu_facts.gpu_nodes)}")
    assert unguarded_gpu_nodes(parse_script(script), gpu_facts) is None


def test_a_cpu_only_partition_needs_no_exclusion(compute_facts):
    assert unguarded_gpu_nodes(parse_script(CPU_SCRIPT), compute_facts) is None
    assert exclusion_ineffective([], compute_facts) is None


def test_an_empty_exclusion_is_called_a_no_op(gpu_facts):
    assert "no-op" in exclusion_ineffective([], gpu_facts)
    assert exclusion_ineffective(["cn-0277"], gpu_facts) is None


def test_landing_on_a_gpu_node_is_refused_after_the_fact():
    placement = {"job_id": "9", "on_gpu_node": ["gn-0010"], "gres": {"gn-0010": "gpu:4"}}
    reason = landed_on_gpu_node(parse_script(CPU_SCRIPT), placement)
    assert "do not let it ride" in reason
    assert landed_on_gpu_node(parse_script(GPU_SCRIPT), placement) is None


def test_policy_is_warned_about_never_refused():
    from cloudfit.collect import SiteFacts

    script = "#SBATCH -p billed\n#SBATCH -A pi-example\n"
    site = SiteFacts(default_partition="compute", discouraged_partitions=["billed"])
    warnings = policy_warnings(parse_script(script), None, site)
    assert any("billed" in w and "discouraged" in w for w in warnings)
    assert any("no --time" in w for w in warnings)
    assert any("no --mem" in w for w in warnings)
    assert check_script(script, None, site).ok


def test_no_partition_is_discouraged_until_a_site_says_so():
    """cloudfit ships no opinion about which partitions cost money."""
    from cloudfit.collect import SiteFacts

    for site in (None, SiteFacts(), SiteFacts(default_partition="compute")):
        warnings = policy_warnings(parse_script("#SBATCH -p billed\n"), None, site)
        assert not any("discouraged" in w for w in warnings)


def test_a_nameless_partition_warning_does_not_invent_a_partition():
    from cloudfit.collect import SiteFacts

    bare = policy_warnings(parse_script("#SBATCH --mem=1G\n"), None, None)
    assert any("whatever this cluster defaults to" in w for w in bare)
    known = policy_warnings(parse_script("#SBATCH --mem=1G\n"), None,
                            SiteFacts(default_partition="normal"))
    assert any("normal is the default here" in w for w in known)


def test_a_script_with_no_directives_is_refused():
    assert not check_script("#!/bin/bash\necho hi\n").ok


def test_a_vm_may_not_outlive_its_work():
    create = ["gcloud", "compute", "instances", "create", "fit-1", "--zone=us-central1-a"]
    assert "outlive" in launch_without_max_run_duration(create)
    assert launch_without_max_run_duration([*create, "--max-run-duration=2h"]) is None
    assert launch_without_max_run_duration(["gcloud", "compute", "instances", "list"]) is None


def test_the_script_argument_is_found_past_the_flags():
    assert script_argument(["sbatch", "--mem=8G", "-p", "compute", "runs/job.sbatch"]) == "runs/job.sbatch"
    assert script_argument(["/usr/bin/sbatch", "job.sh"]) == "job.sh"
    assert script_argument(["sbatch", "--wrap", "echo hi"]) is None


def test_ceilings_and_clamping(compute_facts):
    ceilings = partition_ceilings(compute_facts)
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

    facts = partition_facts("compute", FakeRunner().absent("scontrol").absent("sinfo"))
    assert not facts.queried
    assert not facts.exists
    result = check_script(CPU_SCRIPT, facts)
    assert result.ok  # "I could not check" is not "this is wrong"
    assert any("could not reach scontrol" in w for w in result.warnings)


def test_a_gres_list_counts_only_its_gpus():
    """`--gres=gpu:1,lscratch:10` read as ten GPUs; `lscratch:100` alone made a CPU job a GPU one."""
    assert parse_script("#SBATCH --gres=gpu:1,lscratch:10\n").gpus == 1
    assert parse_script("#SBATCH --gres=gpu:a100:2,tmpspace:50G\n").gpus == 2
    assert parse_script("#SBATCH --gres=lscratch:100\n").gpus == 0
    assert parse_script("#SBATCH --gres=gpu\n").gpus == 1
    assert parse_script("#SBATCH -G 2\n").gpus == 2  # -G is --gpus


def test_a_cpu_job_with_a_scratch_gres_is_still_kept_off_gpu_nodes(gpu_facts):
    script = CPU_SCRIPT.replace("--partition=compute", "--partition=gpu\n#SBATCH --gres=lscratch:100")
    assert "squats a card" in unguarded_gpu_nodes(parse_script(script), gpu_facts)


def test_a_slice_of_a_card_is_still_a_gpu_job(gpu_facts):
    """`--gres=mps:50` is half a card's compute: not fifty GPUs, and not a CPU job either."""
    assert parse_script("#SBATCH --gres=mps:50\n").gpus == 1
    assert parse_script("#SBATCH --gres=shard:2,lscratch:10\n").gpus == 1
    script = CPU_SCRIPT.replace("--partition=compute", "--partition=gpu\n#SBATCH --gres=mps:50")
    assert unguarded_gpu_nodes(parse_script(script), gpu_facts) is None  # it needs a GPU node


def test_attached_short_flags_parse_like_the_spaced_ones():
    request = parse_script("#SBATCH -N1\n#SBATCH -c8\n#SBATCH -pgpu\n#SBATCH -t60\n")
    assert (request.nodes, request.cpus, request.partition) == (1, 8, "gpu")
    assert request.time_seconds == 3600


def test_cpus_counts_every_task_on_the_node():
    assert parse_script("#SBATCH --ntasks-per-node=4\n#SBATCH -c 8\n").cpus == 32
    assert parse_script("#SBATCH --nodes=2\n#SBATCH --ntasks=8\n").cpus == 4
    assert parse_script("#SBATCH -c 8\n").cpus == 8
    # Slurm places `--ntasks` alone wherever it likes: there is no per-node figure yet.
    assert parse_script("#SBATCH --ntasks=256\n").cpus is None


def test_a_limit_check_counts_every_task_on_the_node(compute_facts):
    packed = CPU_SCRIPT.replace("--cpus-per-task=4", "--ntasks-per-node=4\n#SBATCH --cpus-per-task=64")
    assert any("PENDING forever" in r
               for r in exceeds_partition_limit(parse_script(packed), compute_facts))
    # 256 ranks with no --nodes spread over as many nodes as they need; that is not a refusal.
    spread = CPU_SCRIPT.replace("--cpus-per-task=4", "--ntasks=256")
    assert exceeds_partition_limit(parse_script(spread), compute_facts) == []


def test_mem_per_gpu_is_a_memory_request():
    script = "#SBATCH -p gpu\n#SBATCH --gres=gpu:1\n#SBATCH -c 8\n#SBATCH -t 1:00:00\n"
    warnings = policy_warnings(parse_script(script + "#SBATCH --mem-per-gpu=40G\n"))
    assert not any("no --mem" in w for w in warnings)
    assert any("no --mem" in w for w in policy_warnings(parse_script(script)))


def test_command_line_flags_override_the_script():
    request = parse_script(CPU_SCRIPT, ["-p", "gpu", "--time=02:00:00", "--gres=gpu:1"])
    assert request.partition == "gpu"
    assert request.time_seconds == 7200
    assert request.gpus == 1
    assert request.account == "pi-example"  # untouched lines still come from the script


def test_the_sbatch_flags_stop_at_the_script():
    from cloudfit.guard import sbatch_flags

    tokens = ["cd", "runs", "&&", "sbatch", "-p", "gpu", "--parsable", "job.sh", "--lr", "3e-4"]
    assert sbatch_flags(tokens) == ["-p", "gpu", "--parsable"]
    assert sbatch_flags(["squeue", "-u", "me"]) == []


def test_only_gcloud_creating_a_vm_is_held_to_a_deadline():
    assert launch_without_max_run_duration(["echo", "compute", "instances", "create"]) is None
    create = ["/opt/google-cloud-sdk/bin/gcloud", "beta", "compute", "instances", "create", "vm"]
    assert "outlive" in launch_without_max_run_duration(create)
    assert "outlive" in launch_without_max_run_duration(
        ["gcloud", "compute", "instances", "create-with-container", "vm"])


def test_a_termination_time_bounds_a_vm_and_a_termination_action_alone_does_not():
    create = ["gcloud", "compute", "instances", "create", "vm"]
    assert launch_without_max_run_duration([*create, "--termination-time=2026-09-24T00:00:00Z"]) is None
    spot = [*create, "--provisioning-model=SPOT", "--instance-termination-action=DELETE"]
    assert "outlive" in launch_without_max_run_duration(spot)
