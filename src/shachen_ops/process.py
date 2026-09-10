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

from shachen_ops import fetch, gates
from shachen_ops.config import Region
from shachen_ops.scans import Scan

#: Fields the published netCDF carries: the confidence product and its gated
#: form, the three raw branch sums behind them, the three dust tests, the
#: background signals, and the window BT and visible reflectance for context.
#: The two RGB composites are deliberately absent — they are two thirds of the
#: uncompressed volume and the PNGs beside them already carry that picture.
#:
#: The raw sums (Eqs. 16-18 before the Eq. 19 normalization) are what makes
#: the file re-scorable offline: Eq. 19 is a clipped monotone map, so a
#: different interval or calibration can be applied to a published scan
#: without the L1b. ``cf_comb`` alone cannot be inverted where it is 0, and
#: ``dt1 + dt2 + dt3`` is not the raw sum — the cloud mask multiplies it.
#: The reflectance is there so the visible gate can be re-thresholded the
#: same way.
NETCDF_VARS = (
    "cf_comb",
    "cf_gated",
    "cf_day_raw",
    "cf_trm_raw",
    "cf_ngt_raw",
    "dt1",
    "dt2",
    "dt3",
    "rsw_bg",
    "btd_bg",
    "zenith_deg",
    "bt_tir_104",
    "refl_vis_064",
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


def product_fields(debra: xr.Dataset, scene: xr.Dataset, region: Region) -> tuple[xr.Dataset, str]:
    """``debra`` with this pipeline's additions, and the name of the field to paint.

    Two things are added to what :func:`shachen.pipeline.run_debra` returns.
    The raw branch sums (Eqs. 16-18 before Eq. 19) ride along so a published
    scan can be re-scored offline; they are the algorithm's own intermediates.
    With the region's ``gates`` on, ``cf_gated`` is the confidence with the
    :mod:`shachen_ops.gates` cloud gates applied, and it is what the enhanced
    PNG is coloured by; otherwise the PNG paints ``cf_comb``. ``cf_comb``
    itself is never touched.
    """
    from shachen.confidence import confidence_raw

    debra = debra.merge(confidence_raw(debra, debra, region.constants.confidence))
    if not region.gates:
        return debra, "cf_comb"
    debra["cf_gated"] = gates.from_debra(debra, scene)
    return debra, "cf_gated"


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

    When the region has ``gates`` on (the default), the enhanced PNG is
    coloured by ``cf_gated`` rather than ``cf_comb``: DEBRA's confidence with
    the two :mod:`shachen_ops.gates` cloud gates applied. ``cf_comb`` is
    computed and published exactly as `shachen` produced it either way; the
    gates are a product-layer decision recorded in the scan's metadata.
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

    debra, painted = product_fields(
        run_debra(scene, skin_temperature, emissivity, region.constants), scene, region
    )
    # debra_imagery colours by ``cf_comb``; hand it the field to paint under
    # that name. ``debra`` itself keeps cf_comb as computed.
    imagery = debra_imagery(scene, debra.assign(cf_comb=debra[painted]), region.constants, dimming)
    result = xr.merge(
        [debra, imagery, run_dust_rgb(scene), scene[["bt_tir_104", "refl_vis_064"]]],
        combine_attrs="drop",
    )
    result.attrs.update(scene.attrs)

    files = render(result, out_dir, region, dpi=dpi)
    files["debra.nc"] = write_netcdf(result, out_dir / "debra.nc")

    metadata = {
        "scene_start_time": scene.attrs["start_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reader": region.reader,
        "background": "semianalytic",
        "color": color,
        # Which confidence field the enhanced PNG is coloured by, and what
        # stands between cf_comb and it. A reader comparing scans across a
        # configuration change needs this beside the picture.
        "imagery_field": painted,
        "gates": _gate_provenance() if region.gates else None,
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


def _gate_provenance() -> dict:
    """The gates :func:`shachen_ops.gates.from_debra` applies, as ``meta.json`` records them."""
    return {
        "split_window": "cf 0 where dt1 = dt2 = 0",
        "visible": {
            "refl_vis_064_over_cos_zenith_min_pct": gates.VIS_CLOUD_REFL_PCT,
            "zenith_max_deg": gates.VIS_ZENITH_MAX_DEG,
        },
    }


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
