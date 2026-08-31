"""One scan, start to finished artifacts on local disk.

This is the compute half of the pipeline: fetch the inputs, run DEBRA and the
Dust RGB baseline over the cropped scene, and write the three files a scan
publishes — the enhanced PNG, the baseline PNG, and a compressed netCDF of the
fields behind them. Uploading is :mod:`shachen_ops.publish`'s job; nothing here
touches the object store except to read the staged CAMEL month.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr

from shachen_ops import fetch
from shachen_ops.config import Region
from shachen_ops.scans import Scan

#: Fields the published netCDF carries: the confidence product, the three dust
#: tests behind it, the background signals, and the window BT for context.
#: The two RGB composites are deliberately absent — they are two thirds of the
#: uncompressed volume and the PNGs beside them already carry that picture.
NETCDF_VARS = (
    "cf_comb",
    "dt1",
    "dt2",
    "dt3",
    "rsw_bg",
    "btd_bg",
    "zenith_deg",
    "bt_tir_104",
)

#: Deflate at level 4 — most of the ratio at a fraction of level 9's CPU — and
#: store single precision. Everything here comes out of float64 arithmetic on
#: fields whose own precision is nowhere near that, so the second half of every
#: mantissa is noise being paid for by the gigabyte.
_ENCODING = {"zlib": True, "complevel": 4, "dtype": "float32"}

#: Attribute value types the netCDF writer accepts as-is.
_NC_SCALARS = (str, int, float, complex, bool, np.number, np.ndarray, list, tuple)


@dataclass(frozen=True)
class Artifacts:
    """What one processed scan produced, and how it was produced."""

    files: dict[str, Path]
    metadata: dict


def _sanitize(value: object) -> object:
    """Coerce one attr value into something netCDF can store."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, _NC_SCALARS):
        return value
    # AreaDefinition and anything else exotic: keep the description only.
    return str(value)


def _sanitize_attrs(attrs: dict) -> dict:
    return {str(key): _sanitize(value) for key, value in attrs.items()}


def write_netcdf(result: xr.Dataset, path: Path) -> Path:
    """Write the published subset of ``result``, compressed, attrs serializable."""
    present = [name for name in NETCDF_VARS if name in result]
    subset = result[present]
    subset.attrs = _sanitize_attrs(result.attrs)
    for name in subset.variables:
        subset[name].attrs = _sanitize_attrs(result[name].attrs)
    encoding = {
        name: dict(_ENCODING) if subset[name].dtype.kind == "f" else {"zlib": True, "complevel": 4}
        for name in present
    }
    subset.to_netcdf(path, encoding=encoding)
    return path


def render(result: xr.Dataset, out_dir: Path, region: Region, dpi: int = 150) -> dict[str, Path]:
    """Render the enhanced DEBRA PNG and the Dust RGB baseline beside it."""
    from shachen.imagery import to_uint8
    from shachen.render import render_debra_png

    area = result.attrs["area"]
    when = result.attrs["start_time"]
    label = region.description or region.key

    files = {
        "debra.png": render_debra_png(
            to_uint8(result["rgb"]),
            area,
            when,
            out_dir / "debra.png",
            title=f"DEBRA-Dust — {label} — {when:%Y-%m-%d %H:%M} UTC",
            dpi=dpi,
        )
    }
    if "dust_rgb" in result:
        files["dustrgb.png"] = render_debra_png(
            to_uint8(result["dust_rgb"]),
            area,
            when,
            out_dir / "dustrgb.png",
            title=f"Dust RGB — {label} — {when:%Y-%m-%d %H:%M} UTC",
            dpi=dpi,
        )
    return files


def run_scan(
    region: Region,
    scan: Scan,
    work_dir: Path,
    out_dir: Path,
    store,
    color: str = "yellow",
    dpi: int = 150,
) -> Artifacts:
    """Fetch, run and render one scan; returns its files and provenance.

    ``work_dir`` caches inputs (L1b, GFS, CAMEL) and ``out_dir`` receives the
    artifacts. The background is semianalytic — CAMEL emissivity times a Planck
    curve at the skin temperature — because it carries no state between scans;
    the composite background needs a rolling multi-day store and is not wired
    up here.
    """
    from shachen.constants import COLOR_DIMMING
    from shachen.imagery import debra_imagery
    from shachen.io.emissivity import load_band_emissivity
    from shachen.io.satellite import load_scene
    from shachen.pipeline import run_debra, run_dust_rgb

    from shachen_ops import gfs

    dimming = COLOR_DIMMING[color]  # unknown color -> KeyError
    work_dir, out_dir = Path(work_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    l1b = fetch.fetch_l1b(region, scan, work_dir / "l1b" / region.key / scan.prefix)
    ts_source = fetch.fetch_skin_temperature(scan, work_dir / "gfs")
    camel_path = fetch.fetch_emissivity(region, scan, work_dir / "camel", store)

    scene = load_scene(l1b, reader=region.reader, bbox=region.bbox)
    skin_temperature = gfs.load_skin_temperature(ts_source.path)
    emissivity = (
        load_band_emissivity(camel_path) if _is_raw_camel(camel_path) else _open(camel_path)
    )

    debra = run_debra(scene, skin_temperature, emissivity, region.constants)
    imagery = debra_imagery(scene, debra, region.constants, dimming)
    result = xr.merge(
        [debra, imagery, run_dust_rgb(scene), scene[["bt_tir_104"]]], combine_attrs="drop"
    )
    result.attrs.update(scene.attrs)

    files = render(result, out_dir, region, dpi=dpi)
    files["debra.nc"] = write_netcdf(result, out_dir / "debra.nc")

    metadata = {
        "scene_start_time": scene.attrs["start_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reader": region.reader,
        "background": "semianalytic",
        "color": color,
        "l1b_files": len(l1b),
        "skin_temperature": ts_source.as_metadata(),
        "emissivity": {
            "source": emissivity.attrs.get("source", "CAMEL CAM5K30EM V003"),
            "month": emissivity.attrs.get("month", scan.start.strftime("%Y-%m")),
            # The record ends in 2023, so an operational month is served by the
            # same calendar month of an earlier year (see shachen_ops.bootstrap).
            "source_month": emissivity.attrs.get("source_month"),
        },
    }
    return Artifacts(files=files, metadata=metadata)


def _is_raw_camel(path: Path) -> bool:
    """Whether ``path`` is a full CAMEL granule rather than a staged subset.

    The bootstrap job stages ``emis_*`` variables already interpolated to the
    DEBRA band centers; a raw granule still carries the hinge-point cube and
    has to go through ``load_band_emissivity``. Supporting both means a staged
    file can be replaced by a granule in a pinch without a code change.
    """
    with xr.open_dataset(path) as dataset:
        return not any(name.startswith("emis_") for name in dataset.data_vars)


def _open(path: Path) -> xr.Dataset:
    with xr.open_dataset(path) as dataset:
        return dataset.load()
