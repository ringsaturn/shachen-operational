"""The serialization edges: what the published netCDF keeps, and in what type."""

import datetime as dt

import numpy as np
import pytest
import xarray as xr

from shachen_ops.bootstrap import MARGIN_DEG, subset
from shachen_ops.process import NETCDF_VARS, write_netcdf


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
