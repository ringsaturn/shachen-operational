"""Publishing, manifests, and the idempotency the scheduled job depends on."""

import datetime as dt
import json

from shachen_ops.config import Region
from shachen_ops.publish import (
    FileStore,
    index_key,
    is_published,
    latest_key,
    publish_scan,
    published_scans,
    scan_prefix,
    update_latest,
)
from shachen_ops.scans import Scan

REGION = Region(
    key="swus",
    satellite="GOES-19",
    sensor="abi",
    bucket="noaa-goes19",
    product="ABI-L1b-RadC",
    cadence_minutes=5,
    bbox=(-115.0, 25.0, -95.0, 40.0),
)


def scan_at(hour, minute=0):
    return Scan(dt.datetime(2026, 8, 31, hour, minute, 18, tzinfo=dt.UTC))


def artifacts(tmp_path, name="a"):
    png = tmp_path / f"{name}.png"
    png.write_bytes(b"not really a png")
    return {"debra.png": png}


def test_publish_writes_the_scan_then_both_manifests(tmp_path):
    store = FileStore(tmp_path / "bucket")
    scan = scan_at(13)
    meta = publish_scan(store, REGION, scan, artifacts(tmp_path), {"background": "semianalytic"})

    prefix = scan_prefix(REGION, scan)
    assert prefix == "v1/swus/2026/08/31/130018"
    assert store.exists(f"{prefix}/debra.png")
    assert store.exists(f"{prefix}/meta.json")
    assert meta["assets"] == ["debra.png"]
    assert meta["background"] == "semianalytic"

    index = store.get_json(index_key(REGION, dt.date(2026, 8, 1)))
    assert index["scans"] == ["2026-08-31T13:00:18Z"]
    assert store.get_json(latest_key(REGION))["latest"]["scan_start"] == "2026-08-31T13:00:18Z"


def test_republishing_a_scan_is_a_no_op_for_the_manifests(tmp_path):
    store = FileStore(tmp_path / "bucket")
    scan = scan_at(13)
    publish_scan(store, REGION, scan, artifacts(tmp_path), {})
    publish_scan(store, REGION, scan, artifacts(tmp_path), {})

    index = store.get_json(index_key(REGION, dt.date(2026, 8, 1)))
    assert index["scans"] == ["2026-08-31T13:00:18Z"]
    assert index["count"] == 1
    assert len(store.get_json(latest_key(REGION))["recent"]) == 1


def test_the_pointer_never_walks_backwards(tmp_path):
    store = FileStore(tmp_path / "bucket")
    newer, older = scan_at(14), scan_at(13)
    publish_scan(store, REGION, newer, artifacts(tmp_path), {})
    publish_scan(store, REGION, older, artifacts(tmp_path), {})

    payload = store.get_json(latest_key(REGION))
    assert payload["latest"]["scan_start"] == "2026-08-31T14:00:18Z"
    assert [item["scan_start"] for item in payload["recent"]] == [
        "2026-08-31T14:00:18Z",
        "2026-08-31T13:00:18Z",
    ]


def test_the_pointer_keeps_only_the_recent_window(tmp_path):
    from shachen_ops.publish import RECENT_SCANS

    store = FileStore(tmp_path / "bucket")
    for minute in range(RECENT_SCANS + 5):
        scan = scan_at(13, minute)
        update_latest(
            store, REGION, scan, {"scan_start": scan.start.strftime("%Y-%m-%dT%H:%M:%SZ")}
        )
    assert len(store.get_json(latest_key(REGION))["recent"]) == RECENT_SCANS


def test_published_scans_reads_the_month_index(tmp_path):
    store = FileStore(tmp_path / "bucket")
    publish_scan(store, REGION, scan_at(13), artifacts(tmp_path), {})

    window = (
        dt.datetime(2026, 8, 31, 12, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 31, 14, tzinfo=dt.UTC),
    )
    assert published_scans(store, REGION, *window) == {scan_at(13).start}
    assert is_published(store, REGION, scan_at(13))
    assert not is_published(store, REGION, scan_at(14))


def test_published_scans_spans_a_month_boundary(tmp_path):
    store = FileStore(tmp_path / "bucket")
    august = Scan(dt.datetime(2026, 8, 31, 23, 30, tzinfo=dt.UTC))
    september = Scan(dt.datetime(2026, 9, 1, 0, 30, tzinfo=dt.UTC))
    for scan in (august, september):
        publish_scan(store, REGION, scan, artifacts(tmp_path), {})

    found = published_scans(store, REGION, august.start, september.start)
    assert found == {august.start, september.start}


def test_the_manifests_are_readable_json(tmp_path):
    store = FileStore(tmp_path / "bucket")
    publish_scan(store, REGION, scan_at(13), artifacts(tmp_path), {})
    text = (tmp_path / "bucket" / latest_key(REGION)).read_text()
    assert json.loads(text)["region"] == "swus"
