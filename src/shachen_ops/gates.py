"""CF_gated: CF_comb with the cloud failure refer/008 found gated out, two ways.

Vendored from the private research repository (``debra_lab/gates.py``), which
is where these gates were measured and remains their source of truth: change
it there, with its tests, and copy the result down. The only permitted
difference is this paragraph. It is vendored rather than imported because the
gates are a product-layer bias fitted on that repository's ground truth, and
`shachen` ships the published algorithm only.

This is **this repo's** product layer, like :mod:`debra_lab.calibration`: it
reads DEBRA's output and produces a second field, and ``cf_comb`` is left
exactly as `shachen` computed it. Neither gate is part of DEBRA (Eqs. 1-29),
and neither belongs in `shachen`'s defaults -- the first changes the meaning
of Eq. 16 from a sum to a sum with a corroboration requirement, the second
adds a test the published cloud mask does not have. Both were measured on
this repo's ground truth (plans/refer/008-bad-cases.md, S4 and S9;
labs/010-rolling-background/06 and 07), and that is where their numbers live.

The failure is one failure seen from two sides. A mid-level cloud is 15-22 K
colder than the NWP skin temperature: CM1 (Eq. 1) does not start masking
until ~22.5 K, CM2 wants deep convection and CM3/CM4 want cirrus, so the
cloud mask scores it clear and Eq. 15's DT3 turns that coldness into a
confidence of ~0.6 with no split-window signal at all (``dt1 = dt2 = 0``).
On the two 2026-09-08 ``gobi`` scans this painted 5.5% / 2.1% of the domain
yellow on a day with no dust; on 42 dust days it is 34.9% of the daytime
false alarms at cf 0.1 and 31.2% of the night ones.

- :func:`split_window_gate` (refer/008 S4): no colour where neither
  split-window test responded. Cloud has no ``dt1``; dust nearly always does.
  Works around the clock, and at night it is the only gate there is (the
  DT3-only boxes lit at night on 42 days are 4,417 false alarms to 193 hits).
  Cost on 42 days: POD@0.1 -0.035 by day / -0.012 by night for FAR@0.1
  0.42 -> 0.23 / 0.58 -> 0.38; at 0.2 nothing moves, because DT3 alone
  cannot reach 0.2 on the ABI interval. It removes, it never reorders: the
  equal-FAR POD does not improve.
- :func:`visible_cloud_gate` (refer/008 S9, day half): no colour where the
  0.64 um reflectance, divided by cos(solar zenith), says cloud. Dividing by
  the cosine is what lets one threshold mean the same cloud at 07:00 and at
  noon: the two refer/008 scans sit at 70-76 deg, where a raw 35% is beyond
  anything on the ground and the raw gate removes 1-11% of the yellow. The
  DT3-only false alarms have a normalized median of 70% (p10 48%) against
  31-34% for hits (p90 44-60%): dust is bright too, so the threshold has to
  sit above the dust and below the cloud. On 42 days the 60% gate costs
  POD@0.1 0.007 for FAR@0.1 0.42 -> 0.28 and raises the AUC 0.739 -> 0.749;
  50% costs 0.021 for 0.42 -> 0.24; 40% eats dust (POD -0.083).

What they cannot promise: these are event-internal numbers (base rate ~6.7%);
on a random dust-season day the false-alarm ratio is higher for every
configuration (CLAUDE.md, "FAR 有两种口径"). And a gate is a gate: dust so
thin that only DT3 saw it, or dust brighter than the threshold (a thick plume
over the Taklamakan at noon can be), is gone with the cloud. Sun glint over
water is bright and is gated too.
"""

import numpy as np
import xarray as xr

#: cos-zenith-normalized 0.64 um reflectance (percent, i.e. what
#: ``shachen.io.satellite.load_scene`` delivers divided by cos(solar zenith))
#: at or above which a pixel is treated as cloud. Dark land normalizes to
#: ~28% (42-day median of unlit clean boxes), lit dust to 31-40%, the
#: false-yellow cloud to 70%.
VIS_CLOUD_REFL_PCT: float = 60.0
#: Solar zenith beyond which the reflectance is not trusted as a cloud test:
#: past this cos(zenith) is under 0.17 and the normalization amplifies noise
#: and terrain shadow more than it corrects geometry.
VIS_ZENITH_MAX_DEG: float = 80.0


def split_window_gate(cf, dt1, dt2):
    """``cf`` with pixels where neither split-window test fired set to 0.

    ``dt1`` and ``dt2`` are Eqs. 13-14 as DEBRA produced them. A pixel is
    gated when both are ``<= 0``; NaN ``cf`` stays NaN, and a NaN test counts
    as "did not fire". Array-generic.
    """
    d1 = np.nan_to_num(np.asarray(dt1, dtype=float), nan=0.0)
    d2 = np.nan_to_num(np.asarray(dt2, dtype=float), nan=0.0)
    return _zero_where(cf, (d1 <= 0.0) & (d2 <= 0.0))


def normalized_reflectance(
    refl_vis_064_pct, zenith_deg, zenith_max_deg: float = VIS_ZENITH_MAX_DEG
):
    """``refl / cos(zenith)`` in percent, NaN where the sun is past ``zenith_max_deg``."""
    refl = np.asarray(refl_vis_064_pct, dtype=float)
    zen = np.asarray(zenith_deg, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        norm = refl / np.cos(np.radians(zen))
    return np.where(np.isfinite(norm) & (zen <= zenith_max_deg), norm, np.nan)


def visible_cloud_gate(
    cf,
    refl_vis_064_pct,
    zenith_deg,
    refl_max_pct: float = VIS_CLOUD_REFL_PCT,
    zenith_max_deg: float = VIS_ZENITH_MAX_DEG,
):
    """``cf`` with bright, sunlit pixels set to 0.

    A pixel is gated when its cos-normalized reflectance
    (:func:`normalized_reflectance`) is finite and at least ``refl_max_pct``,
    which already requires the solar zenith to be at most ``zenith_max_deg``.
    Night and terminator pixels, and pixels with no reflectance, pass
    through untouched. Array-generic.
    """
    norm = normalized_reflectance(refl_vis_064_pct, zenith_deg, zenith_max_deg)
    return _zero_where(cf, np.isfinite(norm) & (norm >= refl_max_pct))


def from_debra(
    debra: xr.Dataset,
    scene: xr.Dataset | None = None,
    *,
    split_window: bool = True,
    visible: bool = True,
) -> xr.DataArray:
    """``cf_gated`` for a :func:`shachen.pipeline.run_debra` result.

    Applies :func:`split_window_gate` (from ``dt1``/``dt2``) and, when
    ``scene`` is given, :func:`visible_cloud_gate` (from the scene's
    ``refl_vis_064`` and DEBRA's ``zenith_deg``) to ``cf_comb``. Asking for
    the visible gate without a scene, or with a scene that has no
    ``refl_vis_064``, is a ValueError rather than a silent split-window-only
    product. ``debra`` itself is not modified.
    """
    cf = debra["cf_comb"]
    if split_window:
        cf = split_window_gate(cf, debra["dt1"], debra["dt2"])
    if visible:
        if scene is None or "refl_vis_064" not in scene:
            raise ValueError("the visible cloud gate needs a scene carrying refl_vis_064")
        refl = scene["refl_vis_064"]
        if tuple(refl.shape) != tuple(cf.shape):
            raise ValueError(
                f"shape mismatch: refl_vis_064 {tuple(refl.shape)} vs cf_comb {tuple(cf.shape)}"
            )
        cf = visible_cloud_gate(cf, refl.values, debra["zenith_deg"].values)
    out = xr.DataArray(np.asarray(cf, dtype=float), dims=debra["cf_comb"].dims, name="cf_gated")
    out.attrs = {
        "long_name": "DEBRA combined dust confidence factor, cloud-gated",
        "units": "1",
    }
    return out


def _zero_where(cf, condition):
    """``cf`` with 0.0 where ``condition`` holds; NaN in ``cf`` stays NaN."""
    condition = np.asarray(condition, dtype=bool)
    if isinstance(cf, xr.DataArray):
        keep = xr.DataArray(~condition, dims=cf.dims) | cf.isnull()
        return cf.where(keep, 0.0)
    cf = np.asarray(cf, dtype=float)
    return np.where(condition & np.isfinite(cf), 0.0, cf)
