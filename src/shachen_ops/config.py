"""Region table and the per-sensor dispatch every stage keys off.

One :class:`Region` is the unit of work: it names an AWS Open Data bucket and
L1b product, the satpy reader for it, the lat/lon crop, and the DEBRA constant
set to run with. Regions live in ``regions.toml`` so adding one is a config
change; everything else in the package takes a ``Region`` and never hardcodes
a bucket, a band list, or a bbox.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from shachen.constants import ABI_TUNED, DEFAULTS, DebraConstants


def _default_regions_toml() -> Path:
    """The region table shipped beside the source, or the one in the cwd.

    An editable install resolves to the checkout's own file; a wheel install
    has no repository above it, and there the working directory is the only
    sensible place to look.
    """
    beside_source = Path(__file__).resolve().parents[2] / "regions.toml"
    return beside_source if beside_source.exists() else Path("regions.toml")


#: Default region table.
REGIONS_TOML = _default_regions_toml()

#: The 7 DEBRA roles plus the 11.2 um band the classic Dust RGB needs for its
#: green gun, in ``shachen.constants.ABI_BANDS`` order.
ABI_CHANNELS = ("C02", "C05", "C07", "C08", "C11", "C13", "C14", "C15")
AHI_CHANNELS = ("B03", "B05", "B07", "B08", "B11", "B13", "B14", "B15")

#: Per-sensor satpy reader, channel tuple, and the on-disk L1b glob.
SENSOR_READERS: dict[str, str] = {"abi": "abi_l1b", "ahi": "ahi_hsd"}
SENSOR_CHANNELS: dict[str, tuple[str, ...]] = {"abi": ABI_CHANNELS, "ahi": AHI_CHANNELS}
SENSOR_GLOBS: dict[str, str] = {"abi": "*.nc", "ahi": "*.DAT*"}

#: Constant sets selectable per region: ``abi`` is shachen's ABI retune,
#: ``paper`` the published Miller et al. 2017/2020 values.
TUNING: dict[str, DebraConstants] = {"abi": ABI_TUNED, "paper": DEFAULTS}

#: The bucket is shared, so everything this pipeline writes — published scans
#: and staged ancillary alike — lives under one root prefix rather than at the
#: top of the bucket.
ROOT_PREFIX = "shachen-ops"

#: Output layout version, the path element under :data:`ROOT_PREFIX` that every
#: published key carries.
LAYOUT_VERSION = "v1"


@dataclass(frozen=True)
class Region:
    """One operational domain: where its L1b lives and how DEBRA runs on it."""

    key: str
    satellite: str
    sensor: str
    bucket: str
    product: str
    cadence_minutes: int
    bbox: tuple[float, float, float, float]
    publish_every_minutes: int = 30
    tuning: str = "abi"
    description: str = ""

    @property
    def reader(self) -> str:
        """satpy reader for this region's sensor."""
        return SENSOR_READERS[self.sensor]

    @property
    def channels(self) -> tuple[str, ...]:
        """L1b channel names to fetch for this region's sensor."""
        return SENSOR_CHANNELS[self.sensor]

    @property
    def constants(self) -> DebraConstants:
        """The DEBRA constant set named by ``tuning``."""
        return TUNING[self.tuning]


def _region(key: str, entry: dict) -> Region:
    """Build one Region from its TOML table, validating what code assumes."""
    missing = {"satellite", "sensor", "bucket", "product", "cadence_minutes", "bbox"} - set(entry)
    if missing:
        raise ValueError(f"region {key!r} is missing {sorted(missing)}")
    if entry["sensor"] not in SENSOR_READERS:
        raise ValueError(
            f"region {key!r} has unknown sensor {entry['sensor']!r} "
            f"(choose from {sorted(SENSOR_READERS)})"
        )
    tuning = entry.get("tuning", "abi")
    if tuning not in TUNING:
        raise ValueError(
            f"region {key!r} has unknown tuning {tuning!r} (choose from {sorted(TUNING)})"
        )
    bbox = tuple(float(v) for v in entry["bbox"])
    if len(bbox) != 4:
        raise ValueError(f"region {key!r} bbox must be (lon_min, lat_min, lon_max, lat_max)")
    lon_min, lat_min, lon_max, lat_max = bbox
    every = int(entry.get("publish_every_minutes", entry["cadence_minutes"]))
    if every % int(entry["cadence_minutes"]):
        raise ValueError(
            f"region {key!r} publishes every {every} min, which is not a multiple "
            f"of its {entry['cadence_minutes']}-min scan cadence"
        )
    if not (lon_min < lon_max and lat_min < lat_max):
        # An antimeridian-crossing domain would also land here; the GFS regrid
        # works in -180..180, so that is a real restriction, not an oversight.
        raise ValueError(f"region {key!r} bbox is not increasing in both axes: {bbox}")
    return Region(
        key=key,
        satellite=entry["satellite"],
        sensor=entry["sensor"],
        bucket=entry["bucket"],
        product=entry["product"],
        cadence_minutes=int(entry["cadence_minutes"]),
        bbox=bbox,
        publish_every_minutes=int(entry.get("publish_every_minutes", entry["cadence_minutes"])),
        tuning=tuning,
        description=entry.get("description", ""),
    )


def load_regions(path: Path | str = REGIONS_TOML) -> dict[str, Region]:
    """Parse the region table; every entry is validated, so a typo fails here."""
    with open(path, "rb") as handle:
        table = tomllib.load(handle)
    return {key: _region(key, entry) for key, entry in table.items()}


def get_region(key: str, path: Path | str = REGIONS_TOML) -> Region:
    """One region by key; unknown keys raise KeyError naming what exists."""
    regions = load_regions(path)
    if key not in regions:
        raise KeyError(f"unknown region {key!r} (known: {', '.join(sorted(regions))})")
    return regions[key]
