"""Publishing one scan, and the manifests that make the bucket browsable.

The object store is behind a tiny interface with two implementations — R2 over
boto3, and a plain directory — so a local run exercises exactly the code path
the scheduled job takes, minus the network.

The layout is::

    shachen-ops/v1/{region}/{YYYY}/{MM}/{DD}/{HHMMSS}/debra.png
                                                      dustrgb.png
                                                      debra.nc
                                                      meta.json
    shachen-ops/v1/{region}/latest.json          pointer + recent scans, short cache
    shachen-ops/v1/{region}/index/{YYYY}/{MM}.json   month listing, for backfill dedupe

Write order is what makes a rerun safe: scan objects first, then ``meta.json``
(which is what marks the scan published), then the month index, and
``latest.json`` last. A reader following ``latest.json`` therefore never
arrives at a half-written scan, and a job that dies mid-publish leaves a scan
that the next backfill simply redoes.
"""

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from shachen_ops.config import LAYOUT_VERSION, ROOT_PREFIX, Region
from shachen_ops.scans import Scan

#: Imagery never changes, so it can be cached forever; the pointer is
#: refetched every minute and the month index every five. ``meta.json`` is the
#: one scan object that is not immutable: retention removes a scan's field file
#: partway through its life, and the manifest has to stop advertising it.
IMMUTABLE = "public, max-age=31536000, immutable"
META_CACHE = "public, max-age=3600"
POINTER_CACHE = "public, max-age=60"
INDEX_CACHE = "public, max-age=300"

CONTENT_TYPES = {".png": "image/png", ".nc": "application/x-netcdf", ".json": "application/json"}

#: How many scans ``latest.json`` carries beyond the pointer itself.
RECENT_SCANS = 12


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(Path(path).suffix, "application/octet-stream")


def region_prefix(region: Region) -> str:
    """Key prefix (no trailing slash) everything published for one region is under."""
    return f"{ROOT_PREFIX}/{LAYOUT_VERSION}/{region.key}"


def scan_prefix(region: Region, scan: Scan) -> str:
    """Key prefix (no trailing slash) every object of one scan lives under."""
    return f"{region_prefix(region)}/{scan.prefix}"


def index_key(region: Region, month: dt.date) -> str:
    return f"{region_prefix(region)}/index/{month:%Y}/{month:%m}.json"


def latest_key(region: Region) -> str:
    return f"{region_prefix(region)}/latest.json"


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------


@dataclass
class FileStore:
    """A directory pretending to be a bucket, for local runs and tests."""

    root: Path

    def _path(self, key: str) -> Path:
        return Path(self.root) / key

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._path(key).read_bytes())
        return dest

    def put_file(self, key: str, path: Path, cache_control: str = IMMUTABLE) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(path).read_bytes())
        return key

    def put_json(self, key: str, payload: dict, cache_control: str = POINTER_CACHE) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return key

    def get_json(self, key: str) -> dict | None:
        target = self._path(key)
        return json.loads(target.read_text()) if target.exists() else None

    def list_objects(self, prefix: str) -> dict[str, int]:
        root = Path(self.root)
        base = root / prefix
        if not base.is_dir():
            return {}
        return {
            str(path.relative_to(root)): path.stat().st_size
            for path in base.rglob("*")
            if path.is_file()
        }

    def delete(self, keys) -> int:
        removed = 0
        root = Path(self.root).resolve()
        for key in keys:
            target = self._path(key)
            if not target.exists():
                continue
            target.unlink()
            removed += 1
            # A directory bucket has real directories; a pruned scan should not
            # leave an empty one behind to be mistaken for a scan with no files.
            parent = target.parent.resolve()
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        return removed


@dataclass
class R2Store:
    """Cloudflare R2 over its S3-compatible endpoint."""

    bucket: str
    client: object

    @classmethod
    def from_env(cls, bucket: str | None = None) -> "R2Store":
        """Build a store from ``R2_*`` environment variables.

        ``R2_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID`` and ``R2_SECRET_ACCESS_KEY``
        are required; the bucket defaults to ``R2_BUCKET`` or ``shachen``. A
        missing variable raises RuntimeError naming it, so a misconfigured
        workflow fails at startup rather than after the compute.
        """
        import boto3

        missing = [
            name
            for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(f"missing R2 credentials in the environment: {', '.join(missing)}")
        account = os.environ["R2_ACCOUNT_ID"]
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        return cls(bucket=bucket or os.environ.get("R2_BUCKET", "shachen"), client=client)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
                return False
            raise
        return True

    def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(dest))
        return dest

    def put_file(self, key: str, path: Path, cache_control: str = IMMUTABLE) -> str:
        self.client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type(path), "CacheControl": cache_control},
        )
        return key

    def put_json(self, key: str, payload: dict, cache_control: str = POINTER_CACHE) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, indent=2, sort_keys=True).encode(),
            ContentType="application/json",
            CacheControl=cache_control,
        )
        return key

    def get_json(self, key: str) -> dict | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(response["Body"].read())

    def list_objects(self, prefix: str) -> dict[str, int]:
        """Every key under ``prefix`` and its size, following pagination.

        Sizes come free with the listing, which is what lets a prune report the
        footprint it left behind without a HEAD per object.
        """
        found: dict[str, int] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                found[item["Key"]] = item["Size"]
        return found

    def delete(self, keys) -> int:
        """Delete keys in batches; returns how many were submitted.

        ``DeleteObjects`` caps at 1000 keys per call and reports per-key
        errors in the response rather than raising, so a failure to delete one
        object is surfaced instead of silently inflating the bucket.
        """
        keys = list(keys)
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                first = errors[0]
                raise RuntimeError(
                    f"{len(errors)} of {len(batch)} deletions failed, first: "
                    f"{first.get('Key')}: {first.get('Code')} {first.get('Message')}"
                )
        return len(keys)


def store_from_env(destination: str | None) -> FileStore | R2Store:
    """``None`` or ``r2`` gives R2; anything else is a directory to write into."""
    if destination in (None, "", "r2"):
        return R2Store.from_env()
    return FileStore(Path(destination))


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def is_published(store, region: Region, scan: Scan) -> bool:
    """Whether ``scan`` already has a complete publish (its ``meta.json``)."""
    return store.exists(f"{scan_prefix(region, scan)}/meta.json")


def published_scans(
    store, region: Region, start: dt.datetime, end: dt.datetime
) -> set[dt.datetime]:
    """Scan start times already published in [start, end], from the month indexes.

    One GET per calendar month covered, rather than one HEAD per candidate
    scan — a two-hour backfill window on a five-minute cadence is 24 scans, and
    at hourly-plus windows the difference matters.
    """
    found: set[dt.datetime] = set()
    month = start.date().replace(day=1)
    last = end.date().replace(day=1)
    while month <= last:
        payload = store.get_json(index_key(region, month)) or {}
        for stamp in payload.get("scans", []):
            when = dt.datetime.fromisoformat(stamp)
            if start <= when <= end:
                found.add(when)
        month = (month + dt.timedelta(days=32)).replace(day=1)
    return found


def iso_stamp(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_metadata(region: Region, scan: Scan, extra: dict) -> dict:
    """The ``meta.json`` body: what this scan is and what produced it."""
    from importlib.metadata import version

    meta = {
        "region": region.key,
        "description": region.description,
        "satellite": region.satellite,
        "sensor": region.sensor,
        "product": region.product,
        "bucket": region.bucket,
        "bbox": list(region.bbox),
        "scan_start": iso_stamp(scan.start),
        "tuning": region.tuning,
        "layout_version": LAYOUT_VERSION,
        "shachen_version": version("shachen"),
        "shachen_ops_version": version("shachen-ops"),
        "published_at": iso_stamp(dt.datetime.now(dt.UTC)),
    }
    meta.update(extra)
    return meta


def _index_entry(region: Region, scan: Scan, meta: dict) -> dict:
    """The compact per-scan record carried by the index and the pointer."""
    return {
        "scan_start": meta["scan_start"],
        "prefix": scan_prefix(region, scan),
        "assets": meta.get("assets", []),
    }


def retention_policy(region: Region) -> dict:
    """What the pointer tells readers about how long this region's data lives.

    A reader that finds a scan in a month index needs to know whether its
    objects are still there before requesting them, and the honest place to say
    so is the pointer rather than each scan's own manifest.
    """
    return {
        "keep_scans": region.keep_scans,
        "keep_field_scans": region.keep_field_scans,
        "publish_every_minutes": region.publish_every_minutes,
        "note": (
            f"the newest {region.keep_scans} scans are kept; only the newest "
            f"{region.keep_field_scans} of those still carry debra.nc"
        ),
    }


def update_index(store, region: Region, scan: Scan, meta: dict) -> str:
    """Add ``scan`` to its month index, keeping the listing sorted and unique."""
    month = scan.start.date().replace(day=1)
    key = index_key(region, month)
    payload = store.get_json(key) or {"region": region.key, "month": f"{month:%Y-%m}", "scans": []}
    stamps = sorted({*payload.get("scans", []), meta["scan_start"]})
    payload["scans"] = stamps
    payload["count"] = len(stamps)
    payload["updated_at"] = iso_stamp(dt.datetime.now(dt.UTC))
    store.put_json(key, payload, cache_control=INDEX_CACHE)
    return key


def update_latest(store, region: Region, scan: Scan, meta: dict) -> str:
    """Point ``latest.json`` at ``scan`` — unless a newer scan is already there.

    A backfill runs newest-first but a catch-up run can still publish an older
    scan after a newer one; the pointer must not walk backwards when it does.
    """
    key = latest_key(region)
    payload = store.get_json(key) or {"region": region.key, "recent": []}
    entry = _index_entry(region, scan, meta)

    recent = [
        item for item in payload.get("recent", []) if item["scan_start"] != entry["scan_start"]
    ]
    recent.append(entry)
    recent.sort(key=lambda item: item["scan_start"], reverse=True)
    payload["recent"] = recent[:RECENT_SCANS]

    current = (payload.get("latest") or {}).get("scan_start")
    if current is None or entry["scan_start"] >= current:
        payload["latest"] = meta
    payload["retention"] = retention_policy(region)
    payload["updated_at"] = iso_stamp(dt.datetime.now(dt.UTC))
    store.put_json(key, payload, cache_control=POINTER_CACHE)
    return key


def publish_scan(
    store, region: Region, scan: Scan, artifacts: dict[str, Path], extra: dict
) -> dict:
    """Upload one scan's artifacts and update both manifests.

    ``artifacts`` maps published filename to local path. Returns the
    ``meta.json`` body that was written.
    """
    prefix = scan_prefix(region, scan)
    for name, path in sorted(artifacts.items()):
        store.put_file(f"{prefix}/{name}", Path(path), cache_control=IMMUTABLE)

    meta = scan_metadata(region, scan, {**extra, "assets": sorted(artifacts)})
    store.put_json(f"{prefix}/meta.json", meta, cache_control=META_CACHE)
    update_index(store, region, scan, meta)
    update_latest(store, region, scan, meta)
    return meta
