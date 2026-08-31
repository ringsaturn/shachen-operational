"""``shachen-ops`` — the one entry point the workflows call.

Four subcommands, all keyed on a region from ``regions.toml``:

``run``
    Process one scan (``--time``, or the newest available) and publish it.
``backfill``
    Process every scan in a window that the manifests do not already carry,
    newest first. This is what the schedule runs: GitHub crons fire late and
    sometimes not at all, so the job asks the bucket what is missing rather
    than assuming the cron told it.
``bootstrap-camel``
    Stage a region-month of CAMEL emissivity into R2. Manual, credentialed.
``scans``
    List what S3 has and what the bucket already carries. Diagnostics only.
"""

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from shachen_ops import bootstrap, process, publish, scans
from shachen_ops.config import REGIONS_TOML, Region, get_region, load_regions

#: Scans younger than this are left for the next run: an AHI timeline's
#: directory appears before every strip is in it, and processing a scan that is
#: still uploading fails in the reader rather than anywhere useful.
SETTLE = dt.timedelta(minutes=3)

DEFAULT_WINDOW = "2h"
DEFAULT_WORK_DIR = Path("data")
DEFAULT_OUT_DIR = Path("out")

_DURATION_RE = re.compile(r"^(\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> dt.timedelta:
    """``90m``/``2h``/``1d`` to a timedelta; anything else raises ValueError."""
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise ValueError(f"not a duration like 90m, 2h or 1d: {text!r}")
    return dt.timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


def parse_time(text: str) -> dt.datetime:
    """An ISO-8601 UTC instant; a naive one is read as UTC."""
    when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _log(message: str) -> None:
    print(message, flush=True)


def _process_and_publish(
    region: Region,
    scan: scans.Scan,
    store,
    args: argparse.Namespace,
) -> dict:
    """Run one scan and publish it, logging the two halves separately."""
    out_dir = Path(args.out) / region.key / scan.prefix
    started = dt.datetime.now(dt.UTC)
    artifacts = process.run_scan(
        region,
        scan,
        work_dir=Path(args.work),
        out_dir=out_dir,
        store=store,
        color=args.color,
        dpi=args.dpi,
    )
    elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
    _log(f"  processed in {elapsed:.0f}s -> {', '.join(sorted(artifacts.files))}")

    meta = publish.publish_scan(
        store,
        region,
        scan,
        artifacts.files,
        {**artifacts.metadata, "processing_seconds": round(elapsed, 1)},
    )
    _log(f"  published {publish.scan_prefix(region, scan)}")
    return meta


def cmd_run(args: argparse.Namespace) -> int:
    region = get_region(args.region, args.regions)
    store = publish.store_from_env(args.dest)

    if args.time in (None, "latest"):
        scan = scans.latest_scan(region)
    else:
        scan = scans.nearest_scan(region, parse_time(args.time))
    _log(f"{region.key}: scan {scan.stamp}")

    if not args.force and publish.is_published(store, region, scan):
        _log("  already published, nothing to do")
        return 0
    _process_and_publish(region, scan, store, args)
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    region = get_region(args.region, args.regions)
    store = publish.store_from_env(args.dest)

    now = dt.datetime.now(dt.UTC)
    end = now - SETTLE
    start = now - parse_duration(args.since)
    found = scans.available_scans(region, start, end)
    every = args.every if args.every is not None else region.publish_every_minutes
    candidates = scans.on_cadence(found, every, start, end)
    already = set() if args.force else publish.published_scans(store, region, start, end)
    todo = [scan for scan in candidates if scan.start not in already]
    done = len(candidates) - len(todo)
    todo.sort(key=lambda scan: scan.start, reverse=True)
    todo = todo[: args.limit]

    _log(
        f"{region.key}: {len(found)} scan(s) since {start:%Y-%m-%d %H:%M}Z, "
        f"{len(candidates)} on the {every}-min grid, {done} already published, "
        f"{len(todo)} to do (limit {args.limit})"
    )

    failures: list[str] = []
    for scan in todo:
        _log(f"{region.key}: scan {scan.stamp}")
        try:
            _process_and_publish(region, scan, store, args)
        except Exception as error:  # keep going: one bad scan must not stall the rest
            failures.append(f"{scan.stamp}: {type(error).__name__}: {error}")
            _log(f"  FAILED {type(error).__name__}: {error}")

    if failures:
        _log(f"{region.key}: {len(failures)} of {len(todo)} scan(s) failed")
        for line in failures:
            _log(f"  {line}")
        return 1
    return 0


def cmd_bootstrap_camel(args: argparse.Namespace) -> int:
    region = get_region(args.region, args.regions)
    store = publish.store_from_env(args.dest)
    month = (
        dt.date.fromisoformat(f"{args.month}-01")
        if args.month
        else dt.datetime.now(dt.UTC).date().replace(day=1)
    )
    key = bootstrap.bootstrap_camel(region, month, Path(args.work), store)
    _log(f"{region.key}: staged {key}")
    return 0


def cmd_scans(args: argparse.Namespace) -> int:
    region = get_region(args.region, args.regions)
    store = publish.store_from_env(args.dest)
    now = dt.datetime.now(dt.UTC)
    start = now - parse_duration(args.since)
    found = scans.available_scans(region, start, now)
    already = publish.published_scans(store, region, start, now)
    for scan in found:
        _log(f"{scan.stamp}  {'published' if scan.start in already else 'missing'}")
    _log(f"{region.key}: {len(found)} scan(s), {len(already)} published")
    return 0


def cmd_regions(args: argparse.Namespace) -> int:
    for key, region in sorted(load_regions(args.regions).items()):
        _log(
            f"{key:8s} {region.satellite:12s} {region.sensor}  {region.bbox}  {region.description}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shachen-ops", description=__doc__)
    parser.add_argument(
        "--regions",
        default=str(REGIONS_TOML),
        help=f"region table (default: {REGIONS_TOML})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser, *, compute: bool) -> None:
        sub.add_argument("--region", required=True, help="region key from the region table")
        sub.add_argument(
            "--dest",
            default=None,
            help="'r2' (default, needs R2_* in the environment) or a directory to publish into",
        )
        if compute:
            sub.add_argument("--work", default=str(DEFAULT_WORK_DIR), help="input cache directory")
            sub.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="artifact directory")
            sub.add_argument("--color", default="yellow", help="dust coloration preset")
            sub.add_argument("--dpi", type=int, default=150, help="rendered PNG resolution")
            sub.add_argument(
                "--force",
                action="store_true",
                help="reprocess and republish scans the manifests already carry",
            )

    run = subparsers.add_parser("run", help="process and publish one scan")
    add_common(run, compute=True)
    run.add_argument("--time", default="latest", help="scan time (ISO-8601 UTC) or 'latest'")
    run.set_defaults(func=cmd_run)

    backfill = subparsers.add_parser("backfill", help="process every unpublished scan in a window")
    add_common(backfill, compute=True)
    backfill.add_argument("--since", default=DEFAULT_WINDOW, help="window length, e.g. 90m, 2h, 1d")
    backfill.add_argument(
        "--limit",
        type=int,
        default=3,
        help="most scans to process in one invocation (a run is dominated by fetching, "
        "so this is what keeps a catch-up inside the job timeout)",
    )
    backfill.add_argument(
        "--every",
        type=int,
        default=None,
        help="publish one scan per N minutes (default: the region's publish_every_minutes)",
    )
    backfill.set_defaults(func=cmd_backfill)

    camel = subparsers.add_parser("bootstrap-camel", help="stage a region-month of CAMEL into R2")
    add_common(camel, compute=False)
    camel.add_argument("--work", default=str(DEFAULT_WORK_DIR), help="download directory")
    camel.add_argument("--month", default=None, help="YYYY-MM (default: the current month)")
    camel.set_defaults(func=cmd_bootstrap_camel)

    listing = subparsers.add_parser("scans", help="list available and published scans")
    add_common(listing, compute=False)
    listing.add_argument("--since", default=DEFAULT_WINDOW, help="window length, e.g. 90m, 2h, 1d")
    listing.set_defaults(func=cmd_scans)

    regions = subparsers.add_parser("regions", help="list the configured regions")
    regions.set_defaults(func=cmd_regions)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
