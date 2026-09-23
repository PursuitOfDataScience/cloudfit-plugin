"""What this machine can actually tell you, and what the cloud project is missing.

`capabilities()` probes the Slurm side, `doctor()` the GCP side. Both report and
neither acts: every gap carries the exact argv that would fix it, so the
write-side tools can execute `fix_argv` through the same `Runner` without this
module changing shape.
"""

from __future__ import annotations

import getpass
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from . import collect as _collect
from .collect import Runner

SLURM_BINARIES = ("slurmwatch", "slurmpast", "sacct", "sacctmgr", "sinfo", "sbatch",
                  "squeue", "scontrol", "srun")
CLOUD_BINARIES = ("gcloud",)
LOCAL_BINARIES = ("nvidia-smi",)

REQUIRED_SERVICES = ("compute.googleapis.com",)
RECOMMENDED_SERVICES = ("batch.googleapis.com", "storage.googleapis.com",
                        "logging.googleapis.com", "monitoring.googleapis.com",
                        "billingbudgets.googleapis.com")
# Only the project-wide quotas `project-info describe` actually carries; the
# per-region ones (PREEMPTIBLE_CPUS, INSTANCES) are not in that response.
WATCHED_QUOTAS = ("CPUS_ALL_REGIONS", "GPUS_ALL_REGIONS", "IN_USE_ADDRESSES",
                  "STATIC_ADDRESSES")


@dataclass
class Finding:
    """One fact, and the command that would change it. `doctor` never runs the fix."""

    key: str
    status: str  # ok | missing | attention | unknown
    detail: str
    fix_argv: list[str] | None = None


@dataclass
class Capabilities:
    binaries: dict[str, str | None] = field(default_factory=dict)
    accounting: dict[str, object] = field(default_factory=dict)
    permissions: dict[str, object] = field(default_factory=dict)
    history_sources: list[dict] = field(default_factory=list)
    remedy: str | None = None
    privileged: bool = False
    cluster: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _config_pairs(text: str) -> dict[str, str]:
    """`sacctmgr/scontrol show config` prints `Key  = Value` lines."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and " " not in key:
            pairs[key] = value
    return pairs


def capabilities(runner: Runner | None = None,
                 which: Callable[[str], str | None] = shutil.which) -> Capabilities:
    """Which telemetry sources exist here, and whether the user can fix the gaps."""
    runner = runner or _collect.default_runner()
    caps = Capabilities(privileged=os.geteuid() == 0)
    for name in (*SLURM_BINARIES, *CLOUD_BINARIES, *LOCAL_BINARIES):
        caps.binaries[name] = which(name)

    pairs: dict[str, str] = {}
    if caps.binaries.get("sacctmgr"):
        shown = runner(["sacctmgr", "show", "config"], timeout=60.0)
        if shown.ok:
            pairs.update(_config_pairs(shown.stdout))
    if caps.binaries.get("scontrol"):
        shown = runner(["scontrol", "show", "config"], timeout=60.0)
        if shown.ok:
            pairs.update(_config_pairs(shown.stdout))

    storage = pairs.get("AccountingStorageType")
    gather = pairs.get("JobAcctGatherType")
    caps.cluster = pairs.get("ClusterName")
    storage_on = bool(storage) and storage not in {"accounting_storage/none", "(null)"}
    gather_on = bool(gather) and gather not in {"jobacct_gather/none", "(null)"}
    caps.accounting = {
        "AccountingStorageType": storage,
        "JobAcctGatherType": gather,
        "JobAcctGatherFrequency": pairs.get("JobAcctGatherFrequency"),
        "storage_configured": storage_on,
        "gather_configured": gather_on,
        "queried": bool(pairs),
    }

    # The two switches fail independently: sacct can list every job and still
    # report every resource field empty when only the gather side is off.
    if not pairs:
        caps.remedy = "no Slurm config could be read; this machine is not a Slurm client"
    elif storage_on and gather_on:
        caps.remedy = None
    else:
        off = [n for n, on in (("AccountingStorageType", storage_on),
                               ("JobAcctGatherType", gather_on)) if not on]
        if caps.privileged:
            caps.remedy = (
                f"accounting off ({', '.join(off)}). Fix: `apt install slurmdbd mariadb-server`, "
                "set AccountingStorageType=accounting_storage/slurmdbd and "
                "JobAcctGatherType=jobacct_gather/cgroup in slurm.conf, `chmod 600 slurmdbd.conf`, "
                "then `scontrol reconfigure`"
            )
        else:
            caps.remedy = (
                f"accounting off ({', '.join(off)}); using cloudfit's own record instead. "
                "Changing this needs a Slurm admin; ask yours about "
                f"{' and '.join(off)}"
            )

    caps.permissions = _accounting_permissions(runner, caps, pairs, storage_on)

    caps.history_sources = [
        {"source": "slurmpast", "available": bool(caps.binaries.get("slurmpast")) and storage_on,
         "why": "needs slurmdbd, a database behind it, and JobAcctGatherType"},
        {"source": "sacct", "available": bool(caps.binaries.get("sacct")) and storage_on,
         "why": "lists jobs whenever slurmdbd is up, but every resource field is empty "
                "without JobAcctGatherType"},
        {"source": "record", "available": True,
         "why": "cloudfit's own JSON record: always available, which is the point"},
    ]
    if caps.binaries.get("slurmwatch"):
        caps.notes.append(
            "slurmwatch needs none of that infrastructure: it reads cgroups, /proc, nvidia-smi "
            "and scontrol, which exist as a side effect of the job running"
        )
    if not caps.binaries.get("nvidia-smi"):
        caps.notes.append("no nvidia-smi here, so GPU axes only populate on a compute node")
    return caps


ELEVATED = {"OPERATOR", "ADMINISTRATOR", "ADMIN"}


def _accounting_permissions(runner: Runner, caps: Capabilities, pairs: dict[str, str],
                            storage_on: bool) -> dict[str, object]:
    """Whether the caller may *read* accounting, which is separate from whether it exists.

    Configured-and-unreadable is a real state: any user sees their own jobs, but
    another user's history needs AdminLevel=Operator or coordinator rights on the
    account, and `PrivateData` can withdraw even that. Only a Slurm admin grants it.
    """
    user = getpass.getuser()
    out: dict[str, object] = {"user": user, "admin_level": None, "coordinator_of": [],
                              "private_data": pairs.get("PrivateData")}
    if not caps.binaries.get("sacctmgr") or not storage_on:
        out["own_history_readable"] = False
        out["cross_user_history_readable"] = False
        out["remedy"] = None if not storage_on else "sacctmgr is not on PATH"
        return out

    shown = runner(["sacctmgr", "-n", "show", "user", user, "format=User,AdminLevel"], timeout=60.0)
    if shown.ok and shown.stdout.split():
        parts = shown.stdout.split()
        out["admin_level"] = parts[1] if len(parts) > 1 else None

    coord = runner(["sacctmgr", "-n", "show", "user", user, "withcoord", "format=User,Coord"],
                   timeout=60.0)
    if coord.ok:
        out["coordinator_of"] = [a for a in coord.stdout.split()[1:] if a]

    level = (out["admin_level"] or "").upper()
    restricted = "jobs" in (out["private_data"] or "").lower()
    elevated = level in ELEVATED or bool(out["coordinator_of"])
    out["own_history_readable"] = True
    out["cross_user_history_readable"] = elevated or not restricted

    if elevated:
        out["remedy"] = (
            f"AdminLevel={out['admin_level'] or 'coordinator'}: this account can read other "
            "users' accounting. cloudfit still scopes every query to you with -u, so a workload "
            "name shared with another user cannot leak into your fit"
        )
    elif restricted:
        out["remedy"] = (
            f"AdminLevel={out['admin_level'] or 'None'} and PrivateData={out['private_data']}: "
            "you can read your own history and no one else's. That is all a fit needs. Only a "
            "Slurm admin can raise it (`sacctmgr modify user ... set adminlevel=Operator`)"
        )
    else:
        out["remedy"] = None
    return out


# ----------------------------------------------------------------- the cloud


@dataclass
class Doctor:
    project: str | None
    reachable: bool = False
    findings: list[Finding] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def gaps(self) -> list[Finding]:
        return [f for f in self.findings if f.status in {"missing", "attention"}]

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "reachable": self.reachable,
            "findings": [asdict(f) for f in self.findings],
            "gaps": [f.key for f in self.gaps],
            "commands": list(self.commands),
            "notes": list(self.notes),
        }


def _project(runner: Runner, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = os.environ.get("CLOUDFIT_GCP_PROJECT")
    if env:
        return env
    got = runner(["gcloud", "config", "get-value", "project"], timeout=60.0)
    value = got.stdout.strip() if got.ok else ""
    return value or None


def doctor(runner: Runner | None = None, *, project: str | None = None,
           responses: dict | None = None) -> Doctor:
    """Read-only GCP readiness. Makes no changes.

    `responses` replays a recorded capture (the `gcloud_doctor_real.json` fixture)
    instead of calling gcloud, which is how this is tested without touching the
    live project.
    """
    runner = runner or _collect.default_runner()
    if responses is None:
        project = _project(runner, project)
    out = Doctor(project=project)
    if responses is None and not project:
        out.findings.append(Finding("gcloud", "missing",
                                    "no gcloud project configured",
                                    ["gcloud", "config", "set", "project", "PROJECT_ID"]))
        return out

    def fetch(key: str, argv: list[str]) -> object | None:
        if responses is not None:
            return responses.get(key)
        out.commands.append(" ".join(argv))
        result = runner(argv, timeout=240.0)
        if not result.ok:
            out.notes.append(f"{' '.join(argv[:3])} failed: {(result.stderr or '').strip()[:140]}")
            return None
        return result.json()

    scope = ["--project", str(project), "--format", "json"]
    services = fetch("services_list", ["gcloud", "services", "list", "--enabled", *scope])
    info = fetch("project_info_describe", ["gcloud", "compute", "project-info", "describe", *scope])
    rules = fetch("firewall_rules_list", ["gcloud", "compute", "firewall-rules", "list", *scope])
    quota_info = fetch("quotas_info_list",
                       ["gcloud", "quotas", "info", "list",
                        "--service=compute.googleapis.com", *scope])
    iam = fetch("iam_policy", ["gcloud", "projects", "get-iam-policy", str(project),
                               "--format", "json"])
    out.reachable = any(x is not None for x in (services, info, rules))

    enabled = {s.get("config", {}).get("name") for s in (services or []) if isinstance(s, dict)}
    for name in REQUIRED_SERVICES + RECOMMENDED_SERVICES:
        required = name in REQUIRED_SERVICES
        if name in enabled:
            out.findings.append(Finding(name, "ok", "enabled"))
        elif services is None:
            out.findings.append(Finding(name, "unknown", "service list unavailable"))
        else:
            out.findings.append(Finding(
                name, "missing" if required else "attention",
                "not enabled" + ("" if required else " (only needed for staging/logging)"),
                ["gcloud", "services", "enable", name, "--project", str(project)]))

    eligible = {
        q.get("quotaId", "").replace("-", "_").removesuffix("_per_project"): q
        for q in (quota_info or []) if isinstance(q, dict)
    }
    quotas = {q["metric"]: q for q in (info or {}).get("quotas", []) if isinstance(q, dict)}
    for metric in WATCHED_QUOTAS:
        row = quotas.get(metric)
        if row is None:
            out.findings.append(Finding(f"quota:{metric}", "unknown",
                                        "not reported by project-info"))
            continue
        limit, usage = row.get("limit"), row.get("usage")
        can_raise = eligible.get(metric, {}).get("quotaIncreaseEligibility", {}).get("isEligible")
        if limit in (0, 0.0):
            detail = (f"limit 0, so nothing can be launched against {metric} until a request "
                      "is approved")
            out.findings.append(Finding(
                f"quota:{metric}", "missing", detail,
                ["gcloud", "alpha", "quotas", "preferences", "create",
                 f"--quota-id={metric.replace('_', '-')}-per-project",
                 "--service=compute.googleapis.com", "--preferred-value=N",
                 "--project", str(project)] if can_raise else None))
        else:
            out.findings.append(Finding(f"quota:{metric}", "ok",
                                        f"{usage:g}/{limit:g} used"))

    open_tcp = [r for r in (rules or [])
                if isinstance(r, dict) and "0.0.0.0/0" in (r.get("sourceRanges") or [])
                and any(a.get("IPProtocol") == "tcp" for a in r.get("allowed") or [])]
    if open_tcp:
        names = sorted(r.get("name", "?") for r in open_tcp)
        # Deleting the default RDP rule is the one fix that is always safe on a Linux
        # project. Deleting SSH without an IAP rule in its place locks everyone out,
        # so that one is advice, and a rule that is not open gets no fix at all.
        out.findings.append(Finding(
            "firewall", "attention",
            f"{', '.join(names)} allow tcp from 0.0.0.0/0; consider IAP-only access instead",
            ["gcloud", "compute", "firewall-rules", "delete", "default-allow-rdp",
             "--project", str(project)] if "default-allow-rdp" in names else None))
    elif rules is not None:
        out.findings.append(Finding("firewall", "ok", "no tcp rule open to 0.0.0.0/0"))

    default_sa = (info or {}).get("defaultServiceAccount")
    if default_sa:
        roles = sorted(
            b.get("role", "")
            for b in (iam or {}).get("bindings", [])
            if isinstance(b, dict) and f"serviceAccount:{default_sa}" in (b.get("members") or [])
        )
        if "roles/editor" in roles:
            out.findings.append(Finding(
                "default-service-account", "attention",
                f"{default_sa} holds roles/editor on the whole project, and enabling the "
                "compute API creates this silently. Narrow it before launching anything",
                ["gcloud", "projects", "remove-iam-policy-binding", str(project),
                 f"--member=serviceAccount:{default_sa}", "--role=roles/editor"]))
        elif roles:
            out.findings.append(Finding("default-service-account", "ok",
                                        f"{default_sa}: "
                                        + ", ".join(r.split("/")[-1] for r in roles)))
        else:
            out.findings.append(Finding("default-service-account", "unknown",
                                        f"{default_sa}: no project-level binding found"))

    out.notes.append("doctor is read-only; every gap above carries the argv that would fix it")
    return out
