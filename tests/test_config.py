"""Region table validation — a typo in regions.toml must fail at load."""

import pytest

from shachen_ops.config import load_regions

VALID = """
[swus]
satellite = "GOES-19"
sensor = "abi"
bucket = "noaa-goes19"
product = "ABI-L1b-RadC"
cadence_minutes = 5
publish_every_minutes = 30
bbox = [-115.0, 25.0, -95.0, 40.0]
"""


def write(tmp_path, text):
    path = tmp_path / "regions.toml"
    path.write_text(text)
    return path


def test_valid_region_round_trips(tmp_path):
    region = load_regions(write(tmp_path, VALID))["swus"]
    assert region.reader == "abi_l1b"
    assert region.channels[0] == "C02"
    assert region.publish_every_minutes == 30
    assert region.constants is not None


def test_publish_cadence_defaults_to_the_scan_cadence(tmp_path):
    text = VALID.replace("publish_every_minutes = 30\n", "")
    assert load_regions(write(tmp_path, text))["swus"].publish_every_minutes == 5


@pytest.mark.parametrize(
    "edit, message",
    [
        ('sensor = "abi"', "missing"),
        ('sensor = "seviri"', "unknown sensor"),
        ("publish_every_minutes = 7", "not a multiple"),
        ("bbox = [-95.0, 25.0, -115.0, 40.0]", "not increasing"),
    ],
)
def test_invalid_regions_are_rejected(tmp_path, edit, message):
    if edit == 'sensor = "abi"':
        text = VALID.replace('sensor = "abi"\n', "")
    elif edit.startswith("sensor"):
        text = VALID.replace('sensor = "abi"', edit)
    elif edit.startswith("publish"):
        text = VALID.replace("publish_every_minutes = 30", edit)
    else:
        text = VALID.replace("bbox = [-115.0, 25.0, -95.0, 40.0]", edit)
    with pytest.raises(ValueError, match=message):
        load_regions(write(tmp_path, text))
