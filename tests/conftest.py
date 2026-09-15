from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT))


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def cpu_overask_real():
    """A live midway3 job: 6 cores idle, 49 GiB asked, 175 MiB actually resident."""
    return load_json("slurmwatch_cpu_overask_real.json")


@pytest.fixture
def gpu_hbm40():
    return load_json("slurmwatch_gpu_hbm40_synthetic.json")


@pytest.fixture
def gpu_hbm91():
    return load_json("slurmwatch_gpu_hbm91_synthetic.json")


@pytest.fixture
def gpu_starved():
    return load_json("slurmwatch_gpu_starved_synthetic.json")


@pytest.fixture
def arrow_pagecache():
    return load_json("slurmwatch_arrow_pagecache_synthetic.json")


@pytest.fixture
def gpu_pair_uneven():
    return load_json("slurmwatch_gpu_pair_uneven_synthetic.json")


@pytest.fixture
def amd_facts():
    """PartitionFacts for amd, built from the recorded scontrol/sinfo output."""
    from cloudfit.collect import FakeRunner, partition_facts

    runner = (
        FakeRunner()
        .on("scontrol", "show", "partition", "amd",
            stdout=load_text("scontrol_partition_amd_real.txt"))
        .on("sinfo", "-p", "amd", "%n %c %m", stdout=load_text("sinfo_amd_sizes_real.txt"))
        .on("sinfo", "-p", "amd", "%N %G", stdout=load_text("sinfo_amd_nodes_real.txt"))
    )
    return partition_facts("amd", runner)


@pytest.fixture
def gpu_facts():
    from cloudfit.collect import FakeRunner, partition_facts

    runner = (
        FakeRunner()
        .on("scontrol", "show", "partition", "gpu",
            stdout=load_text("scontrol_partition_gpu_real.txt"))
        .on("sinfo", "-p", "gpu", "%n %c %m", stdout=load_text("sinfo_gpu_sizes_real.txt"))
        .on("sinfo", "-p", "gpu", "%N %G", stdout=load_text("sinfo_gpu_nodes_real.txt"))
    )
    return partition_facts("gpu", runner)


@pytest.fixture
def record_home(tmp_path, monkeypatch):
    """Point the own record at a tmp dir, never at the user's config."""
    monkeypatch.setenv("CLOUDFIT_HOME", str(tmp_path / "record"))
    return tmp_path / "record"
