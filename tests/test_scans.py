"""Scan discovery against a fake S3 listing — no network anywhere here."""

import datetime as dt

import pytest

from shachen_ops.config import Region
from shachen_ops.scans import (
    AHI_SEGMENTS,
    Scan,
    abi_scan_start,
    ahi_local_name,
    ahi_segments_for_bbox,
    available_scans,
    on_cadence,
    scan_keys,
    select_ahi_keys,
)

ABI = Region(
    key="swus",
    satellite="GOES-19",
    sensor="abi",
    bucket="noaa-goes19",
    product="ABI-L1b-RadC",
    cadence_minutes=5,
    bbox=(-115.0, 25.0, -95.0, 40.0),
)
AHI = Region(
    key="gobi",
    satellite="Himawari-9",
    sensor="ahi",
    bucket="noaa-himawari9",
    product="AHI-L1b-FLDK",
    cadence_minutes=10,
    bbox=(80.0, 30.0, 146.0, 55.0),
)


class FakeFS:
    """The two-method slice of s3fs that scan discovery actually uses."""

    def __init__(self, listings):
        self.listings = listings

    def ls(self, prefix, refresh=False):
        if prefix not in self.listings:
            raise FileNotFoundError(prefix)
        return self.listings[prefix]


def abi_key(channel, when, hour_prefix):
    stamp = when.strftime("%Y%j%H%M%S")
    return f"{hour_prefix}OR_ABI-L1b-RadC-M6{channel}_G19_s{stamp}4_e{stamp}9_c{stamp}9.nc"


def abi_fs(times, channels=ABI.channels):
    prefix = f"{ABI.bucket}/{ABI.product}/2026/243/13/"
    keys = [abi_key(channel, when, prefix) for when in times for channel in channels]
    return FakeFS({prefix: keys}), prefix


def test_abi_scan_start_parses_the_s_field():
    key = abi_key("C13", dt.datetime(2026, 8, 31, 13, 16, 18), "p/")
    assert abi_scan_start(key) == dt.datetime(2026, 8, 31, 13, 16, 18, tzinfo=dt.UTC)


def test_abi_scans_are_the_complete_ones_only():
    complete = dt.datetime(2026, 8, 31, 13, 16, 18)
    partial = dt.datetime(2026, 8, 31, 13, 21, 18)
    prefix = f"{ABI.bucket}/{ABI.product}/2026/243/13/"
    keys = [abi_key(channel, complete, prefix) for channel in ABI.channels]
    keys += [abi_key(channel, partial, prefix) for channel in ABI.channels[:3]]

    found = available_scans(
        ABI,
        dt.datetime(2026, 8, 31, 13, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 31, 14, 0, tzinfo=dt.UTC),
        fs=FakeFS({prefix: keys}),
    )
    assert [scan.start.minute for scan in found] == [16]


def test_abi_scan_keys_are_one_per_channel():
    when = dt.datetime(2026, 8, 31, 13, 16, 18)
    fs, _ = abi_fs([when])
    keys = scan_keys(ABI, Scan(when.replace(tzinfo=dt.UTC)), fs=fs)
    assert len(keys) == len(ABI.channels)
    assert [k for k in keys if "C02" in k]


def test_a_missing_channel_at_fetch_time_raises():
    when = dt.datetime(2026, 8, 31, 13, 16, 18)
    fs, _ = abi_fs([when], channels=ABI.channels[:-1])
    with pytest.raises(FileNotFoundError, match="C15"):
        scan_keys(ABI, Scan(when.replace(tzinfo=dt.UTC)), fs=fs)


def test_scan_prefix_is_the_published_path():
    assert Scan(dt.datetime(2026, 8, 31, 13, 16, 18, tzinfo=dt.UTC)).prefix == "2026/08/31/131618"


def test_window_wider_than_the_maximum_is_refused():
    now = dt.datetime(2026, 8, 31, tzinfo=dt.UTC)
    with pytest.raises(ValueError, match="exceeds"):
        available_scans(ABI, now - dt.timedelta(days=9), now, fs=FakeFS({}))


# --- AHI ------------------------------------------------------------------


def test_ahi_timelines_come_from_the_day_listing():
    prefix = f"{AHI.bucket}/{AHI.product}/2026/08/31/"
    fs = FakeFS({prefix: [prefix + name for name in ("1240", "1250", "1300", "index.html")]})
    found = available_scans(
        AHI,
        dt.datetime(2026, 8, 31, 12, 45, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 31, 13, 5, tzinfo=dt.UTC),
        fs=fs,
    )
    assert [scan.start.strftime("%H%M") for scan in found] == ["1250", "1300"]


def test_a_regional_bbox_needs_only_the_northern_strips():
    assert ahi_segments_for_bbox(AHI.bbox) == (1, 2, 3)
    assert ahi_segments_for_bbox(None) == tuple(range(1, AHI_SEGMENTS + 1))


def test_select_ahi_keys_prefers_the_two_kilometre_repackaging():
    keys = [
        "b/HS_H09_20260831_1300_B13_FLDK_R20_S0110.DAT.bz2",
        "b/HS_H09_20260831_1300_B13_FLDK_R05_S0110.DAT.bz2",
    ]
    assert select_ahi_keys(keys, ("B13",), (1,)) == [keys[0]]


def test_select_ahi_keys_falls_back_to_the_coarsest_native_resolution():
    keys = ["b/HS_H09_20260831_1300_B03_FLDK_R05_S0110.DAT.bz2"]
    assert select_ahi_keys(keys, ("B03",), (1,)) == keys


def test_select_ahi_keys_drops_strips_outside_the_domain():
    keys = [
        "b/HS_H09_20260831_1300_B13_FLDK_R20_S0110.DAT.bz2",
        "b/HS_H09_20260831_1300_B13_FLDK_R20_S0910.DAT.bz2",
    ]
    assert select_ahi_keys(keys, ("B13",), (1, 2, 3)) == [keys[0]]


def test_a_channel_with_no_usable_strip_raises():
    keys = ["b/HS_H09_20260831_1300_B13_FLDK_R20_S0910.DAT.bz2"]
    with pytest.raises(RuntimeError, match="B13"):
        select_ahi_keys(keys, ("B13",), (1,))


def test_only_the_repackaged_b03_is_stored_decompressed():
    assert ahi_local_name("HS_H09_1300_B03_FLDK_R20_S0110.DAT.bz2").endswith(".DAT")
    assert ahi_local_name("HS_H09_1300_B03_FLDK_R05_S0110.DAT.bz2").endswith(".bz2")
    assert ahi_local_name("HS_H09_1300_B13_FLDK_R20_S0110.DAT.bz2").endswith(".bz2")


# --- cadence --------------------------------------------------------------


def scans_every(minutes, count, first=dt.datetime(2026, 8, 31, 11, 1, 18, tzinfo=dt.UTC)):
    return [Scan(first + dt.timedelta(minutes=minutes * n)) for n in range(count)]


def test_on_cadence_keeps_one_scan_per_slot():
    found = scans_every(5, 25)
    window = (found[0].start, found[-1].start)
    picked = on_cadence(found, 30, *window)
    assert [scan.start.strftime("%H:%M") for scan in picked] == ["11:31", "12:01", "12:31"]


def test_on_cadence_ignores_slots_the_window_only_half_covers():
    found = scans_every(5, 8)  # 11:01 .. 11:36, no slot fully inside
    assert on_cadence(found, 30, found[0].start, found[-1].start) == []


def test_on_cadence_is_stable_across_different_windows():
    found = scans_every(5, 25)
    wide = on_cadence(found, 30, found[0].start, found[-1].start)
    narrow = on_cadence(found, 30, found[3].start, found[-1].start)
    assert set(narrow) <= set(wide)


def test_a_zero_cadence_publishes_every_scan():
    found = scans_every(5, 4)
    assert on_cadence(found, 0, found[0].start, found[-1].start) == found
