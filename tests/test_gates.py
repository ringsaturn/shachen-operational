"""CF_gated: the two product-side gates and the field they produce.

No ground truth here -- the costs live in the research repository (labs/010-rolling-background/06 and
07. What is testable is that each gate zeroes exactly what it says and
nothing else, that NaN stays NaN, that the visible gate is a daytime gate on
the cos-normalized reflectance, and -- the reason the module is here rather
than in shachen -- that asking for the field leaves DEBRA's own output alone.
"""

import numpy as np
import pytest
import xarray as xr

from shachen_ops.gates import (
    VIS_CLOUD_REFL_PCT,
    VIS_ZENITH_MAX_DEG,
    from_debra,
    normalized_reflectance,
    split_window_gate,
    visible_cloud_gate,
)


def _field(values):
    return xr.DataArray(np.asarray(values, dtype=float), dims=("y", "x"))


def test_split_window_gate_zeroes_only_where_both_tests_are_silent():
    cf = np.array([[0.5, 0.5], [0.5, np.nan]])
    dt1 = np.array([[0.0, 0.2], [0.0, 0.0]])
    dt2 = np.array([[0.0, 0.0], [0.1, 0.0]])
    out = split_window_gate(cf, dt1, dt2)
    assert out[0, 0] == 0.0  # neither fired
    assert out[0, 1] == 0.5  # dt1 fired
    assert out[1, 0] == 0.5  # dt2 fired
    assert np.isnan(out[1, 1])  # NaN confidence stays NaN


def test_split_window_gate_treats_a_nan_test_as_silent():
    out = split_window_gate(np.array([0.4, 0.4]), np.array([np.nan, np.nan]), np.array([0.0, 0.3]))
    assert out.tolist() == [0.0, 0.4]


def test_split_window_gate_keeps_xarray_labels():
    cf = _field([[0.3, 0.3]])
    out = split_window_gate(cf, _field([[0.0, 0.1]]), _field([[0.0, 0.0]]))
    assert isinstance(out, xr.DataArray)
    assert out.dims == ("y", "x")
    assert out.values.tolist() == [[0.0, 0.3]]


def test_normalized_reflectance_divides_by_cos_zenith_and_stops_at_the_cap():
    refl = np.array([20.0, 20.0, 20.0, np.nan])
    zen = np.array([0.0, 60.0, VIS_ZENITH_MAX_DEG + 1.0, 30.0])
    out = normalized_reflectance(refl, zen)
    assert out[0] == pytest.approx(20.0)
    assert out[1] == pytest.approx(40.0)
    assert np.isnan(out[2])  # past the zenith cap
    assert np.isnan(out[3])  # no reflectance


def test_visible_cloud_gate_is_a_daytime_gate_on_the_normalized_reflectance():
    cf = np.full(5, 0.6)
    # raw 35% at 60 deg normalizes to 70%: cloud; raw 25% at 60 deg is 50%: not
    # at the default threshold; bright but past the zenith cap passes; NaN
    # reflectance passes; a bright pixel at noon is gated.
    refl = np.array([35.0, 25.0, 80.0, np.nan, VIS_CLOUD_REFL_PCT])
    zen = np.array([60.0, 60.0, 95.0, 30.0, 0.0])
    out = visible_cloud_gate(cf, refl, zen)
    assert out.tolist() == [0.0, 0.6, 0.6, 0.6, 0.0]


def test_visible_cloud_gate_threshold_and_cap_are_parameters():
    cf = np.array([0.6, 0.6])
    refl = np.array([30.0, 30.0])
    zen = np.array([0.0, 85.0])
    assert visible_cloud_gate(cf, refl, zen, refl_max_pct=30.0).tolist() == [0.0, 0.6]
    assert visible_cloud_gate(cf, refl, zen, refl_max_pct=30.0, zenith_max_deg=89.0).tolist() == [
        0.0,
        0.0,
    ]


def _debra():
    return xr.Dataset(
        {
            "cf_comb": _field([[0.5, 0.5], [0.5, np.nan]]),
            "dt1": _field([[0.0, 0.3], [0.0, 0.0]]),
            "dt2": _field([[0.0, 0.0], [0.0, 0.0]]),
            "zenith_deg": _field([[30.0, 30.0], [30.0, 30.0]]),
        }
    )


def _scene(refl):
    return xr.Dataset({"refl_vis_064": _field(refl)})


def test_from_debra_applies_both_gates_and_leaves_cf_comb_alone():
    debra = _debra()
    before = debra["cf_comb"].values.copy()
    # (0,0): DT3-only -> split-window gate; (0,1): dt1 fired but bright -> visible gate;
    # (1,0): DT3-only and dark -> split-window gate; (1,1): NaN stays NaN.
    out = from_debra(debra, _scene([[10.0, 80.0], [10.0, 10.0]]))
    assert out.name == "cf_gated"
    assert out.dims == ("y", "x")
    assert out.values[0, 0] == 0.0
    assert out.values[0, 1] == 0.0
    assert out.values[1, 0] == 0.0
    assert np.isnan(out.values[1, 1])
    np.testing.assert_array_equal(debra["cf_comb"].values, before)
    assert "cf_gated" not in debra


def test_from_debra_split_window_only_needs_no_scene():
    out = from_debra(_debra(), visible=False)
    assert out.values[0, 1] == 0.5  # dt1 fired, nothing else asked
    assert out.values[0, 0] == 0.0


def test_from_debra_refuses_a_visible_gate_without_reflectance():
    with pytest.raises(ValueError, match="refl_vis_064"):
        from_debra(_debra())
    with pytest.raises(ValueError, match="refl_vis_064"):
        from_debra(_debra(), xr.Dataset({"bt_tir_104": _field([[280.0, 280.0], [280.0, 280.0]])}))


def test_from_debra_refuses_a_reflectance_on_another_grid():
    with pytest.raises(ValueError, match="shape mismatch"):
        from_debra(_debra(), _scene([[10.0, 10.0, 10.0]]))


def test_from_debra_is_identity_where_nothing_is_gated():
    debra = _debra()
    debra["dt1"] = _field([[0.2, 0.2], [0.2, 0.2]])
    out = from_debra(debra, _scene([[10.0, 10.0], [10.0, 10.0]]))
    np.testing.assert_array_equal(out.values, debra["cf_comb"].values)
