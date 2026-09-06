"""Retention: keeping the published bucket inside a fixed storage budget.

The bucket has 2 GB to work with and the pipeline writes about 1.8 GB a day,
so nothing can be kept indefinitely and the only question is what to give up
first. A published scan is two PNGs and a manifest — about 4 MB — plus
``debra.nc``, which is 7 MB for ``swus`` and 23 MB for ``gobi`` and therefore
roughly four fifths of everything written. Retention is tiered on exactly that
split:

- the newest ``keep_field_scans`` scans are complete, ``debra.nc`` included;
- back to ``keep_scans``, the imagery and manifest survive and the field file
  is deleted;
- older than that, the scan is removed entirely.

At the defaults (144 and 12) that is three days of imagery over six hours of
field data, and a steady state near 1.5 GB. Both numbers are per region in
``regions.toml``, because a byte budget is the only thing that sets them.

Counting *scans* rather than days is deliberate: an outage must not let the
bucket grow past its ceiling once the pipeline catches up, and a count bounds
the bytes whatever the clock says.

The listing is the source of truth, not the manifests. One ``list_objects``
over the region prefix says what actually exists, which makes a prune
idempotent — a scan whose field file is already gone needs no work — and
means a half-finished publish or a manual deletion cannot desynchronise the
policy from the bucket. The manifests are then corrected to match: a dropped
scan leaves its month index, and ``latest.json`` stops advertising assets that
are no longer there.
"""

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field

from shachen_ops.config import Region
from shachen_ops.fetch import CAMEL_PREFIX
from shachen_ops.publish import (
    INDEX_CACHE,
    META_CACHE,
    POINTER_CACHE,
    index_key,
    iso_stamp,
    latest_key,
    region_prefix,
    retention_policy,
)

#: The one asset with a tier of its own. It is most of the bucket, and it is
#: also the only asset a reader cannot reconstruct from the others.
FIELDS = "debra.nc"

#: CAMEL months kept per region. The hot path looks up the *scan's* month, so
#: a backfill that reaches across a month boundary still needs the previous
#: one; two is the smallest number that never strands such a run.
ANCILLARY_MONTHS = 2

#: ``YYYY/MM/DD/HHMMSS/name`` under the region prefix. Anything that does not
#: match is a manifest — ``latest.json`` or ``index/`` — and is never pruned.
_SCAN_KEY_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})/(\d{2})(\d{2})(\d{2})/([^/]+)$")


@dataclass(frozen=True)
class Plan:
    """Which scans stay whole, which lose their field file, and which go."""

    keep: tuple[str, ...] = ()
    strip: tuple[str, ...] = ()
    drop: tuple[str, ...] = ()


@dataclass
class PruneReport:
    """What one prune did, and the footprint it left behind."""

    region: str
    dry_run: bool = False
    dropped: tuple[str, ...] = ()
    stripped: tuple[str, ...] = ()
    deleted_keys: int = 0
    freed_bytes: int = 0
    kept_scans: int = 0
    kept_bytes: int = 0
    ancillary_deleted: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        verb = "would free" if self.dry_run else "freed"
        return (
            f"{self.region}: dropped {len(self.dropped)} scan(s), "
            f"stripped {FIELDS} from {len(self.stripped)}, "
            f"{verb} {_mb(self.freed_bytes)} across {self.deleted_keys} object(s); "
            f"{self.kept_scans} scan(s) / {_mb(self.kept_bytes)} remain"
        )


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.0f} MB"


def stamp_month(stamp: str) -> dt.date:
    """The first of the month a ``YYYY-MM-DDTHH:MM:SSZ`` stamp falls in."""
    return dt.date(int(stamp[0:4]), int(stamp[5:7]), 1)


def scan_objects(store, region: Region) -> dict[str, dict[str, int]]:
    """``{scan_start: {filename: size}}`` for every scan object of ``region``.

    One listing covers the whole region. Manifest keys share the prefix and are
    filtered out by the path shape rather than by name, so a new manifest never
    becomes prunable by accident.
    """
    prefix = f"{region_prefix(region)}/"
    found: dict[str, dict[str, int]] = defaultdict(dict)
    for key, size in store.list_objects(prefix).items():
        match = _SCAN_KEY_RE.match(key[len(prefix) :])
        if match is None:
            continue
        year, month, day, hour, minute, second, name = match.groups()
        found[f"{year}-{month}-{day}T{hour}:{minute}:{second}Z"][name] = size
    return dict(found)


def plan_retention(stamps, keep_scans: int, keep_field_scans: int) -> Plan:
    """Sort ``stamps`` newest first and cut them into the three tiers."""
    ordered = sorted(set(stamps), reverse=True)
    return Plan(
        keep=tuple(ordered[:keep_field_scans]),
        strip=tuple(ordered[keep_field_scans:keep_scans]),
        drop=tuple(ordered[keep_scans:]),
    )


def _strip_meta(store, region: Region, stamp: str) -> None:
    """Rewrite one scan's ``meta.json`` so it stops advertising its field file.

    The manifest records what the scan *was* produced with; once the field file
    is gone that is no longer what a reader can fetch, so ``assets`` shrinks and
    ``fields_expired_at`` says when. A scan with no manifest — a publish that
    died before writing one — is left alone for the next backfill to redo.
    """
    day, clock = stamp[:10].replace("-", "/"), stamp[11:19].replace(":", "")
    key = f"{region_prefix(region)}/{day}/{clock}/meta.json"
    meta = store.get_json(key)
    if meta is None:
        return
    meta["assets"] = [name for name in meta.get("assets", []) if name != FIELDS]
    meta["fields_expired_at"] = iso_stamp(dt.datetime.now(dt.UTC))
    store.put_json(key, meta, cache_control=META_CACHE)


def _forget_from_indexes(store, region: Region, dropped: tuple[str, ...]) -> None:
    """Remove dropped scans from their month indexes; delete an emptied index.

    The index is what ``published_scans`` reads to decide a backfill is done,
    so leaving a deleted scan in it would advertise objects that are gone. The
    retention window is far wider than any backfill window, so a scan can never
    be pruned and then immediately reprocessed.
    """
    by_month: dict[dt.date, set[str]] = defaultdict(set)
    for stamp in dropped:
        by_month[stamp_month(stamp)].add(stamp)

    for month, stamps in by_month.items():
        key = index_key(region, month)
        payload = store.get_json(key)
        if payload is None:
            continue
        remaining = [item for item in payload.get("scans", []) if item not in stamps]
        if remaining == payload.get("scans"):
            continue
        if not remaining:
            store.delete([key])
            continue
        payload["scans"] = remaining
        payload["count"] = len(remaining)
        payload["updated_at"] = iso_stamp(dt.datetime.now(dt.UTC))
        store.put_json(key, payload, cache_control=INDEX_CACHE)


def _refresh_pointer(
    store, region: Region, dropped: tuple[str, ...], stripped: tuple[str, ...]
) -> None:
    """Drop pruned scans from ``latest.json`` and correct the asset lists.

    ``recent`` is a reader's shortcut into the archive, so every entry in it
    has to still be fetchable. The pointer itself is never at risk: it names
    the newest scan, which is by definition in the top tier.
    """
    key = latest_key(region)
    payload = store.get_json(key)
    if payload is None:
        return

    before = payload.get("recent", [])
    recent = [item for item in before if item.get("scan_start") not in dropped]
    for item in recent:
        if item.get("scan_start") in stripped:
            item["assets"] = [name for name in item.get("assets", []) if name != FIELDS]

    latest = payload.get("latest") or {}
    if latest.get("scan_start") in stripped:
        latest["assets"] = [name for name in latest.get("assets", []) if name != FIELDS]

    policy = retention_policy(region)
    if recent == before and payload.get("retention") == policy:
        return
    payload["recent"] = recent
    payload["retention"] = policy
    payload["updated_at"] = iso_stamp(dt.datetime.now(dt.UTC))
    store.put_json(key, payload, cache_control=POINTER_CACHE)


def prune_ancillary(store, region: Region, *, keep: int = ANCILLARY_MONTHS, dry_run: bool = False):
    """Delete staged CAMEL months beyond the newest ``keep``.

    Small next to the imagery — about 12 MB a month for both regions — but it
    is written once a month forever, and a budget this tight has no room for
    something that only grows. The keys end in ``YYYYMM.nc``, so a lexical sort
    is a chronological one.
    """
    objects = store.list_objects(f"{CAMEL_PREFIX}/{region.key}/")
    doomed = sorted(objects, reverse=True)[keep:]
    if doomed and not dry_run:
        store.delete(doomed)
    return tuple(doomed)


def prune_region(store, region: Region, *, dry_run: bool = False) -> PruneReport:
    """Enforce ``region``'s retention on the bucket and fix up the manifests.

    Deletion happens before the manifests are rewritten, which is the safe
    order under a crash: a manifest that still names a deleted object is a
    stale listing the next prune corrects, whereas a manifest rewritten first
    would hide objects that are still taking up space.
    """
    objects = scan_objects(store, region)
    plan = plan_retention(objects, region.keep_scans, region.keep_field_scans)

    doomed: list[str] = []
    freed = 0
    for stamp in plan.drop:
        day, clock = stamp[:10].replace("-", "/"), stamp[11:19].replace(":", "")
        for name, size in objects[stamp].items():
            doomed.append(f"{region_prefix(region)}/{day}/{clock}/{name}")
            freed += size

    # Only scans that still have a field file need touching; the rest were
    # stripped by an earlier run and must not be rewritten again.
    stripped = tuple(stamp for stamp in plan.strip if FIELDS in objects[stamp])
    stripped_bytes = 0
    for stamp in stripped:
        day, clock = stamp[:10].replace("-", "/"), stamp[11:19].replace(":", "")
        doomed.append(f"{region_prefix(region)}/{day}/{clock}/{FIELDS}")
        stripped_bytes += objects[stamp][FIELDS]
    freed += stripped_bytes

    survivors = set(plan.keep) | set(plan.strip)
    report = PruneReport(
        region=region.key,
        dry_run=dry_run,
        dropped=plan.drop,
        stripped=stripped,
        deleted_keys=len(doomed),
        freed_bytes=freed,
        kept_scans=len(survivors),
        # What the region will hold once this prune lands: everything the two
        # surviving tiers carry, less the field files this run takes out.
        kept_bytes=sum(sum(objects[stamp].values()) for stamp in survivors) - stripped_bytes,
    )
    if dry_run:
        report.ancillary_deleted = prune_ancillary(store, region, dry_run=True)
        return report

    if doomed:
        store.delete(doomed)
        for stamp in stripped:
            _strip_meta(store, region, stamp)
        _forget_from_indexes(store, region, plan.drop)
    _refresh_pointer(store, region, plan.drop, stripped)
    report.ancillary_deleted = prune_ancillary(store, region)
    return report
