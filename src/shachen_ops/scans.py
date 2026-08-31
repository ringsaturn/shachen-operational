"""Scan discovery on the AWS Open Data buckets.

A scan is one (region, start time) pair, and the start time is the key to
everything downstream: it names the output prefix, it is what the manifest
records, and it is what makes a rerun idempotent. This module answers two
questions against anonymous S3 — *which scans exist in a time window* and
*which keys make up one scan* — and nothing else.

The ABI and AHI layouts differ enough that they get separate paths:

- ABI: ``{bucket}/{product}/{YYYY}/{JJJ}/{HH}/`` holds every channel of every
  scan in that hour, each file stamped ``_sYYYYJJJHHMMSS``. One listing gives
  both the scan times and their completeness.
- AHI: ``{bucket}/{product}/{YYYY}/{MM}/{DD}/{HHMM}/`` is one directory per
  10-minute timeline, so the day listing gives the scan times and the timeline
  listing gives the keys.
"""

import datetime as dt
import functools
import re
from dataclasses import dataclass
from pathlib import Path

from shachen_ops.config import Region

#: Scans older than this are never enumerated by a backfill window; a cron
#: that has been down longer than this needs an explicit ``--since``.
MAX_WINDOW = dt.timedelta(days=2)


@dataclass(frozen=True, order=True)
class Scan:
    """One scan of one region, identified by its UTC start time."""

    start: dt.datetime

    @property
    def stamp(self) -> str:
        """``YYYYMMDDTHHMMSS``-ish compact form used in log lines."""
        return self.start.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def prefix(self) -> str:
        """The ``YYYY/MM/DD/HHMMSS`` path element this scan publishes under."""
        return self.start.strftime("%Y/%m/%d/%H%M%S")


@functools.cache
def anon_fs():
    """Anonymous S3 filesystem, shared across calls (s3fs caches listings)."""
    import s3fs

    return s3fs.S3FileSystem(anon=True)


def _ls(fs, prefix: str) -> list[str]:
    """List ``prefix``, treating a missing prefix as empty rather than an error."""
    try:
        return fs.ls(prefix, refresh=True)
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------
# ABI
# --------------------------------------------------------------------------

_ABI_STAMP_RE = re.compile(r"_s(\d{13})")


def abi_scan_start(key: str) -> dt.datetime:
    """Parse the ``_sYYYYJJJHHMMSSs`` field of an ABI filename (UTC)."""
    match = _ABI_STAMP_RE.search(Path(key).name)
    if match is None:
        raise ValueError(f"no _s timestamp in {key!r}")
    return dt.datetime.strptime(match.group(1), "%Y%j%H%M%S").replace(tzinfo=dt.UTC)


def _abi_hour_prefixes(region: Region, start: dt.datetime, end: dt.datetime) -> list[str]:
    """Every hour directory covering [start, end], oldest first."""
    hour = start.replace(minute=0, second=0, microsecond=0)
    prefixes = []
    while hour <= end:
        prefixes.append(f"{region.bucket}/{region.product}/{hour:%Y/%j/%H}/")
        hour += dt.timedelta(hours=1)
    return prefixes


def _abi_scans(region: Region, start: dt.datetime, end: dt.datetime, fs) -> list[Scan]:
    """Complete ABI scans in [start, end] — every channel present, or dropped.

    A scan whose last channels are still uploading would otherwise be picked
    up seconds after the first file lands and processed with missing bands.
    """
    by_start: dict[dt.datetime, set[str]] = {}
    for prefix in _abi_hour_prefixes(region, start, end):
        for key in _ls(fs, prefix):
            name = Path(key).name
            if not name.endswith(".nc"):
                continue
            channel = next(
                (c for c in region.channels if f"-M6{c}_" in name or f"{c}_" in name), None
            )
            if channel is None:
                continue
            when = abi_scan_start(key)
            if start <= when <= end:
                by_start.setdefault(when, set()).add(channel)
    wanted = set(region.channels)
    return sorted(Scan(when) for when, found in by_start.items() if wanted <= found)


def _abi_keys(region: Region, scan: Scan, fs) -> list[str]:
    """The one key per channel belonging to ``scan``."""
    prefix = f"{region.bucket}/{region.product}/{scan.start:%Y/%j/%H}/"
    keys = _ls(fs, prefix)
    if not keys:
        raise FileNotFoundError(f"nothing under s3://{prefix}")
    selected = []
    for channel in region.channels:
        matches = [
            k for k in keys if f"{channel}_" in Path(k).name and abi_scan_start(k) == scan.start
        ]
        if not matches:
            raise FileNotFoundError(f"no {channel} file for {scan.stamp} under s3://{prefix}")
        selected.append(sorted(matches)[0])
    return selected


# --------------------------------------------------------------------------
# AHI
# --------------------------------------------------------------------------

#: ``_S{segment:02d}{total:02d}`` in an HSD basename: which strip of how many.
_AHI_SEGMENT_RE = re.compile(r"_S(\d{2})(\d{2})(?=[._])")
_AHI_RESOLUTION_RE = re.compile(r"_R(\d{2})_")

#: A full-disk HSD timeline is cut into 10 north-to-south strips over the
#: 2-km fixed grid's +-5,500,000 m in y.
AHI_SEGMENTS = 10
AHI_FLDK_Y_EXTENT = 5_500_000.0

#: The AHI fixed grid's projection (Himawari-8/9 parked at 140.7E).
AHI_PROJ = {
    "proj": "geos",
    "h": 35785863.0,
    "lon_0": 140.7,
    "sweep": "y",
    "a": 6378137.0,
    "rf": 298.257024882273,
}

#: Slop added to a domain's projected extent before it becomes strip numbers,
#: so an ellipsoid or pixel-center difference can never truncate the crop.
_SEGMENT_MARGIN_M = 20_000.0


def ahi_segments_for_bbox(bbox: tuple[float, float, float, float] | None) -> tuple[int, ...]:
    """The 1-based HSD strip numbers whose data covers ``bbox`` (None -> all).

    A regional domain needs only a few of the ten strips — the north-China
    dust domain lives in S01-S03 — so this is roughly a quarter of the bytes
    the whole disk would cost. The domain boundary is projected onto the fixed
    grid rather than read off latitude alone: in a geostationary projection y
    depends on longitude too, so the northernmost projected point need not sit
    at the northernmost corner.
    """
    if bbox is None:
        return tuple(range(1, AHI_SEGMENTS + 1))
    from pyproj import CRS, Transformer

    lon_min, lat_min, lon_max, lat_max = bbox
    steps = 100
    lons = [lon_min + (lon_max - lon_min) * i / steps for i in range(steps + 1)]
    lats = [lat_min + (lat_max - lat_min) * i / steps for i in range(steps + 1)]
    boundary = (
        [(lon, lat_min) for lon in lons]
        + [(lon, lat_max) for lon in lons]
        + [(lon_min, lat) for lat in lats]
        + [(lon_max, lat) for lat in lats]
    )
    transformer = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_dict(AHI_PROJ), always_xy=True)
    ys = [
        y
        for _, y in (transformer.transform(lon, lat) for lon, lat in boundary)
        if abs(y) != float("inf") and y == y
    ]
    if not ys:
        raise RuntimeError(f"bbox {bbox} does not intersect the AHI disk")

    strip = 2 * AHI_FLDK_Y_EXTENT / AHI_SEGMENTS
    first = int((AHI_FLDK_Y_EXTENT - (max(ys) + _SEGMENT_MARGIN_M)) // strip) + 1
    last = int((AHI_FLDK_Y_EXTENT - (min(ys) - _SEGMENT_MARGIN_M)) // strip) + 1
    return tuple(range(max(1, first), min(AHI_SEGMENTS, last) + 1))


def _in_segments(key: str, segments: tuple[int, ...]) -> bool:
    """Whether an HSD key belongs to ``segments`` — single-file disks always do."""
    match = _AHI_SEGMENT_RE.search(Path(key).name)
    if match is None:
        return True
    segment, total = int(match.group(1)), int(match.group(2))
    return total <= 1 or segment in segments


def select_ahi_keys(
    keys: list[str],
    channels: tuple[str, ...],
    segments: tuple[int, ...] | None = None,
) -> list[str]:
    """Pick each channel's HSD keys for ``channels`` from a timeline listing.

    Prefers the 2-km ``_R20_`` repackaging where it exists; timelines from
    ~2019 on carry native HSD only (B03 at R05, B05 at R10), so with no R20
    match the coarsest resolution present for that channel is taken instead
    and ``load_scene``'s native resample aggregates it onto the 2-km grid.
    ``segments`` keeps only the strips a regional domain needs. A channel with
    no usable key raises RuntimeError naming it.
    """
    selected: list[str] = []
    for channel in channels:
        candidates = [k for k in keys if f"_{channel}_" in Path(k).name]
        if segments is not None:
            candidates = [k for k in candidates if _in_segments(k, segments)]
        matches = sorted(k for k in candidates if "_R20_" in k)
        if not matches and candidates:
            coarsest = max(
                match.group(1)
                for k in candidates
                if (match := _AHI_RESOLUTION_RE.search(Path(k).name))
            )
            matches = sorted(k for k in candidates if f"_R{coarsest}_" in k)
        if not matches:
            raise RuntimeError(f"no {channel} files in timeline listing")
        selected.extend(matches)
    return selected


def ahi_local_name(key: str) -> str:
    """Local filename an AHI S3 key is stored under.

    satpy's ``ahi_hsd`` ``.DAT.bz2`` patterns hardcode each band's *native*
    resolution, so the repackaged 2-km B03 is unreadable compressed; those
    keys lose the ``.bz2`` suffix and are stored decompressed.
    """
    name = Path(key).name
    if "_B03_" in name and "_R20_" in name and name.endswith(".bz2"):
        return name[: -len(".bz2")]
    return name


def _ahi_scans(region: Region, start: dt.datetime, end: dt.datetime, fs) -> list[Scan]:
    """AHI timelines in [start, end], from the per-day directory listings.

    Completeness is not checked here: proving a timeline has all eight bands
    costs one listing per timeline, and the fetch does that listing anyway.
    """
    scans: list[Scan] = []
    day = start.date()
    while day <= end.date():
        prefix = f"{region.bucket}/{region.product}/{day:%Y/%m/%d}/"
        for entry in _ls(fs, prefix):
            name = Path(entry).name
            if len(name) != 4 or not name.isdigit():
                continue
            when = dt.datetime.combine(day, dt.time(int(name[:2]), int(name[2:])), tzinfo=dt.UTC)
            if start <= when <= end:
                scans.append(Scan(when))
        day += dt.timedelta(days=1)
    return sorted(scans)


def _ahi_keys(region: Region, scan: Scan, fs) -> list[str]:
    """The HSD keys of ``scan``'s timeline, restricted to the region's strips."""
    prefix = f"{region.bucket}/{region.product}/{scan.start:%Y/%m/%d/%H%M}/"
    keys = _ls(fs, prefix)
    if not keys:
        raise FileNotFoundError(f"nothing under s3://{prefix}")
    return select_ahi_keys(keys, region.channels, ahi_segments_for_bbox(region.bbox))


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

_SCANNERS = {"abi": _abi_scans, "ahi": _ahi_scans}
_KEYERS = {"abi": _abi_keys, "ahi": _ahi_keys}


def available_scans(
    region: Region,
    start: dt.datetime,
    end: dt.datetime,
    fs=None,
) -> list[Scan]:
    """Scans of ``region`` starting in [start, end], oldest first.

    A window longer than :data:`MAX_WINDOW` raises ValueError rather than
    quietly listing weeks of S3 — a backfill that wide is a decision, not an
    accident.
    """
    if end < start:
        raise ValueError(f"window ends before it starts: {start} .. {end}")
    if end - start > MAX_WINDOW:
        raise ValueError(f"window {end - start} exceeds the {MAX_WINDOW} maximum")
    return _SCANNERS[region.sensor](region, start, end, fs or anon_fs())


def scan_keys(region: Region, scan: Scan, fs=None) -> list[str]:
    """The S3 keys making up one scan; incomplete scans raise."""
    return _KEYERS[region.sensor](region, scan, fs or anon_fs())


def on_cadence(
    found: list[Scan],
    every_minutes: int,
    start: dt.datetime,
    end: dt.datetime,
) -> list[Scan]:
    """Thin ``found`` down to one scan per publishing slot.

    ABI scans CONUS every five minutes; publishing all of it would be 288 runs
    a day per region for a product nobody reads that fast. The slot grid is
    anchored to the UTC day rather than to the window, so two runs with
    different windows agree on which scan belongs to a slot and a rerun
    republishes the same key instead of a neighbouring one.

    Only slots whose whole half-slot catchment lies inside [start, end] are
    considered: a slot half outside the window could otherwise pick one scan
    now and a better one on the next run, publishing both.
    """
    if every_minutes <= 0:
        return list(found)
    slot = dt.timedelta(minutes=every_minutes)
    half = slot / 2
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)

    picked: list[Scan] = []
    steps = int((end - day) / slot) + 1
    for step in range(steps):
        moment = day + slot * step
        if moment - half < start or moment + half > end:
            continue
        near = [scan for scan in found if abs(scan.start - moment) <= half]
        if near:
            picked.append(min(near, key=lambda scan: abs(scan.start - moment)))
    return sorted(set(picked))


def latest_scan(region: Region, now: dt.datetime | None = None, fs=None) -> Scan:
    """The newest complete scan at or before ``now`` (default: wall clock).

    Looks back six hours before giving up — long enough to ride out a routine
    upload gap, short enough that a genuinely dead feed fails fast.
    """
    now = now or dt.datetime.now(dt.UTC)
    scans = available_scans(region, now - dt.timedelta(hours=6), now, fs=fs)
    if not scans:
        raise FileNotFoundError(
            f"no {region.key} scan in the six hours before {now:%Y-%m-%d %H:%M}Z"
        )
    return scans[-1]


def nearest_scan(region: Region, when: dt.datetime, fs=None) -> Scan:
    """The scan closest to ``when``, searched within one hour either side."""
    window = dt.timedelta(hours=1)
    scans = available_scans(region, when - window, when + window, fs=fs)
    if not scans:
        raise FileNotFoundError(f"no {region.key} scan within an hour of {when:%Y-%m-%d %H:%M}Z")
    return min(scans, key=lambda scan: abs(scan.start - when))
