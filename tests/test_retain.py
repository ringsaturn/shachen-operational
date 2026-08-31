"""Retention: the two tiers, the manifests they have to keep honest, and the
idempotency the scheduled prune depends on."""

import datetime as dt
from dataclasses import replace

import pytest

from shachen_ops.config import Region
from shachen_ops.fetch import camel_key
from shachen_ops.publish import (
    FileStore,
    index_key,
    latest_key,
    publish_scan,
    published_scans,
    scan_prefix,
)
from shachen_ops.retain import FIELDS, plan_retention, prune_region, scan_objects
from shachen_ops.scans import Scan

REGION = Region(
    key="swus",
    satellite="GOES-19",
    sensor="abi",
    bucket="noaa-goes19",
    product="ABI-L1b-RadC",
    cadence_minutes=5,
    bbox=(-115.0, 25.0, -95.0, 40.0),
    keep_scans=4,
    keep_field_scans=2,
)

#: Rough real proportions: the field file dwarfs the two images.
SIZES = {"debra.png": 1_000_000, "dustrgb.png": 2_900_000, FIELDS: 7_200_000}


def scan_at(index: int) -> Scan:
    """Scan ``index`` half-hours after a fixed epoch; higher index is newer."""
    return Scan(
        dt.datetime(2026, 8, 31, 0, 0, 18, tzinfo=dt.UTC) + dt.timedelta(minutes=30 * index)
    )


def artifacts(tmp_path, scan: Scan, names=tuple(SIZES)) -> dict:
    made = {}
    for name in names:
        path = tmp_path / f"{scan.prefix.replace('/', '-')}-{name}"
        path.write_bytes(b"\0" * SIZES[name])
        made[name] = path
    return made


def publish_n(store, tmp_path, count: int, region: Region = REGION) -> list[Scan]:
    """Publish ``count`` consecutive scans, oldest first, as the pipeline would."""
    published = [scan_at(index) for index in range(count)]
    for scan in published:
        publish_scan(store, region, scan, artifacts(tmp_path, scan), {})
    return published


def assets_at(store, scan: Scan, region: Region = REGION) -> set[str]:
    prefix = scan_prefix(region, scan)
    return {name for name in SIZES if store.exists(f"{prefix}/{name}")}


# --------------------------------------------------------------------------
# The policy itself
# --------------------------------------------------------------------------


def test_the_plan_cuts_the_newest_scans_into_three_tiers():
    stamps = [f"2026-08-31T{hour:02d}:00:00Z" for hour in range(6)]
    plan = plan_retention(stamps, keep_scans=4, keep_field_scans=2)

    assert plan.keep == ("2026-08-31T05:00:00Z", "2026-08-31T04:00:00Z")
    assert plan.strip == ("2026-08-31T03:00:00Z", "2026-08-31T02:00:00Z")
    assert plan.drop == ("2026-08-31T01:00:00Z", "2026-08-31T00:00:00Z")


def test_the_plan_keeps_everything_while_under_budget():
    stamps = [f"2026-08-31T{hour:02d}:00:00Z" for hour in range(3)]
    plan = plan_retention(stamps, keep_scans=144, keep_field_scans=12)

    assert set(plan.keep) == set(stamps)
    assert plan.strip == () and plan.drop == ()


def test_the_listing_is_read_back_as_scans(tmp_path):
    store = FileStore(tmp_path / "bucket")
    publish_n(store, tmp_path, 2)

    found = scan_objects(store, REGION)
    assert set(found) == {"2026-08-31T00:00:18Z", "2026-08-31T00:30:18Z"}
    # The manifests share the region prefix and must never be seen as assets.
    assert set(found["2026-08-31T00:00:18Z"]) == {*SIZES, "meta.json"}


# --------------------------------------------------------------------------
# What a prune does to the bucket
# --------------------------------------------------------------------------


def test_prune_drops_old_scans_and_strips_fields_from_the_middle_tier(tmp_path):
    store = FileStore(tmp_path / "bucket")
    published = publish_n(store, tmp_path, 6)

    report = prune_region(store, REGION)

    # Newest two keep everything, next two lose only the field file.
    assert assets_at(store, published[5]) == set(SIZES)
    assert assets_at(store, published[4]) == set(SIZES)
    assert assets_at(store, published[3]) == {"debra.png", "dustrgb.png"}
    assert assets_at(store, published[2]) == {"debra.png", "dustrgb.png"}
    # Oldest two are gone entirely, manifest included.
    assert assets_at(store, published[1]) == set()
    assert not store.exists(f"{scan_prefix(REGION, published[0])}/meta.json")

    assert report.dropped == ("2026-08-31T00:30:18Z", "2026-08-31T00:00:18Z")
    assert report.kept_scans == 4
    # Two whole scans plus the field file from each of the two stripped ones;
    # the few hundred bytes over that are the dropped scans' manifests.
    assets = 2 * sum(SIZES.values()) + 2 * SIZES[FIELDS]
    assert assets <= report.freed_bytes < assets + 10_000


def test_prune_leaves_the_bucket_inside_its_budget(tmp_path):
    store = FileStore(tmp_path / "bucket")
    publish_n(store, tmp_path, 10)

    report = prune_region(store, REGION)

    on_disk = sum(sum(names.values()) for names in scan_objects(store, REGION).values())
    imagery = sum(SIZES.values()) - SIZES[FIELDS]
    budget = (
        REGION.keep_field_scans * sum(SIZES.values())
        + (REGION.keep_scans - REGION.keep_field_scans) * imagery
    )
    # The budget arithmetic models the assets; a manifest per scan is the rest.
    assert budget <= on_disk < budget + 10_000
    assert report.kept_bytes == pytest.approx(on_disk, rel=0.01)


def test_a_second_prune_changes_nothing(tmp_path):
    """A strip must not be redone every half hour for every scan in the tier."""
    store = FileStore(tmp_path / "bucket")
    publish_n(store, tmp_path, 6)
    prune_region(store, REGION)

    before = scan_objects(store, REGION)
    report = prune_region(store, REGION)

    assert report.dropped == () and report.stripped == ()
    assert report.deleted_keys == 0
    assert scan_objects(store, REGION) == before


def test_prune_is_a_no_op_while_every_tier_has_room(tmp_path):
    store = FileStore(tmp_path / "bucket")
    published = publish_n(store, tmp_path, REGION.keep_field_scans)

    report = prune_region(store, REGION)

    assert report.deleted_keys == 0
    assert all(assets_at(store, scan) == set(SIZES) for scan in published)


def test_the_field_tier_bites_before_the_scan_tier(tmp_path):
    """Three scans is inside ``keep_scans`` but past ``keep_field_scans``."""
    store = FileStore(tmp_path / "bucket")
    published = publish_n(store, tmp_path, 3)

    report = prune_region(store, REGION)

    assert report.dropped == ()
    assert report.stripped == (published[0].stamp,)
    assert assets_at(store, published[0]) == {"debra.png", "dustrgb.png"}
    assert assets_at(store, published[1]) == set(SIZES)


def test_dry_run_reports_without_deleting(tmp_path):
    store = FileStore(tmp_path / "bucket")
    published = publish_n(store, tmp_path, 6)

    report = prune_region(store, REGION, dry_run=True)

    assert len(report.dropped) == 2 and len(report.stripped) == 2
    assert report.freed_bytes > 0
    assert all(assets_at(store, scan) == set(SIZES) for scan in published)


# --------------------------------------------------------------------------
# The manifests have to follow
# --------------------------------------------------------------------------


def test_the_month_index_forgets_dropped_scans(tmp_path):
    store = FileStore(tmp_path / "bucket")
    publish_n(store, tmp_path, 6)
    prune_region(store, REGION)

    index = store.get_json(index_key(REGION, dt.date(2026, 8, 1)))
    assert index["count"] == 4
    assert "2026-08-31T00:00:18Z" not in index["scans"]

    # A backfill must not think a pruned scan is still published.
    window = (scan_at(0).start, scan_at(5).start)
    assert scan_at(0).start not in published_scans(store, REGION, *window)


def test_the_pointer_only_advertises_assets_that_still_exist(tmp_path):
    store = FileStore(tmp_path / "bucket")
    published = publish_n(store, tmp_path, 6)
    prune_region(store, REGION)

    payload = store.get_json(latest_key(REGION))
    stamps = [item["scan_start"] for item in payload["recent"]]
    assert "2026-08-31T00:00:18Z" not in stamps
    assert payload["latest"]["scan_start"] == published[5].stamp

    by_stamp = {item["scan_start"]: item for item in payload["recent"]}
    assert FIELDS in by_stamp[published[5].stamp]["assets"]
    assert FIELDS not in by_stamp[published[2].stamp]["assets"]
    assert payload["retention"]["keep_scans"] == REGION.keep_scans


def test_a_stripped_scan_stops_advertising_its_field_file(tmp_path):
    store = FileStore(tmp_path / "bucket")
    published = publish_n(store, tmp_path, 6)
    prune_region(store, REGION)

    meta = store.get_json(f"{scan_prefix(REGION, published[2])}/meta.json")
    assert meta["assets"] == ["debra.png", "dustrgb.png"]
    assert meta["fields_expired_at"].endswith("Z")

    kept = store.get_json(f"{scan_prefix(REGION, published[5])}/meta.json")
    assert FIELDS in kept["assets"]
    assert "fields_expired_at" not in kept


def test_an_emptied_month_index_is_removed(tmp_path):
    store = FileStore(tmp_path / "bucket")
    august = Scan(dt.datetime(2026, 8, 31, 23, 30, tzinfo=dt.UTC))
    september = [
        Scan(dt.datetime(2026, 9, 1, hour, 30, tzinfo=dt.UTC)) for hour in range(REGION.keep_scans)
    ]
    for scan in [august, *september]:
        publish_scan(store, REGION, scan, artifacts(tmp_path, scan), {})

    prune_region(store, REGION)

    assert not store.exists(index_key(REGION, dt.date(2026, 8, 1)))
    assert store.get_json(index_key(REGION, dt.date(2026, 9, 1)))["count"] == REGION.keep_scans


# --------------------------------------------------------------------------
# Ancillary
# --------------------------------------------------------------------------


def test_prune_keeps_only_the_two_newest_camel_months(tmp_path):
    store = FileStore(tmp_path / "bucket")
    source = tmp_path / "camel.nc"
    source.write_bytes(b"\0" * 2_300_000)
    months = [dt.date(2026, month, 1) for month in (5, 6, 7, 8)]
    for month in months:
        store.put_file(camel_key(REGION, month), source)

    report = prune_region(store, REGION)

    assert not store.exists(camel_key(REGION, dt.date(2026, 5, 1)))
    assert not store.exists(camel_key(REGION, dt.date(2026, 6, 1)))
    # The hot path looks up the scan's month, so a backfill across a month
    # boundary still finds the previous one staged.
    assert store.exists(camel_key(REGION, dt.date(2026, 7, 1)))
    assert store.exists(camel_key(REGION, dt.date(2026, 8, 1)))
    assert len(report.ancillary_deleted) == 2


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_one_region_prune_leaves_another_alone(tmp_path):
    store = FileStore(tmp_path / "bucket")
    other = replace(REGION, key="gobi")
    publish_n(store, tmp_path, 6)
    publish_n(store, tmp_path, 6, region=other)

    prune_region(store, REGION)

    assert len(scan_objects(store, REGION)) == 4
    assert len(scan_objects(store, other)) == 6
