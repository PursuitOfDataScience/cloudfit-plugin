from __future__ import annotations

from conftest import load_json

from cloudfit.collect import FakeRunner
from cloudfit.probe import capabilities, doctor

ON = """Configuration data
AccountingStorageType  = accounting_storage/slurmdbd
JobAcctGatherType      = jobacct_gather/linux
JobAcctGatherFrequency = 30
ClusterName            = cluster0
"""
STORAGE_OFF = ON.replace("accounting_storage/slurmdbd", "accounting_storage/none")
GATHER_OFF = ON.replace("jobacct_gather/linux", "jobacct_gather/none")


def have_everything(name):
    return f"/usr/bin/{name}"


def test_capabilities_reads_both_accounting_switches():
    caps = capabilities(FakeRunner().on("show", "config", stdout=ON), which=have_everything)
    assert caps.accounting["storage_configured"]
    assert caps.accounting["gather_configured"]
    assert caps.cluster == "cluster0"
    assert caps.remedy is None
    assert {s["source"] for s in caps.history_sources} == {"slurmpast", "sacct", "record"}
    assert all(s["available"] for s in caps.history_sources)


def test_the_two_switches_fail_independently():
    caps = capabilities(FakeRunner().on("show", "config", stdout=GATHER_OFF), which=have_everything)
    assert caps.accounting["storage_configured"]
    assert not caps.accounting["gather_configured"]
    assert "JobAcctGatherType" in caps.remedy


def test_storage_off_disables_both_slurm_sources_but_not_the_record():
    caps = capabilities(FakeRunner().on("show", "config", stdout=STORAGE_OFF), which=have_everything)
    sources = {s["source"]: s["available"] for s in caps.history_sources}
    assert sources == {"slurmpast": False, "sacct": False, "record": True}


def test_the_remedy_depends_on_whether_the_user_can_act(monkeypatch):
    runner = FakeRunner().on("show", "config", stdout=STORAGE_OFF)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    unprivileged = capabilities(runner, which=have_everything)
    assert "ask RCC" in unprivileged.remedy
    assert "apt install" not in unprivileged.remedy

    monkeypatch.setattr("os.geteuid", lambda: 0)
    root = capabilities(runner, which=have_everything)
    assert "apt install slurmdbd mariadb-server" in root.remedy
    assert "chmod 600" in root.remedy


def test_a_machine_with_no_slurm_says_so():
    caps = capabilities(FakeRunner(), which=lambda name: None)
    assert caps.binaries["sacct"] is None
    assert "not a Slurm client" in caps.remedy


def test_slurmwatch_needs_none_of_that_infrastructure():
    caps = capabilities(FakeRunner().on("show", "config", stdout=STORAGE_OFF), which=have_everything)
    assert any("side effect of the job running" in n for n in caps.notes)


# ------------------------------------------------------------------- the cloud


def test_doctor_reads_the_recorded_project_without_calling_gcloud():
    replay = load_json("gcloud_doctor_real.json")
    report = doctor(FakeRunner(), project="example-project-9f2c", responses=replay)
    assert report.commands == []  # nothing was run
    by_key = {f.key: f for f in report.findings}
    assert by_key["compute.googleapis.com"].status == "ok"
    assert by_key["quota:CPUS_ALL_REGIONS"].detail == "0/12 used"
    assert by_key["quota:GPUS_ALL_REGIONS"].status == "missing"
    assert "until a request is approved" in by_key["quota:GPUS_ALL_REGIONS"].detail


def test_doctor_reports_the_open_ssh_rule_as_attention():
    report = doctor(FakeRunner(), project="p", responses=load_json("gcloud_doctor_real.json"))
    firewall = next(f for f in report.findings if f.key == "firewall")
    assert firewall.status == "attention"
    assert "default-allow-ssh" in firewall.detail
    assert "IAP" in firewall.detail


def test_doctor_reports_a_narrowed_default_service_account_as_ok():
    report = doctor(FakeRunner(), project="p", responses=load_json("gcloud_doctor_real.json"))
    sa = next(f for f in report.findings if f.key == "default-service-account")
    assert sa.status == "ok"
    assert "batch.agentReporter" in sa.detail


def test_doctor_flags_an_editor_default_service_account():
    replay = load_json("gcloud_doctor_real.json")
    account = replay["project_info_describe"]["defaultServiceAccount"]
    replay["iam_policy"]["bindings"].append(
        {"role": "roles/editor", "members": [f"serviceAccount:{account}"]})
    report = doctor(FakeRunner(), project="p", responses=replay)
    sa = next(f for f in report.findings if f.key == "default-service-account")
    assert sa.status == "attention"
    assert "roles/editor" in sa.detail
    assert sa.fix_argv[:2] == ["gcloud", "projects"]


def test_every_gap_carries_the_argv_that_would_fix_it():
    replay = load_json("gcloud_doctor_real.json")
    replay["services_list"] = [s for s in replay["services_list"]
                               if s["config"]["name"] != "storage.googleapis.com"]
    report = doctor(FakeRunner(), project="p", responses=replay)
    storage = next(f for f in report.findings if f.key == "storage.googleapis.com")
    assert storage.status == "attention"
    assert storage.fix_argv == ["gcloud", "services", "enable", "storage.googleapis.com",
                                "--project", "p"]
    assert all(f.fix_argv is None or f.fix_argv[0] == "gcloud" for f in report.findings)


def test_doctor_is_read_only_by_construction():
    replay = load_json("gcloud_doctor_real.json")
    runner = FakeRunner()
    doctor(runner, project="p", responses=replay)
    assert runner.calls == []


def test_doctor_refuses_without_a_project():
    runner = FakeRunner().on("gcloud", "config", stdout="\n")
    report = doctor(runner)
    assert report.findings[0].status == "missing"
    assert not report.reachable


# ------------------------------------------------------- reading it vs having it

OPERATOR = "    alice  Operator  pi-example \n"
PLAIN = "    someone  None  pi-smith \n"


def perms(config=ON, level=PLAIN, coord="    someone \n"):
    runner = (
        FakeRunner()
        .on("sacctmgr", "withcoord", stdout=coord)
        .on("sacctmgr", "format=User,AdminLevel", stdout=level)
        .on("show", "config", stdout=config)
    )
    return capabilities(runner, which=have_everything).permissions


def test_an_ordinary_user_can_read_their_own_history_and_no_one_elses():
    p = perms(config=ON.replace("ClusterName", "PrivateData             = jobs,users\nClusterName"))
    assert p["admin_level"] == "None"
    assert p["own_history_readable"]
    assert not p["cross_user_history_readable"]
    assert "your own history and no one else's" in p["remedy"]
    assert "Only a Slurm admin can raise it" in p["remedy"]


def test_an_operator_is_told_that_queries_are_still_scoped_to_them():
    p = perms(level=OPERATOR)
    assert p["admin_level"] == "Operator"
    assert p["cross_user_history_readable"]
    assert "scopes every query to you with -u" in p["remedy"]


def test_a_coordinator_counts_as_elevated():
    p = perms(coord="    someone  pi-smith \n")
    assert p["coordinator_of"] == ["pi-smith"]
    assert p["cross_user_history_readable"]


def test_permission_is_not_probed_when_there_is_nothing_to_read():
    p = perms(config=STORAGE_OFF)
    assert not p["own_history_readable"]
    assert p["remedy"] is None  # the accounting remedy already covers it
