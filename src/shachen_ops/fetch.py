"""Getting one scan's inputs onto the runner's disk.

Three sources, three very different shapes:

- **L1b** from the anonymous NOAA buckets — the bulk of the bytes, and the
  only stage where the two sensors diverge (ABI netCDF straight to disk, AHI
  HSD strips with one band needing decompression on arrival).
- **Skin temperature** from GFS, a single ranged GRIB read
  (:mod:`shachen_ops.gfs`).
- **CAMEL emissivity** from our own R2 ancillary prefix, staged there once per
  month by the bootstrap job. Earthdata credentials never appear in the
  operational path.

Every fetch is idempotent: a file already on disk under its final name is
kept, and downloads land on a ``.part`` name first so an interrupted transfer
can never be mistaken for a complete one.
"""

import bz2
import datetime as dt
from pathlib import Path

from shachen_ops import gfs
from shachen_ops.config import ROOT_PREFIX, Region
from shachen_ops.scans import Scan, ahi_local_name, anon_fs, scan_keys

#: R2 prefix the bootstrap job stages subsetted CAMEL months under.
CAMEL_PREFIX = f"{ROOT_PREFIX}/ancillary/camel"


def camel_key(region: Region, month: dt.date) -> str:
    """R2 key of ``region``'s CAMEL emissivity for ``month``."""
    return f"{CAMEL_PREFIX}/{region.key}/{month:%Y%m}.nc"


def _download(fs, key: str, dest: Path) -> Path:
    """Fetch one S3 key to ``dest`` via a ``.part`` name; existing files kept."""
    if dest.exists():
        return dest
    partial = dest.with_name(dest.name + ".part")
    fs.get(key, str(partial))
    partial.replace(dest)
    return dest


def _download_ahi(fs, key: str, out_dir: Path) -> Path:
    """Fetch one HSD key, decompressing the 2-km B03 repackaging on arrival.

    satpy's ``ahi_hsd`` ``.DAT.bz2`` file patterns hardcode each band's native
    resolution, so the repackaged 2-km B03 is only readable uncompressed. Its
    local name loses the suffix (:func:`shachen_ops.scans.ahi_local_name`), and
    the exists-check runs against that final name so reruns stay idempotent.
    """
    dest = out_dir / ahi_local_name(key)
    if dest.exists():
        return dest
    partial = out_dir / (Path(key).name + ".part")
    fs.get(key, str(partial))
    if ahi_local_name(key) != Path(key).name:
        decompressed = dest.with_name(dest.name + ".part")
        decompressed.write_bytes(bz2.decompress(partial.read_bytes()))
        decompressed.replace(dest)
        partial.unlink()
    else:
        partial.replace(dest)
    return dest


def fetch_l1b(region: Region, scan: Scan, out_dir: Path, fs=None) -> list[Path]:
    """Download every L1b file of ``scan`` into ``out_dir``.

    For AHI only the HSD strips the region's bbox needs are fetched, which is
    what the crop would keep anyway — so the cache directory holds one bbox's
    strips, and widening a domain means refetching.
    """
    fs = fs or anon_fs()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = scan_keys(region, scan, fs=fs)
    if region.sensor == "ahi":
        return [_download_ahi(fs, key, out_dir) for key in keys]
    return [_download(fs, key, out_dir / Path(key).name) for key in keys]


def fetch_skin_temperature(scan: Scan, out_dir: Path, fs=None) -> gfs.SkinTemperatureSource:
    """The GFS surface-temperature record covering ``scan`` (see :mod:`.gfs`)."""
    return gfs.fetch_skin_temperature(scan.start, out_dir, fs=fs)


def fetch_emissivity(region: Region, scan: Scan, out_dir: Path, store) -> Path:
    """Download ``region``'s staged CAMEL month for ``scan`` from R2.

    A month that has never been bootstrapped raises FileNotFoundError naming
    the workflow that stages it — the operational job cannot reach Earthdata
    and should say so rather than fail somewhere deeper.
    """
    month = scan.start.date().replace(day=1)
    key = camel_key(region, month)
    dest = Path(out_dir) / f"camel_{region.key}_{month:%Y%m}.nc"
    if dest.exists():
        return dest
    if not store.exists(key):
        raise FileNotFoundError(
            f"{key} is not staged in R2; run the bootstrap-ancillary workflow "
            f"for region {region.key!r} and month {month:%Y-%m}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    store.download(key, dest)
    return dest
