"""Skin temperature from the GFS 0.25-degree analysis on AWS Open Data.

DEBRA's background needs a skin temperature field, and shachen was built
against MERRA-2 TS — which lands two to four weeks late and so cannot drive a
near-real-time pipeline. GFS ``TMP:surface`` is the operational stand-in: the
same quantity from a model that publishes about four hours behind its cycle.

Only one GRIB2 record is ever downloaded. Each ``pgrb2.0p25`` file is roughly
half a gigabyte and ships a ``.idx`` sidecar listing every record's byte
offset, so the fetch reads the index, finds ``TMP:surface``, and pulls that
one record with a ranged GET — about a megabyte.

The substitution is recorded in every scan's metadata (see
:mod:`shachen_ops.publish`) so outputs stay traceable to the field that made
them.
"""

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr

BUCKET = "noaa-gfs-bdp-pds"

#: GFS runs 6-hourly and publishes hourly forecast hours out to f120.
CYCLE_HOURS = 6
MAX_FORECAST_HOURS = 24

#: How many cycles back to try before giving up. The newest cycle is roughly
#: four hours behind its own analysis time, so the first one or two candidates
#: are routinely absent and the walk back is the normal path, not the sad one.
MAX_CYCLES_BACK = 5

#: The wgrib2-style index line for the field we want.
_RECORD = ":TMP:surface:"


@dataclass(frozen=True)
class SkinTemperatureSource:
    """Where one skin-temperature field came from, for the scan metadata."""

    path: Path
    cycle: dt.datetime
    forecast_hour: int

    @property
    def valid_time(self) -> dt.datetime:
        """The hour this field is valid at."""
        return self.cycle + dt.timedelta(hours=self.forecast_hour)

    def as_metadata(self) -> dict:
        """JSON-serializable provenance for ``meta.json``."""
        return {
            "source": "GFS 0.25deg TMP:surface",
            "cycle": self.cycle.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast_hour": self.forecast_hour,
            "valid_time": self.valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def _key(cycle: dt.datetime, forecast_hour: int) -> str:
    """S3 key of one GFS 0.25-degree pressure-level file."""
    return (
        f"{BUCKET}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/"
        f"gfs.t{cycle:%H}z.pgrb2.0p25.f{forecast_hour:03d}"
    )


def candidate_sources(when: dt.datetime) -> list[tuple[dt.datetime, int]]:
    """(cycle, forecast hour) pairs for ``when``, freshest analysis first.

    The target is the whole hour nearest the scan; each candidate is a cycle
    that reaches it, newest cycle first, so the chosen field is the shortest
    forecast available rather than the first one that happens to exist.
    """
    valid = (when + dt.timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    latest = valid.replace(hour=valid.hour - valid.hour % CYCLE_HOURS)
    candidates = []
    for step in range(MAX_CYCLES_BACK):
        cycle = latest - dt.timedelta(hours=CYCLE_HOURS * step)
        forecast_hour = round((valid - cycle).total_seconds() / 3600)
        if 0 <= forecast_hour <= MAX_FORECAST_HOURS:
            candidates.append((cycle, forecast_hour))
    return candidates


def record_range(index: str, record: str = _RECORD) -> tuple[int, int | None]:
    """Byte range of ``record`` in the GRIB file its ``.idx`` describes.

    Index lines are ``num:offset:d=YYYYMMDDHH:VAR:level:...``; a record runs
    from its own offset to the next line's, and the last record runs to the
    end of the file (None).
    """
    lines = [line for line in index.splitlines() if line.strip()]
    offsets = [int(line.split(":")[1]) for line in lines]
    for position, line in enumerate(lines):
        if record in line:
            start = offsets[position]
            end = offsets[position + 1] - 1 if position + 1 < len(offsets) else None
            return start, end
    raise LookupError(f"no {record!r} record in the GRIB index ({len(lines)} records)")


def fetch_skin_temperature(
    when: dt.datetime,
    out_dir: Path,
    fs=None,
) -> SkinTemperatureSource:
    """Download the ``TMP:surface`` record covering ``when``.

    Walks :func:`candidate_sources` until one cycle's index is on S3, extracts
    that cycle's surface-temperature record with a ranged read, and caches it
    as ``<out_dir>/gfs_ts_<cycle>_f<hhh>.grib2``. Nothing available in the
    whole walk-back raises FileNotFoundError.
    """
    from shachen_ops.scans import anon_fs

    fs = fs or anon_fs()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tried = []
    for cycle, forecast_hour in candidate_sources(when):
        key = _key(cycle, forecast_hour)
        dest = out_dir / f"gfs_ts_{cycle:%Y%m%d%H}_f{forecast_hour:03d}.grib2"
        source = SkinTemperatureSource(dest, cycle, forecast_hour)
        if dest.exists():
            return source
        tried.append(f"{cycle:%Y-%m-%d %H}z f{forecast_hour:03d}")
        if not fs.exists(f"{key}.idx"):
            continue
        index = fs.cat_file(f"{key}.idx").decode()
        start, end = record_range(index)
        # end is inclusive in the index arithmetic above; s3fs cat_file's end
        # is exclusive, so hand it one past the last byte.
        payload = fs.cat_file(key, start=start, end=None if end is None else end + 1)
        partial = dest.with_suffix(".grib2.part")
        partial.write_bytes(payload)
        partial.replace(dest)
        return source
    raise FileNotFoundError(
        f"no GFS surface temperature for {when:%Y-%m-%d %H:%M}Z; tried {', '.join(tried)}"
    )


def load_skin_temperature(path: Path) -> xr.DataArray:
    """Read a cached GFS record as skin temperature (K) on a lat/lon grid.

    Returned with dims ``(latitude, longitude)`` and longitudes wrapped into
    -180..180 ascending, which is what
    :func:`shachen.geo.regrid_latlon` interpolates against and what
    ``AreaDefinition.get_lonlats`` produces on the target side. A domain
    crossing the antimeridian would need the 0..360 convention instead;
    :func:`shachen_ops.config.load_regions` rejects those.
    """
    dataset = xr.open_dataset(
        path,
        engine="cfgrib",
        backend_kwargs={"indexpath": ""},
    )
    with dataset:
        field = dataset["t"].load()

    field = field.assign_coords(longitude=((field["longitude"] + 180) % 360) - 180)
    field = field.sortby("longitude").sortby("latitude")
    field = xr.DataArray(
        np.asarray(field.values, dtype="float32"),
        dims=("latitude", "longitude"),
        coords={"latitude": field["latitude"].values, "longitude": field["longitude"].values},
        name="skin_temperature",
    )
    field.attrs["units"] = "K"
    return field


_STAMP_RE = re.compile(r"gfs_ts_(\d{10})_f(\d{3})\.grib2$")


def source_from_path(path: Path) -> SkinTemperatureSource:
    """Recover the cycle and forecast hour a cached record was named for."""
    match = _STAMP_RE.search(Path(path).name)
    if match is None:
        raise ValueError(f"not a cached GFS record name: {path}")
    cycle = dt.datetime.strptime(match.group(1), "%Y%m%d%H").replace(tzinfo=dt.UTC)
    return SkinTemperatureSource(Path(path), cycle, int(match.group(2)))
