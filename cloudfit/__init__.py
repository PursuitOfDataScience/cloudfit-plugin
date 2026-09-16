"""cloudfit — what a job should have asked for, from what it actually used.

This module holds only pure data types and unit helpers, so `decide` and `guard`
can import them without pulling in anything that shells out.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

__version__ = "0.1.0"

GIB = 1024**3

CORE_HEADROOM = 1.3
MEM_HEADROOM = 1.4
WALLTIME_HEADROOM = 1.25
HBM_TARGET_PERCENT = 90.0
HBM_RAISE_BELOW = 80.0
HBM_TRIM_ABOVE = 92.0
GPU_COMPUTE_FLOOR_PERCENT = 50.0
AGREEMENT_PERCENT = 10.0
RECOMMENDED_N = 4


@dataclass(frozen=True)
class Confidence:
    """A confidence that carries its own justification, so it cannot be quoted bare."""

    level: str  # none | low | medium | high
    n: int
    spread_percent: float | None = None
    statement: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Observation:
    """One run of a workload, normalised to the four axes plus walltime."""

    source: str  # slurmwatch | sacct | slurmpast | record
    kind: str = "sample"  # sample (live) | final (ended) | rollup (many runs)
    job_id: str | None = None
    workload: str | None = None
    partition: str | None = None
    state: str | None = None
    cores_allocated: int | None = None
    cores_used: float | None = None
    cores_basis: str = "peak"  # peak (cgroup sample) | average (sacct CPU-seconds / elapsed)
    mem_limit_bytes: int | None = None
    mem_peak_bytes: int | None = None
    mem_peak_working_set_bytes: int | None = None
    mem_cache_bytes: int | None = None
    mem_cache_measured: bool = False
    mem_peak_is_lifetime: bool = False  # cgroup memory.peak is a kernel high-watermark
    mem_peak_source: str | None = None
    gpu_count: int = 0
    gpu_hbm_percent: float | None = None
    gpu_util_percent: float | None = None
    gpu_memory_total_bytes: int | None = None
    gpu_model: str | None = None
    elapsed_seconds: float | None = None
    timelimit_seconds: float | None = None
    node_count: int | None = None
    runs: int = 1  # >1 only for a rollup

    @property
    def mem_peak_basis(self) -> str:
        """Which figure `mem_peak_trusted_bytes` came from, so a reason can say it."""
        if self.mem_peak_trusted_bytes is None:
            return "none"
        if self.mem_peak_trusted_bytes == self.mem_peak_working_set_bytes:
            return "working set"
        return "watermark"

    @property
    def mem_peak_trusted_bytes(self) -> int | None:
        """The OOM-relevant peak -- and never below a figure that is a real peak.

        `memory.peak` / MaxRSS count reclaimable page cache, which Arrow-backed
        datasets inflate badly, so the anonymous working set is the tighter
        number -- but only where it is itself a peak over the run.

        A live `slurmwatch --once` snapshot has no history behind it: its
        `peak_working_set_bytes` is that instant's anonymous set. A job that
        frees one phase's arrays before the next reads far below its own high
        mark, so sizing `--mem` to it would OOM the next run. Where the cgroup
        watermark is a lifetime figure and the working set is an instant, the
        watermark is the floor.
        """
        ws = self.mem_peak_working_set_bytes
        if ws is None or not self.mem_cache_measured:
            return self.mem_peak_bytes
        if self.mem_disagrees:
            return ws  # the measured cache accounts for the gap, so subtract it
        if self.kind == "sample" and self.mem_peak_is_lifetime:
            return self.mem_peak_bytes
        return ws

    @property
    def mem_disagrees(self) -> bool:
        """True only where the measured cache actually accounts for the gap.

        The old test -- any gap wider than 25% -- fired on every phased job,
        then blamed page cache for what was really an earlier phase's freed
        arrays. Cache has to carry at least half the gap to be named for it.
        """
        ws = self.mem_peak_working_set_bytes
        peak = self.mem_peak_bytes
        if not self.mem_cache_measured or ws is None or not peak:
            return False
        gap = peak - 1.25 * max(ws, 1)
        if gap <= 0:
            return False
        cache = self.mem_cache_bytes
        return cache is not None and cache >= 0.5 * (peak - max(ws, 1))

    @property
    def mem_peak_understates(self) -> bool:
        """A live working set far under the watermark: phases, not page cache."""
        ws = self.mem_peak_working_set_bytes
        peak = self.mem_peak_bytes
        if ws is None or not peak or self.mem_disagrees:
            return False
        return self.mem_peak_basis == "watermark" and peak > 1.25 * max(ws, 1)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["mem_peak_trusted_bytes"] = self.mem_peak_trusted_bytes
        d["mem_peak_basis"] = self.mem_peak_basis
        d["mem_disagrees"] = self.mem_disagrees
        d["mem_peak_understates"] = self.mem_peak_understates
        return d


@dataclass(frozen=True)
class Directive:
    """One axis, corrected — or explicitly not corrected, and why."""

    axis: str
    direction: str  # down | up | hold | flag | unknown
    reason: str
    confidence: Confidence
    flag: str | None = None
    current: str | None = None
    recommended: str | None = None
    observed: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["confidence"] = self.confidence.as_dict()
        return d


@dataclass
class FitResult:
    workload: str | None
    source: str
    n: int
    confidence: Confidence
    directives: list[Directive] = field(default_factory=list)
    sbatch_block: str = ""
    warnings: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    def as_dict(self) -> dict:
        return {
            "workload": self.workload,
            "source": self.source,
            "n": self.n,
            "confidence": self.confidence.as_dict(),
            "directives": [d.as_dict() for d in self.directives],
            "sbatch_block": self.sbatch_block,
            "warnings": list(self.warnings),
            "refusals": list(self.refusals),
            "notes": list(self.notes),
            "refused": self.refused,
        }


# ---------------------------------------------------------------- unit helpers

_MEM_SUFFIX = {"K": 1024, "M": 1024**2, "G": GIB, "T": 1024**4}


def parse_slurm_mem(text: str | None) -> int | None:
    """`150Gn`, `16G`, `1750M`, `3690348K` -> bytes. Trailing n/c scope is dropped."""
    if not text:
        return None
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT])?[nc]?\s*", str(text), re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "M").upper()  # Slurm's bare default is MB
    return int(value * _MEM_SUFFIX[unit])


def fmt_gib(nbytes: float | None, digits: int = 1) -> str:
    if nbytes is None:
        return "unknown"
    return f"{nbytes / GIB:.{digits}f} GiB"


def gib_request(nbytes: float) -> int:
    """Bytes -> a whole-GiB `--mem=<n>G`, never rounding down below the input."""
    return max(1, math.ceil(nbytes / GIB))


def parse_slurm_time(text: str | None) -> float | None:
    """`3-04:05:06`, `04:05:06`, `05:06`, `60` (minutes) -> seconds."""
    if text is None:
        return None
    text = str(text).strip()
    if not text or text.upper() in {"UNLIMITED", "INVALID", "PARTITION_LIMIT", "NOT_SET"}:
        return None
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        seconds = nums[0] * 60  # bare Slurm time is minutes
    elif len(nums) == 2:
        seconds = nums[0] * 60 + nums[1]
    elif len(nums) == 3:
        seconds = nums[0] * 3600 + nums[1] * 60 + nums[2]
    else:
        return None
    return days * 86400 + seconds


def fmt_slurm_time(seconds: float) -> str:
    """Seconds -> `D-HH:MM:SS` / `HH:MM:SS`, rounded up to a whole minute."""
    total = max(60, int(math.ceil(seconds / 60.0) * 60))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    stem = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}-{stem}" if days else stem


def pct(part: float, whole: float) -> float:
    return 0.0 if not whole else round(100.0 * part / whole, 1)


def spread_percent(values: list[float]) -> float | None:
    """Relative range of a sample, as a percentage of its max. None below n=2."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    hi = max(vals)
    if hi <= 0:
        return 0.0
    return round(100.0 * (hi - min(vals)) / hi, 1)
