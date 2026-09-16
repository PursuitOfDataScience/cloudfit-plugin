"""Past runs of a workload, from whichever source this machine actually has.

`slurmpast` -> `sacct` -> cloudfit's own JSON record, in that order, always
saying which one answered and with what `n`. The own record is the reason this
works on a cluster whose admin never configured accounting, on a fresh cloud VM,
or on a laptop.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import GIB, Observation, parse_slurm_mem, parse_slurm_time
from .collect import Runner, default_runner

DEFAULT_SINCE = "now-30days"
RECORD_NAME = "history.jsonl"


@dataclass
class HistoryResult:
    workload: str | None
    source: str  # slurmpast | sacct | record | none
    n: int
    observations: list[Observation] = field(default_factory=list)
    advice: list[dict] = field(default_factory=list)  # slurmpast's own, passed through
    tried: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "workload": self.workload,
            "source": self.source,
            "n": self.n,
            "observations": [o.as_dict() for o in self.observations],
            "advice": list(self.advice),
            "tried": list(self.tried),
            "notes": list(self.notes),
        }


def workload_key(name: str | None) -> str:
    """slurmpast collapses digit runs when it groups runs into a workload.

    `caai-p56a` and `caai-p139-verify` both live under `caai-p#…`, so a lookup has
    to normalise the same way or it silently finds nothing.
    """
    return re.sub(r"\d+", "#", name or "")


def workload_from_script(text: str, path: str | None = None) -> str | None:
    from .guard import parse_script

    name = parse_script(text).job_name
    if name:
        return name
    return Path(path).stem if path else None


def record_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Where the own record lives. Never inside the user's Claude config."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("CLOUDFIT_HOME")
    return Path(env) if env else Path.cwd() / ".cloudfit"


def record(observation: Observation, *, directory: str | os.PathLike | None = None) -> Path:
    """Append one observation, so history exists even with accounting switched off."""
    target = record_dir(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / RECORD_NAME
    row = observation.as_dict()
    row["recorded_at"] = time.time()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def read_record(workload: str | None, *, directory: str | os.PathLike | None = None,
                ) -> list[Observation]:
    path = record_dir(directory) / RECORD_NAME
    if not path.exists():
        return []
    key = workload_key(workload)
    fields = set(Observation.__dataclass_fields__)
    out: list[Observation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if workload and workload_key(row.get("workload")) != key:
            continue
        if "mem_peak_is_lifetime" not in row and row.get("mem_peak_source") == "cgroup":
            # Written before 0.1.2 recorded the flag. cgroup `memory.peak` is a
            # kernel high-watermark by definition, so a row sourced from it gets
            # the safe reading rather than being sized to a sampled instant.
            row["mem_peak_is_lifetime"] = True
        out.append(Observation(**{k: v for k, v in row.items() if k in fields}))
    return out


# ------------------------------------------------------------------ slurmpast

_MEM_PEAK = re.compile(r"([\d.]+)\s*(KiB|MiB|GiB|TiB)\s+peak(?:\s+across\s+(\d+)\s+runs)?", re.I)
_MEM_OOM = re.compile(r"the largest at\s+([\d.]+)\s*(KiB|MiB|GiB|TiB)", re.I)
_CORES = re.compile(r"([\d.]+)\s+cores busy per task at peak(?:\s+across\s+(\d+)\s+runs)?", re.I)
_LONGEST = re.compile(r"((?:\d+-)?[\d:]+)\s+longest", re.I)
_UNIT = {"KIB": 1024, "MIB": 1024**2, "GIB": GIB, "TIB": 1024**4}


def _observations_from_slurmpast(entry: dict) -> tuple[list[Observation], list[str]]:
    """One rollup observation per axis, each carrying that axis's own run count.

    slurmpast counts `runs` per workload but samples each axis separately — 100
    runs can carry only 13 usable memory readings — so a single `n` would overstate
    the memory sample.
    """
    name = entry.get("name")
    partition = entry.get("partition")
    runs = int(entry.get("runs") or 0)
    out: list[Observation] = []
    notes: list[str] = []

    def base(**kw) -> Observation:
        return Observation(source="slurmpast", kind="rollup", workload=name,
                           partition=partition, state="COMPLETED", **kw)

    for advice in entry.get("advice") or []:
        flag, observed = advice.get("flag"), advice.get("observed") or ""
        if flag == "--mem":
            m = _MEM_PEAK.search(observed) or _MEM_OOM.search(observed)
            if m:
                nbytes = int(float(m.group(1)) * _UNIT[m.group(2).upper()])
                axis_n = int(m.group(3)) if m.lastindex == 3 and m.group(3) else runs
                out.append(base(runs=max(1, axis_n), mem_peak_bytes=nbytes,
                                mem_limit_bytes=parse_slurm_mem(_requested_mem(advice)),
                                mem_peak_source="slurmpast rollup"))
                if _MEM_OOM.search(observed):
                    notes.append(f"{name}: slurmpast reports an OOM kill — the peak is a floor")
            else:
                notes.append(f"{name}: no usable memory reading in slurmpast ({observed!r})")
        elif flag == "--cpus-per-task":
            m = _CORES.search(observed)
            if m:
                axis_n = int(m.group(2)) if m.group(2) else runs
                out.append(base(runs=max(1, axis_n), cores_used=float(m.group(1)),
                                cores_allocated=_requested_int(advice)))
            else:
                notes.append(f"{name}: no CPU accounting in slurmpast ({observed!r})")
        elif flag == "--time":
            m = _LONGEST.search(observed)
            if m:
                seconds = parse_slurm_time(m.group(1))
                if seconds:
                    out.append(base(runs=max(1, runs), elapsed_seconds=seconds,
                                    timelimit_seconds=parse_slurm_time(advice.get("requested"))))
            else:
                notes.append(f"{name}: no completed run to time ({observed!r})")
    return out, notes


def _requested_mem(advice: dict) -> str | None:
    text = advice.get("requested") or ""
    m = re.fullmatch(r"\s*([\d.]+)\s*(KiB|MiB|GiB|TiB)\s*", text, re.I)
    return f"{m.group(1)}{m.group(2)[0].upper()}" if m else text or None


def _requested_int(advice: dict) -> int | None:
    text = (advice.get("requested") or "").strip()
    return int(text) if text.isdigit() else None


def from_slurmpast(workload: str, runner: Runner, *, since: str = DEFAULT_SINCE,
                   partition: str | None = None, user: str | None = None) -> HistoryResult | str:
    """Returns a HistoryResult, or a string saying why this source could not answer.

    `-u` is passed explicitly rather than relying on the default: an account with
    AdminLevel=Operator can see the whole cluster, and a workload name shared with
    another user would otherwise size your job from their telemetry.
    """
    result = runner(["slurmpast", "--sizing", "--json", "-S", since, "-n", "0",
                     "-u", user or getpass.getuser()], timeout=300.0)
    if result.missing:
        return "slurmpast is not installed"
    doc = result.json()
    if not isinstance(doc, dict) or "workloads" not in doc:
        detail = (result.stderr or result.stdout).strip()[:160]
        return f"slurmpast returned no workload rollup (rc={result.returncode}) {detail}".strip()

    key = workload_key(workload)
    matches = [w for w in doc["workloads"] if workload_key(w.get("name")) == key]
    if partition:
        narrowed = [w for w in matches if w.get("partition") == partition]
        matches = narrowed or matches
    if not matches:
        return f"slurmpast has no workload matching {workload!r} since {since}"

    entry = max(matches, key=lambda w: int(w.get("runs") or 0))
    observations, notes = _observations_from_slurmpast(entry)
    if not observations:
        return (f"slurmpast knows {entry['runs']} run(s) of {workload!r} but none carry a usable "
                "resource reading")
    return HistoryResult(
        workload=entry.get("name"), source="slurmpast",
        n=int(entry.get("runs") or 0), observations=observations,
        advice=entry.get("advice") or [], notes=notes,
    )


# ---------------------------------------------------------------------- sacct

_SACCT_FORMAT = ("JobID,JobName,Partition,State,AllocCPUS,ReqMem,MaxRSS,TotalCPU,Elapsed,"
                 "Timelimit,AllocTRES")


def _gpus_from_tres(tres: str | None) -> int:
    if not tres:
        return 0
    m = re.search(r"gres/gpu=(\d+)", tres)
    return int(m.group(1)) if m else 0


def observations_from_sacct(text: str) -> list[Observation]:
    """Parse `sacct -P` rows, folding step rows into their job.

    `-X` alone is not enough: MaxRSS lives only on the `.batch`/`.extern` steps,
    so the job row shows every other field and a blank memory peak.
    """
    jobs: dict[str, dict] = {}
    steps: dict[str, int] = {}
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 11:
            continue
        (job_id, name, partition, state, cpus, req_mem, max_rss,
         total_cpu, elapsed, timelimit, tres) = parts[:11]
        base = job_id.split(".")[0]
        if "." in job_id:
            rss = parse_slurm_mem(max_rss)
            if rss:
                steps[base] = max(steps.get(base, 0), rss)
            continue
        jobs[base] = {
            "name": name, "partition": partition, "state": state.split()[0] if state else None,
            "cpus": int(cpus) if cpus.isdigit() else None,
            "req_mem": parse_slurm_mem(req_mem), "max_rss": parse_slurm_mem(max_rss),
            "total_cpu": parse_slurm_time(total_cpu) or _parse_total_cpu(total_cpu),
            "elapsed": parse_slurm_time(elapsed), "timelimit": parse_slurm_time(timelimit),
            "gpus": _gpus_from_tres(tres),
        }

    out: list[Observation] = []
    for base, row in jobs.items():
        peak = row["max_rss"] or steps.get(base)
        elapsed = row["elapsed"] or 0
        cores_used = (row["total_cpu"] / elapsed) if (row["total_cpu"] and elapsed) else None
        out.append(Observation(
            source="sacct", kind="final", job_id=base, workload=row["name"] or None,
            partition=row["partition"] or None, state=row["state"],
            cores_allocated=row["cpus"], cores_used=cores_used, cores_basis="average",
            mem_limit_bytes=row["req_mem"], mem_peak_bytes=peak,
            mem_peak_working_set_bytes=None, mem_cache_measured=False,
            mem_peak_source="sacct MaxRSS" if peak else None,
            gpu_count=row["gpus"], elapsed_seconds=row["elapsed"] or None,
            timelimit_seconds=row["timelimit"],
        ))
    return out


def _parse_total_cpu(text: str) -> float | None:
    """sacct's TotalCPU can carry sub-second precision: `00:38.821`, `01:02:03.4`."""
    if not text or "." not in text:
        return None
    head, _, frac = text.rpartition(".")
    seconds = parse_slurm_time(head)
    try:
        return (seconds or 0) + float(f"0.{frac}")
    except ValueError:
        return seconds


def from_sacct(workload: str, runner: Runner, *, since: str = DEFAULT_SINCE,
               user: str | None = None) -> HistoryResult | str:
    argv = ["sacct", "-P", "-n", "--format", _SACCT_FORMAT, "--name", workload,
            "-S", since, "-u", user or getpass.getuser()]
    result = runner(argv, timeout=180.0)
    if result.missing:
        return "sacct is not installed"
    if not result.ok:
        return f"sacct failed (rc={result.returncode}) {(result.stderr or '').strip()[:160]}"
    if not result.stdout.strip():
        return f"sacct has no run named {workload!r} since {since}"

    observations = observations_from_sacct(result.stdout)
    usable = [o for o in observations
              if o.mem_peak_bytes or o.cores_used is not None or o.elapsed_seconds]
    if not usable:
        return (f"sacct lists {len(observations)} run(s) of {workload!r} with every resource field "
                "empty — JobAcctGatherType is off")
    notes = []
    if not any(o.mem_peak_bytes for o in usable):
        notes.append("sacct returned no MaxRSS on any run or step: memory accounting is not "
                     "gathered")
    notes.append("sacct reports CPU-seconds, so cores_used is a run average and understates bursts")
    return HistoryResult(workload=workload, source="sacct", n=len(usable),
                         observations=usable, notes=notes)


# ------------------------------------------------------------------ the chain


def history(workload: str, *, runner: Runner | None = None, since: str = DEFAULT_SINCE,
            partition: str | None = None, directory: str | os.PathLike | None = None,
            user: str | None = None) -> HistoryResult:
    runner = runner or default_runner()
    tried: list[str] = []

    for attempt in (
        lambda: from_slurmpast(workload, runner, since=since, partition=partition, user=user),
        lambda: from_sacct(workload, runner, since=since, user=user),
    ):
        outcome = attempt()
        if isinstance(outcome, HistoryResult):
            outcome.tried = tried
            return outcome
        tried.append(outcome)

    own = read_record(workload, directory=directory)
    if own:
        return HistoryResult(
            workload=workload, source="record", n=len(own), observations=own, tried=tried,
            notes=[f"cloudfit's own record at {record_dir(directory) / RECORD_NAME} "
                   f"answered with n={len(own)}"],
        )
    tried.append(f"cloudfit's own record at {record_dir(directory) / RECORD_NAME} is empty")
    return HistoryResult(workload=workload, source="none", n=0, tried=tried)
