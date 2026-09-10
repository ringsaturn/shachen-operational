"""The serialization edges: what the published netCDF keeps, and in what type."""

import datetime as dt

import numpy as np
import pytest
import xarray as xr

from shachen_ops.bootstrap import MARGIN_DEG, subset
from shachen_ops.config import Region
from shachen_ops.process import NETCDF_VARS, product_fields, write_netcdf


def result(shape=(4, 5)):
    fields = {name: (("y", "x"), np.random.rand(*shape)) for name in NETCDF_VARS}
    fields["rgb"] = (("y", "x", "gun"), np.random.rand(*shape, 3))
    dataset = xr.Dataset(fields)
    dataset.attrs["start_time"] = dt.datetime(2026, 8, 31, 13, 16, 18)
    dataset.attrs["area"] = object()  # stands in for the AreaDefinition
    dataset.attrs["reader"] = "abi_l1b"
    return dataset


def test_only_the_published_subset_is_written(tmp_path):
    path = write_netcdf(result(), tmp_path / "debra.nc")
    with xr.open_dataset(path) as stored:
        assert set(stored.data_vars) == set(NETCDF_VARS)
        assert "rgb" not in stored.data_vars


def test_fields_are_stored_single_precision(tmp_path):
    path = write_netcdf(result(), tmp_path / "debra.nc")
    with xr.open_dataset(path) as stored:
        assert stored["cf_comb"].dtype == np.float32


def test_unserializable_attrs_survive_as_strings(tmp_path):
    path = write_netcdf(result(), tmp_path / "debra.nc")
    with xr.open_dataset(path) as stored:
        assert stored.attrs["start_time"].startswith("2026-08-31T13:16:18")
        assert isinstance(stored.attrs["area"], str)


def test_a_missing_variable_is_skipped_rather_than_fatal(tmp_path):
    dataset = result().drop_vars("dt3")
    path = write_netcdf(dataset, tmp_path / "debra.nc")
    with xr.open_dataset(path) as stored:
        assert "dt3" not in stored.data_vars
        assert "cf_comb" in stored.data_vars


# --- product fields: raw sums and the cloud gates ---------------------------


def _field(values):
    return xr.DataArray(np.asarray(values, dtype=float), dims=("y", "x"))


def debra_output():
    """A run_debra result reduced to what product_fields reads.

    Pixel (0,0): DT3 only, dark -> the split-window gate zeroes it.
    Pixel (0,1): dt1 fired, bright at noon -> the visible gate zeroes it.
    Pixel (1,0): dt1 fired, dark -> kept.
    Pixel (1,1): NaN confidence -> stays NaN.
    """
    return xr.Dataset(
        {
            "cf_comb": _field([[0.5, 0.5], [0.5, np.nan]]),
            "dt1": _field([[0.0, 0.3], [0.3, 0.0]]),
            "dt2": _field([[0.0, 0.0], [0.0, 0.0]]),
            "dt3": _field([[0.6, 0.1], [0.1, 0.0]]),
            "cm_norm_day": _field([[0.0, 0.0], [0.5, 0.0]]),
            "cm_norm_ngt": _field([[0.0, 0.0], [0.0, 0.0]]),
            "zenith_deg": _field([[30.0, 0.0], [30.0, 30.0]]),
        }
    )


def scene():
    return xr.Dataset({"refl_vis_064": _field([[10.0, 80.0], [10.0, 10.0]])})


def region(gates=True):
    return Region(
        key="t",
        satellite="Himawari-9",
        sensor="ahi",
        bucket="b",
        product="p",
        cadence_minutes=10,
        bbox=(80.0, 30.0, 146.0, 55.0),
        gates=gates,
    )


def test_product_fields_paints_cf_gated_and_leaves_cf_comb_alone():
    debra = debra_output()
    before = debra["cf_comb"].values.copy()
    out, painted = product_fields(debra, scene(), region())
    assert painted == "cf_gated"
    assert out["cf_gated"].values[0, 0] == 0.0  # DT3 only
    assert out["cf_gated"].values[0, 1] == 0.0  # bright
    assert out["cf_gated"].values[1, 0] == 0.5  # corroborated and dark
    assert np.isnan(out["cf_gated"].values[1, 1])
    np.testing.assert_array_equal(out["cf_comb"].values, before)


def test_product_fields_carries_the_raw_branch_sums():
    out, _ = product_fields(debra_output(), scene(), region())
    # Eq. 16 at (1,0): (dt1 + dt2 + dt3) * (1 - cm_norm_day) = 0.4 * 0.5
    assert out["cf_day_raw"].values[1, 0] == pytest.approx(0.2)
    assert {"cf_day_raw", "cf_trm_raw", "cf_ngt_raw"} <= set(out.data_vars)


def test_product_fields_without_gates_paints_cf_comb():
    out, painted = product_fields(debra_output(), scene(), region(gates=False))
    assert painted == "cf_comb"
    assert "cf_gated" not in out
    assert "cf_day_raw" in out


def test_every_product_field_is_published():
    out, _ = product_fields(debra_output(), scene(), region())
    for name in ("cf_comb", "cf_gated", "cf_day_raw", "cf_trm_raw", "cf_ngt_raw"):
        assert name in NETCDF_VARS and name in out


# --- CAMEL staging --------------------------------------------------------


def emissivity(descending=True):
    lat = np.arange(60, 20, -0.05) if descending else np.arange(20, 60, 0.05)
    lon = np.arange(-130, -80, 0.05)
    values = np.ones((lat.size, lon.size), dtype="float32")
    return xr.Dataset(
        {"emis_tir_104": (("latitude", "longitude"), values)},
        coords={"latitude": lat, "longitude": lon},
    )


@pytest.mark.parametrize("descending", [True, False])
def test_the_subset_covers_the_bbox_plus_its_margin(descending):
    bbox = (-115.0, 25.0, -95.0, 40.0)
    cropped = subset(emissivity(descending), bbox)
    assert cropped["latitude"].min() <= bbox[1] - MARGIN_DEG + 0.05
    assert cropped["latitude"].max() >= bbox[3] + MARGIN_DEG - 0.05
    assert cropped["longitude"].min() <= bbox[0] - MARGIN_DEG + 0.05
    assert cropped["longitude"].max() >= bbox[2] + MARGIN_DEG - 0.05
    assert cropped.sizes["latitude"] < emissivity().sizes["latitude"]
