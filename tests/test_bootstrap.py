"""The CAMEL staging step: which month it says it served."""

import datetime as dt
from pathlib import Path

from shachen_ops import bootstrap


def test_granule_month_reads_the_year_off_the_filename():
    assert bootstrap.granule_month(Path("x/CAM5K30EM_202309.nc")) == dt.date(2023, 9, 1)


def test_granule_month_is_none_for_other_names():
    assert bootstrap.granule_month(Path("emissivity.nc")) is None


def test_resolve_month_records_the_month_shachen_actually_served(monkeypatch, tmp_path):
    # shachen >= 0.3.0 falls back across years itself and returns the earlier
    # granule with a warning; the source month must come from that file, not
    # from the month that was asked for.
    served = tmp_path / "CAM5K30EM_202309.nc"

    def fetch(month, out_dir):
        assert month == dt.date(2026, 9, 1)
        return served

    monkeypatch.setattr("shachen.io.emissivity.fetch_emissivity", fetch)
    path, source_month = bootstrap.resolve_month(dt.date(2026, 9, 14), tmp_path)
    assert path == served
    assert source_month == dt.date(2023, 9, 1)


def test_resolve_month_walks_back_when_shachen_raises(monkeypatch, tmp_path):
    def fetch(month, out_dir):
        if month.year == 2026:
            raise RuntimeError("no granule")
        return tmp_path / f"CAM5K30EM_{month:%Y%m}.nc"

    monkeypatch.setattr("shachen.io.emissivity.fetch_emissivity", fetch)
    _, source_month = bootstrap.resolve_month(dt.date(2026, 9, 1), tmp_path)
    assert source_month == dt.date(2025, 9, 1)
