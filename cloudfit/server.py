"""The MCP server. Thin: every tool composes probe / collect / history / decide / guard."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script by the plugin manifest entry
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from cloudfit import collect as _collect  # noqa: E402
from cloudfit import decide as _decide  # noqa: E402
from cloudfit import guard as _guard  # noqa: E402
from cloudfit import history as _history  # noqa: E402
from cloudfit import probe as _probe  # noqa: E402

mcp = FastMCP("cloudfit")


def _read_script(script_path: str | None, script: str | None) -> tuple[str, str | None]:
    if script:
        return script, script_path
    if not script_path:
        raise ValueError("pass either script_path or script")
    return Path(script_path).read_text(encoding="utf-8"), script_path


def _site_for(runner=None, overrides: dict | None = None) -> _collect.SiteFacts:
    return _collect.site_facts(runner, overrides)


def _facts_for(request: _guard.SbatchRequest, runner=None,
               site: _collect.SiteFacts | None = None) -> _collect.PartitionFacts | None:
    """Partition limits for whatever partition this script will actually use.

    With no `--partition` and no cluster default to fall back on there is no
    partition to check against. Returning None says "unchecked" rather than
    inventing a name and reporting it absent.
    """
    name = request.partition or (site or _site_for(runner)).default_partition
    if not name:
        return None
    return _collect.partition_facts(name, runner)


@mcp.tool()
def site(default_partition: str | None = None, account: str | None = None,
         discouraged_partitions: list[str] | None = None, save: bool = False) -> dict:
    """What this cluster calls things, and how to correct cloudfit when it guesses wrong.

    Called with no arguments it only reports: the default partition `sinfo`
    marks with `*`, every partition, and the accounts this user is associated
    with. cloudfit ships no partition or account names of its own, so this is
    the whole of what it knows about where it is running.

    Pass any argument to adapt it on the spot; add `save=True` to write the
    profile so later sessions and the submit hook start from it too.
    """
    overrides = {
        "default_partition": default_partition,
        "suggested_account": account,
        "discouraged_partitions": discouraged_partitions,
    }
    overrides = {k: v for k, v in overrides.items() if v}
    facts = _site_for(overrides=overrides)
    out = facts.as_dict()
    out["profile_path"] = str(_collect.site_profile_path())
    out["saved"] = False
    if save and overrides:
        _collect.save_site_profile(overrides)
        out["saved"] = True
    elif save:
        out["notes"] = ["nothing to save: pass a value to pin"]
    return out


@mcp.tool()
def capabilities() -> dict:
    """Which telemetry sources exist on this machine, and how to fix the gaps."""
    return _probe.capabilities().as_dict()


@mcp.tool()
def measure(job_id: str, record: bool = True) -> dict:
    """The four axes for a running job, now, from slurmwatch.

    Records the snapshot so history exists even where Slurm accounting is off.
    """
    measurement = _collect.measure(job_id)
    payload = measurement.as_dict()
    if record and measurement.observation is not None:
        payload["recorded_to"] = str(_history.record(measurement.observation))
    return payload


@mcp.tool()
def history(workload: str | None = None, script_path: str | None = None,
            since: str = _history.DEFAULT_SINCE) -> dict:
    """Past runs of a workload: slurmpast, else sacct, else cloudfit's own record."""
    partition = None
    if not workload:
        if not script_path:
            raise ValueError("pass either workload or script_path")
        text = Path(script_path).read_text(encoding="utf-8")
        workload = _history.workload_from_script(text, script_path)
        partition = _guard.parse_script(text).partition
    if not workload:
        raise ValueError("could not determine a workload name; add #SBATCH --job-name")
    return _history.history(workload, since=since, partition=partition).as_dict()


@mcp.tool()
def fit(job_id: str | None = None, script_path: str | None = None,
        script: str | None = None, since: str = _history.DEFAULT_SINCE) -> dict:
    """What the job should have asked for: a corrected #SBATCH block, with confidence.

    Give a job id to fit from live telemetry, a script to fit from its past runs,
    or both to use both.
    """
    observations, notes, sources = [], [], []
    request = None
    facts = None

    site = _site_for()
    if script or script_path:
        text, path = _read_script(script_path, script)
        request = _guard.parse_script(text)
        facts = _facts_for(request, site=site)
        workload = _history.workload_from_script(text, path)
    else:
        workload = None

    live = None
    if job_id:
        measurement = _collect.measure(job_id)
        notes += measurement.warnings
        live = measurement.observation
        if live is not None:
            observations.append(live)
            sources.append("slurmwatch")
            workload = workload or live.workload
            if facts is None and live.partition:
                facts = _collect.partition_facts(live.partition)

    if workload:
        # One workload on a GPU partition and on a CPU one is two jobs to size, so
        # read the runs from where this one will run.
        past = _history.history(workload, since=since,
                                partition=facts.name if facts is not None else None)
        if past.observations:
            observations += past.observations
            sources.append(past.source)
        notes += past.notes
        notes += [f"tried: {t}" for t in past.tried]

    if not observations and not job_id and not workload:
        raise ValueError("pass a job_id, a script_path or a script")

    # Recorded after the lookup, so the snapshot just taken is not read back as a
    # second run, and a job sampled again is still the one run it was.
    if live is not None:
        _history.record(live)
    observations = _decide.one_per_run(observations)

    result = _decide.fit(observations, request=request,
                         ceilings=_guard.partition_ceilings(facts),
                         source="+".join(sources) or "none", workload=workload, notes=notes,
                         account=site.suggested_account)
    return result.as_dict()


@mcp.tool()
def check(script_path: str | None = None, script: str | None = None) -> dict:
    """Pre-submit lint against the live partition: refuses what the scheduler would."""
    text, _ = _read_script(script_path, script)
    request = _guard.parse_script(text)
    site = _site_for()
    return _guard.check_script(text, _facts_for(request, site=site), site).as_dict()


@mcp.tool()
def submit(script_path: str, dry_run: bool = False) -> dict:
    """Submit a checked script, generating --exclude for GPU nodes and verifying it took."""
    text = Path(script_path).read_text(encoding="utf-8")
    request = _guard.parse_script(text)
    site = _site_for()
    facts = _facts_for(request, site=site)
    return submit_with(text, script_path, request, facts, dry_run=dry_run, site=site)


def submit_with(text: str, script_path: str, request: _guard.SbatchRequest,
                facts: _collect.PartitionFacts | None, *, dry_run: bool = False,
                runner=None, site: _collect.SiteFacts | None = None) -> dict:
    """The submit path, with the backend injectable so it can run against a fake."""
    out = _collect.Submission(submitted=False)

    # The exclusion is generated first: `check` refuses an unguarded CPU job, and
    # the whole point is to hand it the argv that makes it safe.
    excluded: list[str] = []
    if not request.gpus and facts is not None and facts.gpu_nodes:
        excluded = sorted(set(facts.gpu_nodes) - set(request.excludes))
    argv_tail = [f"--exclude={','.join(excluded)}"] if excluded else []
    argv_tail.append(script_path)
    out.excluded = excluded

    checked = _guard.check_script(text, facts, site)
    unresolved = [r for r in checked.refusals if "--exclude" not in r]
    if unresolved:
        out.refusals = unresolved
        return out.as_dict() | {"check": checked.as_dict()}
    out.warnings = list(checked.warnings)
    ineffective = _guard.exclusion_ineffective(excluded, facts) if not request.gpus else None
    if ineffective:
        out.warnings.append(ineffective)

    out.argv = _collect.sbatch_argv(argv_tail)
    if dry_run:
        out.warnings.append("dry_run: nothing was submitted")
        return out.as_dict() | {"check": checked.as_dict()}

    job_id, result = _collect.sbatch(argv_tail, runner)
    if not job_id:
        out.refusals.append(f"sbatch failed (rc={result.returncode}) {result.stderr.strip()[:200]}")
        return out.as_dict() | {"check": checked.as_dict()}
    out.submitted = True
    out.job_id = job_id
    out.stdout = result.stdout.strip()

    placement = _collect.placement(job_id, runner)
    landed = _guard.landed_on_gpu_node(request, placement)
    out.exclusion_verified = (
        "verified" if placement.get("verified") else placement.get("note", "not yet placed")
    )
    if landed:
        out.refusals.append(landed)
        out.warnings.append(f"cancel it with: scancel {job_id}")
    return out.as_dict() | {"check": checked.as_dict(), "placement": placement}


@mcp.tool()
def doctor(project: str | None = None) -> dict:
    """Read-only GCP readiness: what is configured, what is missing, what would fix it."""
    return _probe.doctor(project=project).as_dict()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
