"""The decision logic: the only genuinely new code here.

Telemetry in, directives plus per-directive reasoning plus confidence out. No
subprocess calls in this module, by design: everything it needs arrives as
`Observation` objects, which is what lets the recorded fixtures test it.

The corrections are deliberately two-sided. Cores, host RAM and walltime are
over-requested and come down; GPU memory is under-driven and goes up. Presenting
them as one pass is the point of the tool.
"""

from __future__ import annotations

import math
from dataclasses import replace

from . import (
    AGREEMENT_PERCENT,
    CORE_HEADROOM,
    GIB,
    GPU_COMPUTE_FLOOR_PERCENT,
    HBM_RAISE_BELOW,
    HBM_TARGET_PERCENT,
    HBM_TRIM_ABOVE,
    MEM_HEADROOM,
    RECOMMENDED_N,
    WALLTIME_HEADROOM,
    Confidence,
    Directive,
    FitResult,
    Observation,
    fmt_gib,
    fmt_slurm_time,
    spread_percent,
)
from . import guard as _guard
from .guard import SbatchRequest

# The two ends a run can reach that say how long its work takes: all of it, or
# at least its limit. FAILED, CANCELLED, OUT_OF_MEMORY and the rest say only
# where it stopped.
RAN_TO_AN_END = {"COMPLETED", "TIMEOUT"}


def runs_behind(observations: list[Observation]) -> int:
    """How many runs the observations describe, each counted once.

    A slurmpast rollup arrives as one observation per axis, each carrying its
    own run count, so summing `runs` counts one workload once per axis: 100
    runs read as n=212. A rollup counts once, at its best-covered axis.
    """
    rollups: dict[tuple, int] = {}
    n = 0
    for obs in observations:
        if obs.kind == "rollup":
            key = (obs.source, obs.workload, obs.partition)
            rollups[key] = max(rollups.get(key, 0), obs.runs)
        else:
            n += obs.runs
    return n + sum(rollups.values())


_MEM_FIELDS = ("mem_limit_bytes", "mem_peak_bytes", "mem_peak_working_set_bytes",
               "mem_cache_bytes", "mem_cache_measured", "mem_peak_is_lifetime", "mem_peak_source")


def one_per_run(observations: list[Observation]) -> list[Observation]:
    """Collapse repeated snapshots of one job into the one run they are.

    `measure` records every call, so a job sampled four times would read as
    four agreeing runs: a recommendation from one job. Snapshots of a job from
    one source become one observation, the latest, lifted to the busiest
    reading any snapshot saw on each axis. The same job from a second source is
    not a second run either, so only the first source's copy is kept. Rollups
    and observations with no job id pass through untouched.
    """
    groups: dict[str, list[Observation]] = {}
    order: list[Observation | str] = []
    for obs in observations:
        if obs.job_id is None or obs.kind == "rollup":
            order.append(obs)
        elif obs.job_id not in groups:
            groups[obs.job_id] = [obs]
            order.append(obs.job_id)
        elif groups[obs.job_id][0].source == obs.source:
            groups[obs.job_id].append(obs)
    return [_merged(groups[item]) if isinstance(item, str) else item for item in order]


def _merged(snapshots: list[Observation]) -> Observation:
    if len(snapshots) == 1:
        return snapshots[0]

    def top(attr: str):
        values = [getattr(s, attr) for s in snapshots if getattr(s, attr) is not None]
        return max(values) if values else None

    finals = [s for s in snapshots if s.kind == "final"]
    base = finals[-1] if finals else snapshots[-1]
    # Memory fields travel together: which peak is trusted depends on all of them.
    fullest = max(snapshots, key=lambda s: s.mem_peak_trusted_bytes or -1)
    return replace(
        base,
        **{name: getattr(fullest, name) for name in _MEM_FIELDS},
        cores_used=top("cores_used"),
        gpu_count=top("gpu_count") or 0,
        gpu_hbm_percent=top("gpu_hbm_percent"),
        gpu_util_percent=top("gpu_util_percent"),
        gpu_memory_total_bytes=top("gpu_memory_total_bytes"),
        gpu_model=base.gpu_model or next((s.gpu_model for s in snapshots if s.gpu_model), None),
        elapsed_seconds=top("elapsed_seconds"),
    )


def _axis_sample(observations: list[Observation],
                 attr: str) -> tuple[list[float], int, float | None]:
    """Values, effective n and relative spread for one axis.

    n is counted per axis, not per fit: a workload with four runs where only one
    carried GPU numbers is still n=1 on the GPU axes, and must say so.
    """
    values, n = [], 0
    for obs in observations:
        value = getattr(obs, attr, None) if attr != "mem" else obs.mem_peak_trusted_bytes
        if value is None:
            continue
        values.append(float(value))
        n += obs.runs
    return values, n, spread_percent(values)


def sample_confidence(n: int, spread: float | None, *, rollup: bool = False,
                      axis: str = "") -> Confidence:
    """Confidence is a property of the sample and is always stated with it."""
    where = f"{axis}: " if axis else ""
    if n <= 0:
        return Confidence("none", 0, None, f"{where}no data on this axis")
    if n == 1:
        return Confidence("low", 1, None,
                          f"{where}n=1: one observation is a guess, not a recommendation")
    if n < RECOMMENDED_N:
        return Confidence("medium", n, spread,
                          f"{where}n={n}: tentative; {RECOMMENDED_N} agreeing runs is the bar")
    if rollup:
        return Confidence("medium", n, None,
                          f"{where}n={n} from a rollup: per-run spread is not recoverable, "
                          "so agreement cannot be checked")
    if spread is None:
        return Confidence("medium", n, None, f"{where}n={n} but the spread is unknown")
    if spread <= AGREEMENT_PERCENT:
        return Confidence("high", n, spread,
                          f"{where}n={n} agreeing within {spread:g}%: a recommendation")
    return Confidence("medium", n, spread,
                      f"{where}n={n} but they spread {spread:g}%; the peak is not settled")


def _direction(recommended: float, current: float | None) -> str:
    if current is None:
        return "unknown"
    if recommended < current * 0.98:
        return "down"
    if recommended > current * 1.02:
        return "up"
    return "hold"


def _cores_per_task(obs: Observation, request: SbatchRequest | None) -> float | None:
    """One observation's cores, in the unit `--cpus-per-task` is written in.

    slurmpast reports per task already, slurmwatch reads one node's cgroup, and
    sacct's CPU-seconds cover every task on every node. For the one-task job
    most scripts are, all three are the same number. With no script there is
    no layout to split by, so the reading stands as one task's. None where the
    split is not knowable: Slurm spread `--ntasks` over several nodes itself.
    """
    if obs.cores_used is None or obs.cores_scope == "task" or request is None:
        return obs.cores_used
    if obs.cores_scope == "job":
        tasks = request.total_tasks or request.nodes or obs.node_count or 1
    elif request.tasks_per_node is not None:
        tasks = request.tasks_per_node
    elif obs.node_count == 1:
        tasks = request.total_tasks  # every task Slurm placed was on the node it read
    else:
        return None
    return obs.cores_used / max(tasks or 1, 1)


def _cores(observations, request, ceiling) -> Directive | None:
    readings = [(o, _cores_per_task(o, request)) for o in observations
                if o.cores_used is not None]
    usable = [(o, v) for o, v in readings if v is not None]
    values = [v for _, v in usable]
    n = sum(o.runs for o, _ in usable)
    rollup = any(o.kind == "rollup" for o in observations)
    conf = sample_confidence(n, spread_percent(values), rollup=rollup, axis="cores")
    averaged = bool(usable) and all(o.cores_basis == "average" for o, _ in usable)
    if averaged and conf.level == "high":
        # An average is not a peak: 1.3x a run average can still sit under a burst.
        conf = Confidence("medium", conf.n, conf.spread_percent,
                          f"cores: n={conf.n} agreeing within {conf.spread_percent:g}%, but every "
                          "reading is a run average (sacct CPU-seconds), not a peak")

    per_node = request.tasks_per_node if request else 1  # None: Slurm chose the spread
    total = (request.total_tasks if request else None) or 1
    current = request.cpus_per_task if request else None
    if current is None and request is not None and (request.has("--ntasks")
                                                    or request.has("--ntasks-per-node")):
        current = 1  # Slurm's own default: one core per task
    if current is None:
        current = next((o.cores_allocated for o in observations if o.cores_allocated), None)

    if not values:
        reason = "no CPU utilisation was measured, so the request cannot be fitted"
        if readings:
            reason = (
                f"the script runs {request.total_tasks} tasks with no --nodes or "
                "--ntasks-per-node, and Slurm spread them over several nodes, so a "
                "per-task figure cannot be read off one node's cgroup. Pin the layout "
                "and fit again"
            )
        return Directive("cores", "unknown", reason, conf, flag="--cpus-per-task",
                         current=str(current) if current else None)

    peak = max(values)
    per_task_ceiling = max(1, int(ceiling // (per_node or 1))) if ceiling else None
    recommended = max(1, math.ceil(CORE_HEADROOM * peak))
    recommended, clamped = _guard.clamp(recommended, per_task_ceiling)
    recommended = int(recommended)

    if peak == 0:
        reason = (
            "no CPU work at all was observed (0.0 effective cores). Either this is a "
            "reservation/idle allocation, or the sample landed between bursts. Do not "
            "cut cores on this evidence alone"
        )
        return Directive("cores", "flag", reason, conf, flag="--cpus-per-task",
                         current=str(current) if current else None, recommended=None,
                         observed="0.0 cores busy at peak")

    basis = "averaged over the run" if averaged else "busiest run used"
    split = any(v != o.cores_used for o, v in usable)
    unit = " per task" if split or total > 1 or (per_node or 1) > 1 else ""
    reason = (
        f"{basis} {peak:.1f} of {current or '?'} cores{unit}; "
        f"ceil({CORE_HEADROOM} x {peak:.1f}) = {recommended}"
    )
    if (per_node or 1) > 1:
        reason += f" per task, for {per_node} tasks on each node"
    elif per_node is None:
        reason += f" per task, for {total} tasks"
    if split:
        reason += (
            ". That splits a whole-node or whole-job reading evenly across the tasks: one "
            "that does more than its share (a rank that also loads data) needs more than this"
        )
    if averaged:
        reason += (
            ". This is sacct's CPU-seconds / elapsed, an average that hides bursts, so "
            "confirm with slurmwatch's peak_effective_cores before cutting"
        )
    if clamped:
        reason += f", clamped to the partition ceiling of {int(ceiling)}"
        if (per_node or 1) > 1:
            reason += f" shared by {per_node} tasks"
    if current and peak / current < 0.5:
        reason += (f". Cores scale sublinearly for many tools: {peak:.1f}/{current} is "
                   f"{peak / current:.0%}")
    return Directive("cores", _direction(recommended, current), reason, conf,
                     flag="--cpus-per-task", current=str(current) if current else None,
                     recommended=str(recommended),
                     observed=f"{peak:.1f} cores busy{unit} at peak")


def _memory(observations, request, ceiling) -> tuple[Directive | None, list[str]]:
    values, n, spread = _axis_sample(observations, "mem")
    rollup = any(o.kind == "rollup" for o in observations)
    conf = sample_confidence(n, spread, rollup=rollup, axis="host RAM")
    warnings: list[str] = []
    current = request.mem_bytes if request else None
    if current is None:
        current = next((o.mem_limit_bytes for o in observations if o.mem_limit_bytes), None)

    if not values:
        return Directive("memory", "unknown",
                         "no memory peak was measured (JobAcctGatherType off, or the job never "
                         "reported), so the request cannot be fitted",
                         conf, flag="--mem",
                         current=fmt_gib(current) if current else None), warnings

    peak = max(values)
    recommended_bytes = MEM_HEADROOM * peak
    recommended_gib = max(1, math.ceil(recommended_bytes / GIB))
    clamped_bytes, clamped = _guard.clamp(recommended_gib * GIB, ceiling)
    if clamped:
        recommended_gib = max(1, int(clamped_bytes // GIB))

    reason = (
        f"peak was {fmt_gib(peak)}; ceil({MEM_HEADROOM} x {fmt_gib(peak)}) = {recommended_gib}G"
    )
    if clamped:
        reason += f", clamped to the largest node in the partition ({fmt_gib(ceiling)})"

    disagreeing = [o for o in observations if o.mem_disagrees]
    if disagreeing:
        worst = max(disagreeing, key=lambda o: (o.mem_peak_bytes or 0))
        warnings.append(
            f"memory.peak {fmt_gib(worst.mem_peak_bytes)} vs anonymous working set "
            f"{fmt_gib(worst.mem_peak_working_set_bytes)} on job {worst.job_id}, and the "
            f"{fmt_gib(worst.mem_cache_bytes)} of reclaimable page cache measured alongside it "
            "accounts for the gap (Arrow-backed datasets inflate it badly). Sized to the working "
            "set, which is the OOM-relevant number, not to the larger figure."
        )
        reason += "; page cache excluded"

    understating = [o for o in observations if o.mem_peak_understates]
    if understating:
        worst = max(understating, key=lambda o: (o.mem_peak_bytes or 0))
        warnings.append(
            f"job {worst.job_id} read a {fmt_gib(worst.mem_peak_working_set_bytes)} working set "
            f"against a {fmt_gib(worst.mem_peak_bytes)} cgroup watermark, and the measured page "
            "cache does not account for the gap: an earlier phase held more and freed it. A live "
            "sample's working set is one instant, not a peak, so this is sized to the watermark."
        )
        reason += "; sized to the cgroup lifetime watermark, not the sampled working set"

    direction = _direction(recommended_gib * GIB, current)
    if current and recommended_gib * GIB < current:
        reason += f", down from {fmt_gib(current)}"
    return Directive("memory", direction, reason, conf, flag="--mem",
                     current=fmt_gib(current) if current else None,
                     recommended=f"{recommended_gib}G",
                     observed=f"{fmt_gib(peak)} peak"), warnings


def _gpu_memory(observations, request) -> Directive | None:
    gpu_obs = [o for o in observations if o.gpu_count]
    asked = request.gpus if request else 0
    if not gpu_obs and not asked:
        return None
    values, n, spread = _axis_sample(gpu_obs, "gpu_hbm_percent")
    conf = sample_confidence(n, spread, axis="GPU memory")
    if not values:
        return Directive("gpu_memory", "unknown",
                         "GPUs were requested but no HBM telemetry came back; the GPU axes are "
                         "unknown, not zero (run measure on the compute node)", conf)

    hbm = max(values)
    total = next((o.gpu_memory_total_bytes for o in gpu_obs if o.gpu_memory_total_bytes), None)
    model = next((o.gpu_model for o in gpu_obs if o.gpu_model), None) or "the card"
    used = f" ({fmt_gib(total * hbm / 100)} of {fmt_gib(total)})" if total else ""

    if hbm < HBM_RAISE_BELOW:
        scale = HBM_TARGET_PERCENT / max(hbm, 1.0)
        return Directive(
            "gpu_memory", "up",
            f"{model} is at {hbm:.0f}% HBM{used}: the expensive resource is idle. Raise batch "
            f"size / K / sequence length / KV-cache by roughly {scale:.1f}x to reach the "
            f"~{HBM_TARGET_PERCENT:.0f}% target. This is a knob in the training or inference "
            "script, not an #SBATCH flag",
            conf, current=f"{hbm:.0f}% HBM", recommended=f"~{HBM_TARGET_PERCENT:.0f}% HBM",
            observed=f"{hbm:.0f}% HBM at peak")
    if hbm > HBM_TRIM_ABOVE:
        return Directive(
            "gpu_memory", "down",
            f"{model} is at {hbm:.0f}% HBM{used}: above ~{HBM_TRIM_ABOVE:.0f}% a transient "
            f"allocation OOMs. Trim the knobs back towards {HBM_TARGET_PERCENT:.0f}%",
            conf, current=f"{hbm:.0f}% HBM", recommended=f"~{HBM_TARGET_PERCENT:.0f}% HBM",
            observed=f"{hbm:.0f}% HBM at peak")
    return Directive(
        "gpu_memory", "hold",
        f"{model} is at {hbm:.0f}% HBM{used}: inside the ~{HBM_TARGET_PERCENT:.0f}% target band. "
        "Leave it alone",
        conf, current=f"{hbm:.0f}% HBM", recommended=f"{hbm:.0f}% HBM",
        observed=f"{hbm:.0f}% HBM at peak")


def _gpu_compute(observations, request) -> Directive | None:
    gpu_obs = [o for o in observations if o.gpu_count]
    if not gpu_obs and not (request.gpus if request else 0):
        return None
    values, n, spread = _axis_sample(gpu_obs, "gpu_util_percent")
    conf = sample_confidence(n, spread, axis="GPU compute")
    if not values:
        return None  # _gpu_memory already reported the missing telemetry
    util = max(values)
    hbm = max((o.gpu_hbm_percent or 0) for o in gpu_obs)

    if util >= GPU_COMPUTE_FLOOR_PERCENT:
        return Directive("gpu_compute", "hold",
                         f"GPU compute at {util:.0f}%: the card is the bottleneck, which is where "
                         "it should be", conf, current=f"{util:.0f}%", recommended=f"{util:.0f}%",
                         observed=f"{util:.0f}% utilisation")
    if hbm >= HBM_RAISE_BELOW:
        reason = (
            f"GPU compute is {util:.0f}% while HBM is {hbm:.0f}% full: the card is loaded and "
            "idle, so this is a data-pipeline stall (loader workers, tokenisation, I/O), not a "
            "sizing problem. Raising batch size will not fix it"
        )
    else:
        reason = (
            f"GPU compute is {util:.0f}% and HBM is only {hbm:.0f}%: both low. Raise the work per "
            "step (batch / packing / fewer grad-accum micro-steps) before blaming the pipeline"
        )
    return Directive("gpu_compute", "flag", reason, conf, current=f"{util:.0f}%",
                     recommended=f">{GPU_COMPUTE_FLOOR_PERCENT:.0f}%",
                     observed=f"{util:.0f}% utilisation")


def _walltime(observations, request, ceiling) -> tuple[Directive | None, list[str]]:
    warnings: list[str] = []
    finished = [o for o in observations if o.kind in {"final", "rollup"}]
    ended = [o for o in finished if (o.state or "COMPLETED").upper() in RAN_TO_AN_END]
    stopped = sorted({(o.state or "").upper() for o in finished
                      if (o.state or "COMPLETED").upper() not in RAN_TO_AN_END})
    live = [o for o in observations if o.kind == "sample"]
    pool = ended or live
    values, n, spread = _axis_sample(pool, "elapsed_seconds")
    rollup = any(o.kind == "rollup" for o in pool)
    conf = sample_confidence(n, spread, rollup=rollup, axis="walltime")
    current = request.time_seconds if request else None
    if current is None:
        current = next((o.timelimit_seconds for o in observations if o.timelimit_seconds), None)
    current_text = fmt_slurm_time(current) if current else None

    if not values:
        reason = "no elapsed time recorded"
        if stopped:
            reason = (f"no run finished its work: they ended {'/'.join(stopped)}, and that "
                      "elapsed time is where a run stopped, not how long the work takes")
        return Directive("walltime", "unknown", reason, conf,
                         flag="--time", current=current_text), warnings

    peak = max(values)
    timeouts = [o for o in pool if (o.state or "").upper() == "TIMEOUT"]
    if not ended:
        warnings.append(
            "walltime is based on a job that is still running: elapsed so far is a floor, not the "
            "run's duration. Fit it again after it finishes"
        )
        return Directive("walltime", "unknown",
                         f"still running, {fmt_slurm_time(peak)} elapsed so far, which is a floor, "
                         "not a fit",
                         conf, flag="--time", current=current_text,
                         observed=f"{fmt_slurm_time(peak)} so far"), warnings

    recommended = WALLTIME_HEADROOM * peak
    recommended, clamped = _guard.clamp(recommended, ceiling)
    text = fmt_slurm_time(recommended)
    reason = (
        f"longest of {n} run(s) took {fmt_slurm_time(peak)}; {WALLTIME_HEADROOM} x that is {text}"
    )
    if clamped:
        reason += f", clamped to the partition MaxTime ({fmt_slurm_time(ceiling)})"
    if stopped:
        reason += (f". Runs that ended {'/'.join(stopped)} are not counted: their elapsed time "
                   "is where they stopped")
    direction = _direction(recommended, current)
    if timeouts:
        reason += (
            f". {len(timeouts)} run(s) hit TIMEOUT, so the requirement is at least the limit that "
            "cut them off. This is a floor, not a fit"
        )
        if current and recommended < current:
            # The evidence is a floor under the need, so it can raise a limit but never cut one.
            recommended = min(current, ceiling) if ceiling else current
            text = fmt_slurm_time(recommended)
            if recommended == current:
                reason += f", so the current {current_text} stays rather than coming down"
            else:
                reason += f", so it comes down only as far as the partition MaxTime ({text})"
        direction = "up" if current is None else _direction(recommended, current)
    return Directive("walltime", direction, reason, conf, flag="--time",
                     current=current_text, recommended=text,
                     observed=f"{fmt_slurm_time(peak)} longest"), warnings


def render_block(directives: list[Directive], request: SbatchRequest | None = None,
                 account: str | None = None) -> str:
    """The corrected `#SBATCH` block: only the lines a fit actually changed."""
    lines: list[str] = []
    if request is not None and not request.account and account:
        lines.append(f"#SBATCH --account={account}")
    for d in directives:
        if d.flag and d.recommended and d.direction in {"down", "up", "hold"}:
            lines.append(f"#SBATCH {d.flag}={d.recommended}")
    return "\n".join(lines)


def fit(observations: list[Observation], *, request: SbatchRequest | None = None,
        ceilings: dict | None = None, source: str = "unknown",
        workload: str | None = None, notes: list[str] | None = None,
        account: str | None = None) -> FitResult:
    """Fit a workload from its observations. The whole decision, in one call."""
    ceilings = ceilings or {}
    notes = list(notes or [])
    workload = workload or next((o.workload for o in observations if o.workload), None)

    n_total = runs_behind(observations)
    refusal = _guard.insufficient_sample(n_total)
    if refusal:
        return FitResult(workload=workload, source=source, n=0,
                         confidence=Confidence("none", 0, None, "no sample"),
                         refusals=[refusal], notes=notes)

    rollup = any(o.kind == "rollup" for o in observations)
    overall_spread = spread_percent([o.mem_peak_trusted_bytes for o in observations
                                     if o.mem_peak_trusted_bytes is not None])

    warnings: list[str] = []
    directives: list[Directive] = []

    cores = _cores(observations, request, ceilings.get("cores"))
    memory, mem_warnings = _memory(observations, request, ceilings.get("mem_bytes"))
    walltime, wall_warnings = _walltime(observations, request, ceilings.get("time_seconds"))
    warnings += mem_warnings + wall_warnings
    for d in (cores, memory, _gpu_memory(observations, request),
              _gpu_compute(observations, request), walltime):
        if d is not None:
            directives.append(d)

    # Self-check: a recommendation below a measured peak is a bug, not advice. Cores
    # are compared per task, the unit the flag is written in.
    peaks = _guard.observed_peaks(observations)
    per_task = [v for v in (_cores_per_task(o, request) for o in observations) if v is not None]
    peaks["cores"] = max(per_task) if per_task else None
    refusals: list[str] = []
    for d in directives:
        if d.axis == "cores" and d.recommended:
            refusals.append(_guard.below_observed_peak("cores", float(d.recommended),
                                                       peaks["cores"], " cores"))
        if d.axis == "memory" and d.recommended and peaks["mem_bytes"]:
            gib = float(d.recommended.rstrip("G"))
            refusals.append(_guard.below_observed_peak("memory", gib,
                                                       peaks["mem_bytes"] / GIB, " GiB"))
    refusals = [r for r in refusals if r]

    per_unit = next((f for f in ("--mem-per-cpu", "--mem-per-gpu")
                     if request is not None and request.has(f)), None)
    if per_unit and memory is not None and memory.recommended:
        notes.append(f"the block's --mem replaces the script's {per_unit}: sbatch refuses a "
                     "script that sets both")

    if request is not None and not request.account:
        notes.append(
            f"the script has no --account; the block adds --account={account}" if account
            else "the script has no --account, so Slurm will use your default association"
        )

    return FitResult(
        workload=workload,
        source=source,
        n=n_total,
        confidence=sample_confidence(n_total, overall_spread, rollup=rollup),
        directives=directives,
        sbatch_block="" if refusals else render_block(directives, request, account),
        warnings=warnings,
        refusals=refusals,
        notes=notes,
    )
