"""Argument parsing, and that every subcommand is reachable."""

import datetime as dt

import pytest

from shachen_ops.cli import build_parser, parse_duration, parse_time


@pytest.mark.parametrize(
    "text, expected",
    [
        ("90m", dt.timedelta(minutes=90)),
        ("2h", dt.timedelta(hours=2)),
        ("1d", dt.timedelta(days=1)),
    ],
)
def test_durations(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["2", "h", "2 hours", "-1h", ""])
def test_bad_durations_are_refused(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_times_are_utc_with_or_without_a_zone():
    naive = parse_time("2026-08-31T13:16:18")
    zoned = parse_time("2026-08-31T13:16:18Z")
    assert naive == zoned == dt.datetime(2026, 8, 31, 13, 16, 18, tzinfo=dt.UTC)


def test_backfill_defaults():
    args = build_parser().parse_args(["backfill", "--region", "swus"])
    assert (args.since, args.limit, args.every, args.force) == ("2h", 3, None, False)


def test_run_takes_a_scan_time():
    args = build_parser().parse_args(["run", "--region", "swus", "--time", "2026-08-31T13:00Z"])
    assert args.time == "2026-08-31T13:00Z"


def test_a_region_is_always_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])
