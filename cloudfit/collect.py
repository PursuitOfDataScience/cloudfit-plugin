"""The subprocess boundary: every external command in cloudfit is issued here.

One `Runner` interface, one real implementation, one fake. Nothing else in the
package calls `subprocess`, which is what makes the decision logic testable
against fixtures and the cloud half testable without a cloud.
"""

from __future__ import annotations

import getpass
import json
import pathlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from . import Observation, parse_slurm_mem, parse_slurm_time

DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    missing: bool = False  # the binary is not on PATH

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.missing

    def json(self) -> object | None:
        try:
            return json.loads(self.stdout)
        except (ValueError, TypeError):
            return None


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> RunResult: ...


class SubprocessRunner:
    """`subprocess.run` with an argument list. No shell, ever."""

    def __init__(self, env: dict[str, str] | None = None):
        self.env = env

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> RunResult:
        argv = [str(a) for a in argv]
        if shutil.which(argv[0]) is None:
            return RunResult(tuple(argv), 127, stderr=f"{argv[0]}: not found", missing=True)
        try:
            proc = subprocess.run(  # noqa: S603 -- argv list, shell=False by construction
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or DEFAULT_TIMEOUT,
                check=False,
                env=self.env,
            )
        except subprocess.TimeoutExpired:
            return RunResult(tuple(argv), 124, stderr="timed out", timed_out=True)
        except OSError as exc:
            return RunResult(tuple(argv), 127, stderr=str(exc), missing=True)
        return RunResult(tuple(argv), proc.returncode, proc.stdout, proc.stderr)


@dataclass
class FakeRunner:
    """The fake backend. Rules are matched in order against the joined argv."""

    rules: list[tuple[Callable[[list[str]], bool], RunResult]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    default: RunResult | None = None

    def on(self, *needles: str, stdout: str = "", returncode: int = 0,
           stderr: str = "", missing: bool = False) -> FakeRunner:
        """Reply to the first argv containing every needle as a substring."""

        def matches(argv: list[str], needles: tuple[str, ...] = needles) -> bool:
            joined = " ".join(argv)
            return all(n in joined for n in needles)

        self.rules.append(
            (matches, RunResult((), returncode, stdout, stderr, missing=missing))
        )
        return self

    def absent(self, *needles: str) -> FakeRunner:
        return self.on(*needles, returncode=127, stderr="not found", missing=True)

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> RunResult:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for matches, result in self.rules:
            if matches(argv):
                return RunResult(tuple(argv), result.returncode, result.stdout,
                                 result.stderr, result.timed_out, result.missing)
        if self.default is not None:
            return RunResult(tuple(argv), self.default.returncode, self.default.stdout,
                             self.default.stderr, missing=self.default.missing)
        return RunResult(tuple(argv), 127, stderr="no fake rule matched", missing=True)

    def argv_containing(self, *needles: str) -> list[list[str]]:
        return [c for c in self.calls if all(n in " ".join(c) for n in needles)]


def default_runner() -> Runner:
    return SubprocessRunner()


# --------------------------------------------------------------- slurmwatch


def observation_from_slurmwatch(doc: dict, *, source: str = "slurmwatch") -> Observation:
    """Normalise one `slurmwatch --once --json` snapshot to the four axes.

    Pure: this is the function the recorded fixtures exercise.
    """
    cpu = doc.get("cpu") or {}
    mem = doc.get("memory") or {}
    gpus = [g for g in (doc.get("gpus") or []) if g.get("memory_available", True)]

    # The fullest card drives the fit; the emptiest drives the imbalance warning.
    hbm = [g["memory_utilization_percent"] for g in gpus
           if g.get("memory_utilization_percent") is not None]
    util = [
        g.get("process_utilization_percent")
        if g.get("process_utilization_available")
        and g.get("process_utilization_percent") is not None
        else g.get("utilization_percent")
        for g in gpus
        if g.get("utilization_available", True)
    ]
    util = [u for u in util if u is not None]

    cores_used = cpu.get("peak_effective_cores")
    if cores_used is None:
        cores_used = cpu.get("effective_cores")

    return Observation(
        source=source,
        kind="sample",
        job_id=str(doc.get("job_id")) if doc.get("job_id") is not None else None,
        workload=doc.get("job_name") or None,
        partition=doc.get("partition") or None,
        state="RUNNING" if doc.get("usage_sampled") else None,
        cores_allocated=cpu.get("cores_allocated"),
        cores_used=cores_used,
        mem_limit_bytes=mem.get("limit_bytes"),
        mem_peak_bytes=mem.get("peak_bytes"),
        mem_peak_working_set_bytes=mem.get("peak_working_set_bytes"),
        mem_cache_bytes=mem.get("cache_bytes"),
        mem_cache_measured=bool(mem.get("cache_measured")),
        mem_peak_is_lifetime=bool(mem.get("peak_is_lifetime")),
        mem_peak_source=mem.get("source"),
        gpu_count=len(gpus) or int(doc.get("gpu_count_requested") or 0),
        gpu_hbm_percent=max(hbm) if hbm else None,
        gpu_util_percent=max(util) if util else None,
        gpu_memory_total_bytes=(max((g.get("memory_total_bytes") or 0) for g in gpus) or None)
        if gpus else None,
        gpu_model=(gpus[0].get("name") if gpus else doc.get("gpu_node_model")) or None,
        elapsed_seconds=doc.get("elapsed_seconds"),
        timelimit_seconds=doc.get("time_limit_seconds"),
        node_count=doc.get("node_count"),
    )


@dataclass
class Measurement:
    observation: Observation | None
    raw: dict | None
    warnings: list[str] = field(default_factory=list)
    gpu_source: str = "none"  # none | login | srun-overlap
    argv: list[list[str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "observation": self.observation.as_dict() if self.observation else None,
            "warnings": list(self.warnings),
            "gpu_source": self.gpu_source,
            "commands": [" ".join(a) for a in self.argv],
        }


def measure(job_id: str, runner: Runner | None = None, *, allow_srun: bool = True) -> Measurement:
    """`slurmwatch --once --json <id>`, with the compute-node hop for GPU fields."""
    runner = runner or default_runner()
    argv = ["slurmwatch", "--once", "--json", str(job_id)]
    result = runner(argv)
    out = Measurement(observation=None, raw=None, argv=[argv])

    if result.missing:
        out.warnings.append("slurmwatch is not on PATH; nothing can be measured live")
        return out

    doc = result.json()
    if not isinstance(doc, dict):
        out.warnings.append(
            f"slurmwatch returned no JSON object (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
        return out

    if not result.ok:
        # Exit 1 is slurmwatch's "no live telemetry"; the object is still printed.
        out.warnings.append(
            "slurmwatch reports no live telemetry (pending, ended, or no such job); "
            "the shape below is facts only"
        )

    if doc.get("mock"):
        out.warnings.append("this is slurmwatch --demo output: simulated, not a measurement")

    # With no Slurm reachable, slurmwatch emits a flat facts-only object instead of
    # the nested telemetry one. Report its reason rather than an all-null observation.
    if doc.get("telemetry_available") is False or "cpu" not in doc:
        reason = doc.get("reason") or doc.get("telemetry_unavailable_reason") or "no reason given"
        out.warnings.append(f"no telemetry: {reason}")
        out.raw = doc
        return out

    gpus_missing = not doc.get("gpus") and int(doc.get("gpu_count_requested") or 0) > 0
    if gpus_missing and allow_srun:
        # GPU fields only populate on the compute node; slurmwatch relocates itself,
        # but when it cannot be granted a step we ask for one explicitly.
        hop = ["srun", f"--jobid={job_id}", "--overlap", "--ntasks=1",
               "slurmwatch", "--once", "--json", str(job_id)]
        out.argv.append(hop)
        hop_result = runner(hop, timeout=180.0)
        hop_doc = hop_result.json()
        if isinstance(hop_doc, dict) and hop_doc.get("gpus"):
            doc = hop_doc
            out.gpu_source = "srun-overlap"
        else:
            out.warnings.append(
                f"{int(doc.get('gpu_count_requested') or 0)} GPU(s) requested but no GPU "
                "telemetry: srun --overlap could not be granted the job's GRES "
                f"({(hop_result.stderr or '').strip()[:120] or 'no detail'}). "
                "GPU axes are unknown, not zero."
            )
    elif doc.get("gpus"):
        out.gpu_source = "login"

    if doc.get("gpu_unavailable_reason"):
        out.warnings.append(f"slurmwatch: {doc['gpu_unavailable_reason']}")
    if (doc.get("node_count") or 1) > 1:
        out.warnings.append(
            f"node {int(doc.get('node_index') or 0)} of {doc['node_count']}: "
            "one node's snapshot, so a straggler on another node is invisible here"
        )

    out.raw = doc
    out.observation = observation_from_slurmwatch(doc)
    return out


# ------------------------------------------------------------- partition facts


@dataclass
class PartitionFacts:
    """What `check` needs to refuse a request the scheduler would reject."""

    name: str
    exists: bool = False
    queried: bool = False  # False means scontrol could not be reached, not "no such partition"
    state: str | None = None
    max_time_seconds: float | None = None
    max_nodes: int | None = None
    max_cpus_per_node: int | None = None
    max_mem_per_node_bytes: int | None = None
    default_mem_per_cpu_bytes: int | None = None
    allowed_accounts: list[str] = field(default_factory=list)
    allowed_qos: list[str] = field(default_factory=list)
    total_cpus: int | None = None
    nodes: str | None = None
    node_cpus_max: int | None = None
    node_mem_max_bytes: int | None = None
    gpu_nodes: list[str] = field(default_factory=list)
    gpu_nodes_known: bool = False

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


@dataclass
class SiteFacts:
    """What this cluster calls things: asked, never assumed.

    cloudfit ships no partition or account names. When a script names no
    partition there is a right answer to substitute, and `sinfo` knows it: the
    default partition is the one it marks with `*`. Same for the account: the
    user's default association is what `sbatch` itself would have used. A site
    that wants to state any of it instead sets the `CLOUDFIT_*` env vars.
    """

    default_partition: str | None = None
    partitions: list[str] = field(default_factory=list)
    user_default_account: str | None = None  # evidence, from sacctmgr
    accounts: list[str] = field(default_factory=list)  # every association this user has
    account_lookup_ok: bool = False  # False means sacctmgr was unreachable, not "no account"
    suggested_account: str | None = None  # preference: profile, env var or caller
    discouraged_partitions: list[str] = field(default_factory=list)
    configured: dict = field(default_factory=dict)  # only what something actually set
    source: dict = field(default_factory=dict)  # per key: profile | env | override

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


SITE_PROFILE_NAME = "site.json"
SITE_FIELDS = ("default_partition", "suggested_account", "discouraged_partitions")


def site_profile_path() -> pathlib.Path:
    from .history import record_dir

    return record_dir() / SITE_PROFILE_NAME


def load_site_profile() -> dict:
    """What a previous session learned about this cluster. Absent is normal."""
    path = site_profile_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in SITE_FIELDS and v}


def save_site_profile(values: dict) -> pathlib.Path:
    """Pin what the agent worked out, so the next session starts knowing it."""
    path = site_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_site_profile() | {k: v for k, v in values.items() if k in SITE_FIELDS and v}
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def site_facts(runner: Runner | None = None, overrides: dict | None = None) -> SiteFacts:
    """What this cluster calls things, in precedence order.

    Discovery (`sinfo`, `sacctmgr`) is the floor, so cloudfit works on a cluster
    nobody has configured it for. On top of that, in order: the saved profile,
    the `CLOUDFIT_*` env vars, and whatever the caller passes right now, which
    is how an agent adapts the plugin mid-session without editing anything.
    """
    import os

    from .guard import (
        ENV_DEFAULT_ACCOUNT,
        ENV_DEFAULT_PARTITION,
        ENV_DISCOURAGED_PARTITIONS,
    )

    runner = runner or default_runner()
    facts = SiteFacts()

    listed = runner(["sinfo", "-h", "-o", "%P"])
    if listed.ok:
        names = listed.stdout.split()
        facts.partitions = [n.rstrip("*") for n in names]
        for name in names:
            if name.endswith("*"):
                facts.default_partition = name.rstrip("*")
                break

    user = getpass.getuser()
    shown = runner(["sacctmgr", "-nP", "show", "user", user, "format=DefaultAccount"])
    if shown.ok:
        facts.account_lookup_ok = True
        lines = [ln.strip() for ln in shown.stdout.splitlines() if ln.strip()]
        facts.user_default_account = lines[0] if lines else None
    assoc = runner(["sacctmgr", "-nP", "show", "assoc", f"user={user}", "format=account"])
    if assoc.ok:
        facts.accounts = sorted({ln.strip() for ln in assoc.stdout.splitlines() if ln.strip()})

    def env_list(raw: str | None) -> list[str] | None:
        items = [p.strip() for p in (raw or "").split(",") if p.strip()]
        return items or None

    layers = [
        ("profile", load_site_profile()),
        ("env", {
            "default_partition": os.environ.get(ENV_DEFAULT_PARTITION, "").strip() or None,
            "suggested_account": os.environ.get(ENV_DEFAULT_ACCOUNT, "").strip() or None,
            "discouraged_partitions": env_list(os.environ.get(ENV_DISCOURAGED_PARTITIONS)),
        }),
        ("override", overrides or {}),
    ]
    for origin, layer in layers:
        for key in SITE_FIELDS:
            value = layer.get(key)
            if value:
                setattr(facts, key, value)
                facts.source[key] = origin

    # Only what something actually set. Discovery is reported, not "configured".
    facts.configured = {k: getattr(facts, k) for k in facts.source}
    return facts


def _unlimited(value: str | None) -> bool:
    return value is None or value.upper() in {"UNLIMITED", "NONE", "N/A", "INFINITE"}


def _scontrol_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            pairs.setdefault(key, value)
    return pairs


def partition_facts(partition: str, runner: Runner | None = None) -> PartitionFacts:
    """`scontrol show partition` + `sinfo`: the live limits, not a hardcoded table."""
    runner = runner or default_runner()
    facts = PartitionFacts(name=partition)

    shown = runner(["scontrol", "show", "partition", partition])
    facts.queried = not (shown.missing or shown.timed_out)
    if shown.ok and "PartitionName=" in shown.stdout:
        pairs = _scontrol_pairs(shown.stdout)
        facts.exists = True
        facts.state = pairs.get("State")
        facts.max_time_seconds = (
            None if _unlimited(pairs.get("MaxTime")) else parse_slurm_time(pairs.get("MaxTime"))
        )
        facts.max_nodes = None if _unlimited(pairs.get("MaxNodes")) else int(pairs["MaxNodes"])
        facts.max_cpus_per_node = (
            None if _unlimited(pairs.get("MaxCPUsPerNode")) else int(pairs["MaxCPUsPerNode"])
        )
        facts.max_mem_per_node_bytes = (
            None if _unlimited(pairs.get("MaxMemPerNode"))
            else parse_slurm_mem(pairs.get("MaxMemPerNode"))
        )
        facts.default_mem_per_cpu_bytes = parse_slurm_mem(pairs.get("DefMemPerCPU"))
        facts.total_cpus = int(pairs["TotalCPUs"]) if pairs.get("TotalCPUs", "").isdigit() else None
        facts.nodes = pairs.get("Nodes")
        accounts = pairs.get("AllowAccounts", "ALL")
        facts.allowed_accounts = [] if accounts.upper() == "ALL" else accounts.split(",")
        qos = pairs.get("AllowQos", "ALL")
        facts.allowed_qos = [] if qos.upper() == "ALL" else qos.split(",")

    # Per-node ceilings and the GRES map, from one sinfo call each.
    sizes = runner(["sinfo", "-p", partition, "-h", "-N", "-o", "%n %c %m"])
    if sizes.ok:
        cpus, mems = [], []
        for line in sizes.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                if parts[1].isdigit():
                    cpus.append(int(parts[1]))
                mem = parse_slurm_mem(parts[2] + "M") if parts[2].isdigit() else None
                if mem:
                    mems.append(mem)
        facts.node_cpus_max = max(cpus) if cpus else None
        facts.node_mem_max_bytes = max(mems) if mems else None

    gres = runner(["sinfo", "-p", partition, "-h", "-N", "-o", "%N %G"])
    if gres.ok:
        facts.gpu_nodes_known = True
        facts.gpu_nodes = sorted(gpu_nodes_from_sinfo(gres.stdout))
    return facts


def gpu_nodes_from_sinfo(text: str) -> set[str]:
    """Nodes with a non-null GRES.

    Filters on GRES, not on the hostname prefix. Clusters routinely put a
    CPU-only node inside a GPU-named series, so a name is not evidence.
    """
    nodes: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        name, gres = parts[0], parts[1].strip()
        if gres and gres != "(null)":
            nodes.add(name)
    return nodes


# ------------------------------------------------------------------- sbatch


@dataclass
class Submission:
    submitted: bool
    job_id: str | None = None
    argv: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    exclusion_verified: str | None = None
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stdout: str = ""

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def sbatch_argv(argv_tail: Sequence[str]) -> list[str]:
    """The exact command `sbatch()` will run, so a dry run cannot misreport it."""
    return ["sbatch", "--parsable", *[str(a) for a in argv_tail]]


def sbatch(argv_tail: Sequence[str], runner: Runner | None = None) -> tuple[str | None, RunResult]:
    """`sbatch --parsable <tail>` -> job id."""
    runner = runner or default_runner()
    argv = sbatch_argv(argv_tail)
    result = runner(argv, timeout=180.0)
    if not result.ok:
        return None, result
    job_id = result.stdout.strip().split(";")[0].strip()
    return (job_id or None), result


def placement(job_id: str, runner: Runner | None = None) -> dict:
    """Where a submitted job actually landed, and whether that node has a GPU.

    `--exclude` silently does nothing when the list is empty, so the exclusion is
    only real once this says so.
    """
    runner = runner or default_runner()
    out: dict = {"job_id": job_id, "nodes": [], "gres": {}, "verified": False}
    q = runner(["squeue", "-j", str(job_id), "-h", "-o", "%T|%R|%N"])
    if not q.ok or not q.stdout.strip():
        out["note"] = "squeue returned nothing; job not placed yet"
        return out
    state, _, rest = q.stdout.strip().partition("|")
    reason, _, nodelist = rest.partition("|")
    out["state"] = state
    out["reason"] = reason
    if not nodelist.strip():
        out["note"] = f"not yet placed ({reason})"
        return out
    expanded = runner(["scontrol", "show", "hostnames", nodelist.strip()])
    names = expanded.stdout.split() if expanded.ok else [nodelist.strip()]
    out["nodes"] = names
    for name in names:
        shown = runner(["scontrol", "show", "node", name])
        pairs = _scontrol_pairs(shown.stdout) if shown.ok else {}
        out["gres"][name] = pairs.get("Gres", "unknown")
    out["verified"] = bool(out["gres"]) and all(v != "unknown" for v in out["gres"].values())
    out["on_gpu_node"] = [n for n, g in out["gres"].items() if g not in {"(null)", "unknown"}]
    return out
