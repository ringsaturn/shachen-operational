# shachen-operational

An unattended pipeline that runs [shachen](https://github.com/ringsaturn/shachen)'s
DEBRA-Dust algorithm on live GOES ABI and Himawari AHI data from the AWS Open
Data buckets and publishes the results — imagery, fields, and manifests — to
Cloudflare R2. All compute runs on GitHub Actions.

Each published scan carries two pictures and one data file: the DEBRA enhanced
composite, in which dust is coloured by detection confidence over an
unmodified background; the classic EUMETSAT Dust RGB over the same scene, as
the baseline to compare it against; and a netCDF of the confidence field and
the tests behind it.

## Regions

| Key | Satellite | Domain | Published |
|---|---|---|---|
| `swus` | GOES-19 (East), `ABI-L1b-RadC` | 115–95 °W, 25–40 °N | every 30 min |
| `gobi` | Himawari-9, `AHI-L1b-FLDK` | 80–135 °E, 30–55 °N | every 30 min |

Regions live in [`regions.toml`](regions.toml); adding one is a config change.
`publish_every_minutes` is deliberately coarser than the sensor cadence — ABI
scans CONUS every five minutes, and publishing all of it would be 288 runs a
day for a product nobody reads that fast.

The `gobi` domain reaches the western limb of the Himawari disk, so its
north-western corner has no data. That is the geometry of a satellite parked
at 140.7 °E, not a fetch problem.

## Output layout

Everything is under a single bucket, keyed by region and scan start time:

```
shachen-ops/v1/{region}/{YYYY}/{MM}/{DD}/{HHMMSS}/debra.png      DEBRA enhanced imagery
                                                  dustrgb.png    Dust RGB baseline
                                                  debra.nc       confidence + dust tests
                                                  meta.json      what this scan is
shachen-ops/v1/{region}/latest.json                              pointer + recent scans
shachen-ops/v1/{region}/index/{YYYY}/{MM}.json                   month listing
```

`latest.json` is the entry point. It carries `latest` — the full `meta.json`
body of the newest published scan — `recent`, the last twelve scans as
`{scan_start, prefix, assets}`, and `retention`, which says how long each kind
of object survives. Imagery is immutable and cached forever; `latest.json` is
cached for a minute, the month index for five, and `meta.json` for an hour —
it is the one scan object that changes, because retention takes a scan's
netCDF away partway through its life and the manifest has to stop advertising
it.

Write order is what makes a rerun safe: scan objects, then `meta.json` (which
is what marks a scan published), then the month index, then `latest.json`.
A reader following the pointer never arrives at a half-written scan, and a job
that dies mid-publish leaves a scan the next backfill simply redoes.

## Retention

The bucket has a 2 GB budget and the pipeline writes about 1.7 GB a day, so
nothing is kept indefinitely. `debra.nc` is four fifths of the bytes — 7 MB
for `swus`, 20 MB for `gobi`, against 4 MB of imagery — so it gets a much
shorter life than the pictures:

| Age | What survives |
|---|---|
| newest 12 scans (6 h) | everything, `debra.nc` included |
| back to 144 scans (3 days) | `debra.png`, `dustrgb.png`, `meta.json` |
| older | nothing; the scan is deleted |

That settles near 1.5 GB across both regions. Both numbers are per region in
`regions.toml` and are counts of scans rather than durations: an outage must
not let the bucket blow past its ceiling once the pipeline catches up, and a
count bounds the bytes whatever the clock says. Staged CAMEL months are kept
two deep, enough that a backfill reaching across a month boundary still finds
the month it needs.

Pruning is part of the scheduled job — `run` and `backfill` both do it, after
publishing and whether or not they published anything, since the run that
finds nothing to do is exactly the one after which the bucket is still over
budget. It is a step in the pipeline rather than an R2 lifecycle rule because
the manifests have to stay honest: a lifecycle expiry would delete objects
that `latest.json` and the month index still point at. `shachen-ops prune
--region <key> --dry-run` reports what would go without touching anything.

The listing is the source of truth, not the manifests — one `list_objects`
over the region prefix says what actually exists — so a prune is idempotent
and a half-finished publish cannot desynchronise the policy from the bucket.

Every `meta.json` records the inputs, not just the output — which GFS cycle
and forecast hour supplied the skin temperature, which CAMEL month supplied
the emissivity, which shachen version and constant set ran. Outputs stay
traceable to the fields that made them.

## How a scan is produced

```
resolve scan      newest scan on S3 with every band present
fetch L1b         anonymous S3; for AHI only the strips the domain needs
fetch ancillary   GFS skin temperature (one ranged GRIB read), CAMEL from R2
load_scene        satpy -> bt_*/refl_* on the 2-km grid, cropped to the bbox
run_debra         confidence field and intermediates
run_dust_rgb      baseline composite
render            georeferenced PNGs with coastlines and a graticule
write netCDF      compressed, single precision
publish           objects, meta.json, month index, latest.json
prune             enforce retention; fix up the manifests behind it
```

### Ancillary data

DEBRA needs a skin temperature field and a background. Neither is available
in the form shachen was built against, and both substitutions are recorded per
scan:

**Skin temperature.** MERRA-2 TS lands two to four weeks late, so it cannot
drive a near-real-time pipeline. GFS `TMP:surface` from `noaa-gfs-bdp-pds`
stands in: the same quantity from a model that publishes about four hours
behind its cycle. Only one GRIB2 record is downloaded — each 0.25° file is
around half a gigabyte and ships a `.idx` sidecar, so the fetch reads the
index and pulls that one record with a ranged GET, about a megabyte.

DEBRA's background is calibrated against MERRA-2, and GFS will not agree with
it everywhere, over desert at night least of all. Quantifying that is future
work; in the meantime every scan says which field it used.

**Emissivity.** The background here is the semianalytic one — CAMEL
emissivity times a Planck curve at the skin temperature — because it carries
no state between scans. CAMEL is a monthly climatology behind Earthdata
authentication, so it is staged once per region and month into a private R2
prefix by the `bootstrap-ancillary` workflow; the operational path holds no
Earthdata credentials.

`CAM5K30EM` V003 ends in December 2023, so an operational month has no granule
of its own. Since the product *is* a climatology, the substitution is the same
calendar month from the most recent year that has one, and the year actually
used is in each scan's `meta.json` as `emissivity.source_month`.

## Running it

```console
$ uv sync
$ uv run shachen-ops regions
$ uv run shachen-ops bootstrap-camel --region swus --dest ./localbucket
$ uv run shachen-ops run --region swus --dest ./localbucket
$ uv run shachen-ops backfill --region swus --since 2h --dest ./localbucket
$ uv run shachen-ops scans --region swus --since 2h --dest ./localbucket
$ uv run shachen-ops prune --region swus --dest ./localbucket --dry-run
```

`--dest` is a directory to publish into; omit it (or pass `r2`) to publish to
R2, which needs `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`
in the environment, and optionally `R2_BUCKET` (default `shachen`). Local runs
write the same layout a real publish would, so they exercise the same code.

`backfill` is what the schedule runs. It asks the bucket which scans in the
window are missing and processes those, newest first — GitHub crons fire late
and sometimes not at all, and a missed run heals itself on the next one
instead of leaving a hole. Scans younger than three minutes are left alone,
since an AHI timeline's directory appears before every strip is in it. It
prunes when it is done; `--no-prune` skips that, and the standalone `prune`
subcommand does it on its own.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `ci` | push, PR | ruff, pytest, region table parses |
| `process` | every 30 min, manual | backfill each region, then prune it, one job per region |
| `bootstrap-ancillary` | manual | stage a region-month of CAMEL into R2 |

`process` builds its matrix from `regions.toml`, so a new region needs no
workflow edit. Each region's job takes a concurrency group of its own, with
`cancel-in-progress: false`, so overlapping runs queue rather than fight over
that region's manifests.

Secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` for
everything that publishes; `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` only
in the `earthdata` environment that `bootstrap-ancillary` runs in.

## Costs, roughly

Measured on one scan of each region, on a laptop:

| | `swus` (ABI) | `gobi` (AHI) |
|---|---|---|
| L1b downloaded | 95 MB | 77 MB (3 of 10 strips) |
| Wall time, cold | ~3 min | ~2.5 min |
| Wall time, inputs cached | ~5 s | ~10 s |
| Peak RSS | 2.9 GB | 2.6 GB |
| Published per scan | ~11 MB | ~23 MB |
| of which `debra.nc` | 7 MB | 20 MB |

So a run is dominated by fetching, not by DEBRA, and fits a 4-vCPU / 16 GB
runner with room to spare. At 48 scans a day across both regions that is about
1.7 GB a day of new objects, against a 2 GB bucket — which is what the
retention above exists to reconcile, and why the netCDF is the first thing to
go. Steady state is near 1.5 GB, so R2 storage stays inside a dollar a month
and the writes (six per scan, ~17k a month) stay inside the free tier.

## Not here yet

- The composite background (a rolling 14-day cloud-cleared TIR store), which
  carries the split-window water-vapour depression the semianalytic
  background does not.
- A reanalysis pass that reprocesses older scans with real MERRA-2 TS, to put
  a number on the GFS substitution.
- Any frontend. The layout above is meant to make one easy.
