"""The guardrails, as pure predicates.

Each predicate returns a reason string when it refuses and `None` when it is
satisfied, so the caller never has to restate the rule.

The line between the two severities: a **refusal** is something the scheduler
would reject, something that silently squats a resource someone else needs, or a
number that is knowably wrong. A **warning** is policy (the local default, the
partition that bills), which the user is allowed to overrule.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from . import GIB, Observation, fmt_gib, fmt_slurm_time, parse_slurm_mem, parse_slurm_time

# No partition, account or "billed partition" name is baked in. Those are site
# policy, and a plugin that ships one cluster's names checks every other
# cluster's scripts against limits that belong to nothing. Everything
# site-specific arrives as `SiteFacts`, read from the cluster itself or from
# these env vars when a site wants to state it. This module stays pure: it
# never reads the environment, so the same inputs always give the same verdict.
ENV_DEFAULT_PARTITION = "CLOUDFIT_DEFAULT_PARTITION"
ENV_DEFAULT_ACCOUNT = "CLOUDFIT_DEFAULT_ACCOUNT"
ENV_DISCOURAGED_PARTITIONS = "CLOUDFIT_DISCOURAGED_PARTITIONS"


_FLAG_ALIASES = {
    "-p": "--partition",
    "-A": "--account",
    "-c": "--cpus-per-task",
    "-t": "--time",
    "-n": "--ntasks",
    "-N": "--nodes",
    "-J": "--job-name",
    "-w": "--nodelist",
    "-x": "--exclude",
}


@dataclass
class SbatchRequest:
    """What a script asks for. Parsed from `#SBATCH` lines, in script order."""

    directives: list[tuple[int, str, str | None]] = field(default_factory=list)
    text: str = ""

    def get(self, flag: str) -> str | None:
        for _, name, value in reversed(self.directives):
            if name == flag:
                return value
        return None

    def has(self, flag: str) -> bool:
        return any(name == flag for _, name, _ in self.directives)

    @property
    def partition(self) -> str | None:
        return self.get("--partition")

    @property
    def account(self) -> str | None:
        return self.get("--account")

    @property
    def job_name(self) -> str | None:
        return self.get("--job-name")

    @property
    def cpus(self) -> int | None:
        for flag in ("--cpus-per-task", "--ntasks-per-node", "--ntasks"):
            value = self.get(flag)
            if value and value.isdigit():
                return int(value)
        return None

    @property
    def nodes(self) -> int | None:
        value = self.get("--nodes")
        if not value:
            return None
        head = value.split("-")[0]
        return int(head) if head.isdigit() else None

    @property
    def mem_bytes(self) -> int | None:
        total = parse_slurm_mem(self.get("--mem"))
        if total is not None:
            return total
        per_cpu = parse_slurm_mem(self.get("--mem-per-cpu"))
        if per_cpu is not None:
            return per_cpu * (self.cpus or 1)
        return None

    @property
    def time_seconds(self) -> float | None:
        return parse_slurm_time(self.get("--time"))

    @property
    def gpus(self) -> int:
        """GPU count from whichever of the four spellings the script used."""
        for flag in ("--gres", "--gpus", "--gpus-per-node", "--gpus-per-task"):
            value = self.get(flag)
            if not value:
                continue
            m = re.search(r"(\d+)\s*$", value)
            if m:
                return int(m.group(1))
            if value.strip():
                return 1
        return 0

    @property
    def excludes(self) -> list[str]:
        value = self.get("--exclude")
        return [v for v in re.split(r"[,\s]+", value) if v] if value else []

    def as_dict(self) -> dict:
        return {
            "partition": self.partition,
            "account": self.account,
            "job_name": self.job_name,
            "cpus": self.cpus,
            "nodes": self.nodes,
            "mem_bytes": self.mem_bytes,
            "time_seconds": self.time_seconds,
            "gpus": self.gpus,
            "excludes": self.excludes,
            "directive_count": len(self.directives),
        }


def parse_script(text: str) -> SbatchRequest:
    """Read the `#SBATCH` block. Stops at the first non-comment command line."""
    request = SbatchRequest(text=text)
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        if not stripped.startswith("#"):
            break  # Slurm ignores #SBATCH after the first real command; so do we
        m = re.match(r"#\s*SBATCH\s+(.*)$", stripped, re.IGNORECASE)
        if not m:
            continue
        for token in _split_directive(m.group(1)):
            name, _, value = token.partition("=")
            if not value and " " in token:
                name, _, value = token.partition(" ")
            name = name.strip()
            name = _FLAG_ALIASES.get(name, name)
            request.directives.append((lineno, name, value.strip() or None))
    return request


def _split_directive(body: str) -> list[str]:
    """`--mem=16G --time=01:00:00  # note` -> the two flags."""
    body = re.split(r"\s+#", body, maxsplit=1)[0]
    tokens: list[str] = []
    for chunk in body.split():
        if chunk.startswith("-"):
            tokens.append(chunk)
        elif tokens and "=" not in tokens[-1] and " " not in tokens[-1]:
            tokens[-1] = f"{tokens[-1]} {chunk}"  # `-c 8` spelling
    return tokens


# ------------------------------------------------------------------ predicates


def insufficient_sample(n: int) -> str | None:
    if n <= 0:
        return (
            "no past runs of this workload and no live telemetry. Submit it once "
            "first, then fit it. A number with n=0 behind it is invention."
        )
    return None


def below_observed_peak(axis: str, recommended: float | None, observed: float | None,
                        unit: str = "") -> str | None:
    if recommended is None or observed is None:
        return None
    if recommended < observed:
        return (
            f"{axis}: refusing to recommend {recommended:g}{unit}, because a run already "
            f"peaked at {observed:g}{unit}. A fit never goes below an observed peak."
        )
    return None


def exceeds_partition_limit(request: SbatchRequest, facts) -> list[str]:
    """Anything the scheduler would reject outright, or accept and never schedule."""
    reasons: list[str] = []
    if facts is None or not getattr(facts, "exists", False):
        return reasons
    name = facts.name

    over_time = (facts.max_time_seconds and request.time_seconds
                 and request.time_seconds > facts.max_time_seconds)
    if over_time:
        reasons.append(
            f"--time={request.get('--time')} exceeds partition {name} MaxTime "
            f"({fmt_slurm_time(facts.max_time_seconds)})"
        )
    if facts.max_nodes and request.nodes and request.nodes > facts.max_nodes:
        reasons.append(f"--nodes={request.nodes} exceeds partition {name} "
                       f"MaxNodes ({facts.max_nodes})")

    cpu_ceiling = facts.max_cpus_per_node or facts.node_cpus_max
    if cpu_ceiling and request.cpus and (request.nodes or 1) == 1 and request.cpus > cpu_ceiling:
        reasons.append(
            f"{request.cpus} cores on one node exceeds the largest node in {name} "
            f"({cpu_ceiling} cores); it would sit PENDING forever"
        )

    mem_ceiling = facts.max_mem_per_node_bytes or facts.node_mem_max_bytes
    if (mem_ceiling and request.mem_bytes and (request.nodes or 1) == 1
            and request.mem_bytes > mem_ceiling):
        reasons.append(
            f"--mem={request.get('--mem') or request.mem_bytes} exceeds the largest node in "
            f"{name} ({fmt_gib(mem_ceiling)}); it would sit PENDING forever"
        )

    if request.gpus and facts.gpu_nodes_known and not facts.gpu_nodes:
        reasons.append(f"{request.gpus} GPU(s) requested but partition {name} has no node "
                       "with a GRES")

    if request.account and facts.allowed_accounts and request.account not in facts.allowed_accounts:
        reasons.append(
            f"--account={request.account} is not in {name} AllowAccounts "
            f"({','.join(facts.allowed_accounts)})"
        )
    return reasons


def missing_account(request: SbatchRequest, site=None) -> str | None:
    """Refuse only where the cluster has actually said this script cannot run.

    Most clusters fill a missing `--account` from the user's default
    association, so a blanket refusal here rejects scripts `sbatch` would have
    taken. It is a real failure only when the lookup succeeded and came back
    empty: then Slurm answers 'Account is not specified', which names no cause.
    """
    if request.account:
        return None
    if site is None or not getattr(site, "account_lookup_ok", False):
        return None
    if getattr(site, "user_default_account", None):
        return None
    suggestion = getattr(site, "suggested_account", None)
    hint = (f"add `#SBATCH --account={suggestion}`" if suggestion
            else "add `#SBATCH --account=<account>`: "
                 "`sacctmgr -nP show assoc user=$USER format=account` lists yours")
    return (
        "no --account, and your Slurm user has no default association, so this "
        "fails with 'Account is not specified', which says nothing about the "
        f"cause; {hint}."
    )


def partition_unusable(request: SbatchRequest, facts) -> str | None:
    if facts is None or not getattr(facts, "queried", True):
        return None  # scontrol was unreachable; that is not evidence against the partition
    if not facts.exists:
        return f"--partition={facts.name} does not exist on this cluster"
    if facts.state and facts.state.upper() not in {"UP", "DRAIN"}:
        return f"partition {facts.name} is State={facts.state}; a job submitted there will not run"
    return None


def unguarded_gpu_nodes(request: SbatchRequest, facts) -> str | None:
    """A job with no --gres must not land on a GPU node."""
    if request.gpus or facts is None or not facts.gpu_nodes_known:
        return None
    unguarded = sorted(set(facts.gpu_nodes) - set(request.excludes))
    if not unguarded:
        return None
    return (
        f"no --gres, but partition {facts.name} has {len(unguarded)} node(s) with a GRES "
        f"({', '.join(unguarded[:3])}{'…' if len(unguarded) > 3 else ''}). CPU work on a GPU "
        "node squats a card and runs no faster: generate --exclude at submit time."
    )


def exclusion_ineffective(excluded: list[str], facts) -> str | None:
    """`--exclude` with an empty list silently does nothing."""
    if excluded:
        return None
    if facts is not None and facts.gpu_nodes_known and facts.gpu_nodes:
        return (
            f"--exclude was generated empty while {facts.name} has "
            f"{len(facts.gpu_nodes)} GRES node(s); an empty --exclude is a no-op"
        )
    return None


def landed_on_gpu_node(request: SbatchRequest, placement: dict) -> str | None:
    if request.gpus:
        return None
    on_gpu = placement.get("on_gpu_node") or []
    if on_gpu:
        return (
            f"job {placement.get('job_id')} asked for no GPU but landed on {', '.join(on_gpu)} "
            f"(Gres={placement['gres'][on_gpu[0]]}). scancel and resubmit; do not let it ride."
        )
    return None


def launch_without_max_run_duration(tokens: list[str]) -> str | None:
    """A VM must not be able to outlive the work it was created for."""
    joined = " ".join(tokens)
    if "compute" not in tokens or "instances" not in tokens or "create" not in tokens:
        return None
    if any(t.startswith("--max-run-duration") for t in tokens):
        return None
    if "--instance-termination-action" in joined:
        return None
    return (
        "gcloud compute instances create without --max-run-duration: the VM bills until someone "
        "remembers it. Add --max-run-duration=<walltime> (plus "
        "--instance-termination-action=DELETE) so it cannot outlive the job."
    )


def script_argument(tokens: list[str]) -> str | None:
    """The batch script in an `sbatch ...` command line, ignoring its flags."""
    seen_sbatch = False
    for token in tokens:
        if not seen_sbatch:
            seen_sbatch = token.endswith("sbatch")
            continue
        if token.startswith("-"):
            continue
        if token.endswith((".sh", ".sbatch", ".slurm", ".batch")):
            return token
    return None


def policy_warnings(request: SbatchRequest, facts=None, site=None) -> list[str]:
    """Local defaults, and only the actionable ones.

    Every warning here surfaces as a permission prompt in the hook, so "this
    partition bills" does not belong: it is true of the default and asking about
    it on every submission is pure friction.
    """
    out: list[str] = []
    named = getattr(site, "default_partition", None) if site else None
    discouraged = list(getattr(site, "discouraged_partitions", []) or []) if site else []
    if not request.partition:
        out.append(f"no --partition; {named} is the default here" if named
                   else "no --partition; the job lands on whatever this cluster defaults to")
    elif request.partition in discouraged:
        out.append(
            f"--partition={request.partition} is discouraged by local policy "
            f"({ENV_DISCOURAGED_PARTITIONS})"
            + (f"; {named} is the default here" if named else "")
        )
    if not request.account and facts is not None and getattr(facts, "allowed_accounts", None):
        out.append(
            f"no --account, and partition {facts.name} only allows "
            f"{','.join(facts.allowed_accounts)}; your default association may not be one"
        )
    if not request.has("--time"):
        out.append("no --time; the job inherits the partition default and can squat until MaxTime")
    if not (request.has("--mem") or request.has("--mem-per-cpu")):
        out.append("no --mem; DefMemPerCPU applies and is usually far below what the job needs")
    if request.gpus and not request.has("--cpus-per-task"):
        out.append("GPU job with no --cpus-per-task; one core often starves the data loader")
    return out


# --------------------------------------------------------------------- ceilings


def partition_ceilings(facts) -> dict[str, float | None]:
    """The numbers `decide` must clamp to, so it cannot emit an invalid request."""
    if facts is None or not getattr(facts, "exists", False):
        return {"cores": None, "mem_bytes": None, "time_seconds": None}
    return {
        "cores": facts.max_cpus_per_node or facts.node_cpus_max,
        "mem_bytes": facts.max_mem_per_node_bytes or facts.node_mem_max_bytes,
        "time_seconds": facts.max_time_seconds,
    }


def clamp(value: float, ceiling: float | None) -> tuple[float, bool]:
    if ceiling is None or value <= ceiling:
        return value, False
    return ceiling, True


# ----------------------------------------------------------------------- check


@dataclass
class CheckResult:
    ok: bool
    partition: str | None
    request: dict
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partition_facts: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def check_script(text: str, facts=None, site=None) -> CheckResult:
    """The pre-submit lint. Pure: partition and site facts come in as data."""
    request = parse_script(text)
    refusals = [r for r in (missing_account(request, site), partition_unusable(request, facts),
                            unguarded_gpu_nodes(request, facts)) if r]
    refusals += exceeds_partition_limit(request, facts)
    if not request.directives:
        refusals.append("no #SBATCH directives found. Is this a batch script?")
    warnings = policy_warnings(request, facts, site)
    if facts is not None and not getattr(facts, "queried", True):
        warnings.append(
            f"could not reach scontrol, so partition {facts.name}'s limits were not checked"
        )
    return CheckResult(
        ok=not refusals,
        partition=request.partition,
        request=request.as_dict(),
        refusals=refusals,
        warnings=warnings,
        partition_facts=facts.as_dict() if facts is not None else None,
    )


def observed_peaks(observations: list[Observation]) -> dict[str, float | None]:
    """The floors every recommendation is held above."""

    def top(attr: str) -> float | None:
        vals = [getattr(o, attr) for o in observations if getattr(o, attr) is not None]
        return max(vals) if vals else None

    mem = [o.mem_peak_trusted_bytes for o in observations if o.mem_peak_trusted_bytes is not None]
    return {
        "cores": top("cores_used"),
        "mem_bytes": max(mem) if mem else None,
        "elapsed_seconds": top("elapsed_seconds"),
    }


def mem_request_floor_gib(nbytes: float) -> int:
    return max(1, math.ceil(nbytes / GIB))
