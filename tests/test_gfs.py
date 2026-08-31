"""GFS cycle selection and GRIB index arithmetic."""

import datetime as dt

import pytest

from shachen_ops.gfs import candidate_sources, record_range, source_from_path

INDEX = """1:0:d=2026083100:PRMSL:mean sea level:anl:
562:407000000:d=2026083100:HGT:surface:anl:
563:408215400:d=2026083100:TMP:surface:anl:
564:408791000:d=2026083100:WEASD:surface:anl:
"""


def test_candidates_are_freshest_analysis_first():
    when = dt.datetime(2026, 8, 31, 13, 16, 18, tzinfo=dt.UTC)
    got = [(cycle.strftime("%H"), hour) for cycle, hour in candidate_sources(when)]
    assert got[:3] == [("12", 1), ("06", 7), ("00", 13)]


def test_a_scan_past_the_half_hour_rounds_up_to_the_next_hour():
    when = dt.datetime(2026, 8, 31, 13, 46, tzinfo=dt.UTC)
    cycle, hour = candidate_sources(when)[0]
    assert cycle + dt.timedelta(hours=hour) == dt.datetime(2026, 8, 31, 14, tzinfo=dt.UTC)


def test_candidates_stop_before_the_forecast_gets_stale():
    when = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)
    assert max(hour for _, hour in candidate_sources(when)) <= 24


def test_record_range_spans_to_the_next_record():
    assert record_range(INDEX) == (408215400, 408790999)


def test_the_last_record_runs_to_the_end_of_the_file():
    assert record_range(INDEX, ":WEASD:surface:") == (408791000, None)


def test_a_missing_record_raises():
    with pytest.raises(LookupError, match="TSOIL"):
        record_range(INDEX, ":TSOIL:surface:")


def test_a_cached_record_names_its_own_provenance():
    source = source_from_path("data/gfs/gfs_ts_2026083106_f007.grib2")
    assert source.cycle == dt.datetime(2026, 8, 31, 6, tzinfo=dt.UTC)
    assert source.forecast_hour == 7
    assert source.as_metadata()["valid_time"] == "2026-08-31T13:00:00Z"
