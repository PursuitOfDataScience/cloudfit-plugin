from __future__ import annotations

import pytest

from cloudfit import (
    GIB,
    fmt_gib,
    fmt_slurm_time,
    gib_request,
    parse_slurm_mem,
    parse_slurm_time,
    spread_percent,
)


@pytest.mark.parametrize(("text", "expected"), [
    ("150Gn", 150 * GIB),
    ("16G", 16 * GIB),
    ("1750M", 1750 * 1024**2),
    ("3690348K", 3690348 * 1024),
    ("48Gc", 48 * GIB),
    ("2048", 2048 * 1024**2),  # a bare Slurm memory value is megabytes
    ("", None),
    (None, None),
    ("UNLIMITED", None),
])
def test_parse_slurm_mem(text, expected):
    assert parse_slurm_mem(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("3-04:05:06", 3 * 86400 + 4 * 3600 + 5 * 60 + 6),
    ("04:05:06", 4 * 3600 + 5 * 60 + 6),
    ("05:06", 5 * 60 + 6),
    ("60", 3600),  # a bare Slurm time is minutes
    ("10-00:00:00", 10 * 86400),
    ("UNLIMITED", None),
    ("", None),
])
def test_parse_slurm_time(text, expected):
    assert parse_slurm_time(text) == expected


def test_fmt_slurm_time_rounds_up_to_a_whole_minute():
    assert fmt_slurm_time(1745) == "00:30:00"
    assert fmt_slurm_time(1) == "00:01:00"
    assert fmt_slurm_time(90061) == "1-01:02:00"  # 61s of remainder rounds up to 2 minutes


def test_gib_request_never_rounds_down():
    assert gib_request(9.1 * GIB) == 10
    assert gib_request(1) == 1
    assert gib_request(16 * GIB) == 16


def test_fmt_gib():
    assert fmt_gib(3 * GIB) == "3.0 GiB"
    assert fmt_gib(None) == "unknown"


def test_spread_percent():
    assert spread_percent([10.0]) is None
    assert spread_percent([]) is None
    assert spread_percent([9.1, 9.7]) == pytest.approx(6.2, abs=0.1)
    assert spread_percent([4.0, 10.0]) == 60.0
