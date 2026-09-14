"""Staging the CAMEL emissivity climatology into R2, once per region-month.

CAMEL is a monthly 0.05-degree emissivity climatology behind Earthdata
authentication. Pulling a global granule on every scan would be absurd, and
its credentials have no business in a job that runs every twenty minutes, so
it is fetched once per region and month by a manually triggered workflow,
interpolated to the DEBRA band centers, cropped to the region, and written to
a private R2 prefix. The operational path reads only that.

The record has an end. ``CAM5K30EM`` V003 runs through December 2023 and no
further, so an operational month in 2026 has no granule of its own. Because
the product *is* a climatology, the honest substitution is the same calendar
month from the most recent year that has one — :func:`resolve_month` walks
back year by year to find it, and the year actually used is recorded in the
staged file and echoed into every scan's metadata.
"""

import datetime as dt
import re
from pathlib import Path

import xarray as xr

from shachen_ops.config import Region
from shachen_ops.fetch import camel_key

#: Degrees of padding around the region bbox. The regrid onto the satellite
#: grid is bilinear, so the staged subset has to overhang the domain; a degree
#: is twenty CAMEL cells, comfortably more than the interpolation reads.
MARGIN_DEG = 1.0

#: How many years back :func:`resolve_month` looks for a calendar month. The
#: record currently ends in 2023, so this has to cover the gap to today and
#: keep covering it as the gap grows.
LOOKBACK_YEARS = 12

_LAT_NAMES = ("latitude", "lat")
_LON_NAMES = ("longitude", "lon")

_ENCODING = {"zlib": True, "complevel": 4}


def resolve_month(
    month: dt.date, out_dir: Path, lookback: int = LOOKBACK_YEARS
) -> tuple[Path, dt.date]:
    """Download the CAMEL granule for ``month``, or its nearest earlier year.

    Returns ``(path, source_month)`` so callers can record which year was
    actually used. Nothing found within ``lookback`` years raises
    RuntimeError.

    From shachen 0.3.0 ``fetch_emissivity`` walks back the years itself and
    returns the earlier granule with a warning, so the month actually served
    is read off the file it hands back rather than assumed to be the one
    asked for; the loop here only matters for a shachen that raises instead.
    """
    from shachen.io.emissivity import fetch_emissivity

    errors = []
    for back in range(lookback + 1):
        candidate = month.replace(year=month.year - back, day=1)
        try:
            granule = fetch_emissivity(candidate, out_dir)
        except RuntimeError as error:  # no granule for that year; try the one before
            errors.append(f"{candidate:%Y-%m}: {error}")
            continue
        return granule, granule_month(granule) or candidate
    joined = "\n  ".join(errors)
    raise RuntimeError(f"no CAMEL granule for {month:%Y-%m} within {lookback} years:\n  {joined}")


_GRANULE_MONTH_RE = re.compile(r"CAM5K30EM.*?(\d{4})(\d{2})")


def granule_month(path: Path) -> dt.date | None:
    """The month a CAMEL granule carries, from its ``CAM5K30EM_YYYYMM`` name.

    ``None`` for a file not named that way, so a caller can fall back to the
    month it asked for rather than fail on a renamed granule.
    """
    match = _GRANULE_MONTH_RE.search(Path(path).name)
    if match is None:
        return None
    return dt.date(int(match.group(1)), int(match.group(2)), 1)


def _dim(dataset: xr.Dataset, candidates: tuple[str, ...]) -> str:
    for name in candidates:
        if name in dataset.dims:
            return name
    raise KeyError(f"emissivity has none of {candidates}; got {tuple(dataset.dims)}")


def subset(emissivity: xr.Dataset, bbox: tuple[float, float, float, float]) -> xr.Dataset:
    """Crop a global emissivity Dataset to ``bbox`` plus :data:`MARGIN_DEG`.

    The coordinates are sorted ascending first: CAMEL stores latitude
    north-to-south, and a ``slice`` against a descending coordinate silently
    selects nothing.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lat, lon = _dim(emissivity, _LAT_NAMES), _dim(emissivity, _LON_NAMES)
    ordered = emissivity.sortby(lat).sortby(lon)
    return ordered.sel(
        {
            lat: slice(lat_min - MARGIN_DEG, lat_max + MARGIN_DEG),
            lon: slice(lon_min - MARGIN_DEG, lon_max + MARGIN_DEG),
        }
    )


def bootstrap_camel(region: Region, month: dt.date, work_dir: Path, store) -> str:
    """Fetch, subset and stage ``region``'s CAMEL month; returns the R2 key.

    Needs Earthdata credentials in a netrc (``earthaccess`` reads them), which
    is why this lives behind its own workflow. The key is named for the
    *operational* month so the hot path can look it up without knowing about
    the climatology substitution; the granule's own month is in the file's
    ``source_month`` attribute. Existing keys are overwritten — restaging is
    how a bad subset gets fixed.
    """
    from shachen.io.emissivity import load_band_emissivity

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    granule, source_month = resolve_month(month, work_dir / "camel_granules")
    cropped = subset(load_band_emissivity(granule), region.bbox)
    if 0 in cropped.sizes.values():
        raise ValueError(f"CAMEL subset for region {region.key!r} is empty; check its bbox")

    cropped.attrs.update(
        {
            "region": region.key,
            "month": f"{month:%Y-%m}",
            "source_month": f"{source_month:%Y-%m}",
            "source": "CAMEL CAM5K30EM V003 monthly emissivity",
            "bbox": list(region.bbox),
            "margin_deg": MARGIN_DEG,
        }
    )
    local = work_dir / f"camel_{region.key}_{month:%Y%m}.nc"
    cropped.to_netcdf(local, encoding={name: dict(_ENCODING) for name in cropped.data_vars})

    key = camel_key(region, month)
    store.put_file(key, local)
    return key
