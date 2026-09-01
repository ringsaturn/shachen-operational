# shachen-operational

An unattended pipeline that runs [shachen](https://github.com/ringsaturn/shachen)'s
DEBRA-Dust algorithm on live GOES ABI and Himawari AHI data from the AWS Open
Data buckets and publishes the results (imagery, fields, and manifests) to
Cloudflare R2. All compute runs on GitHub Actions.

Each published scan carries two images and one data file: the DEBRA enhanced
composite, in which dust is coloured by detection confidence over an
unmodified background; the EUMETSAT Dust RGB over the same scene, as the
baseline for comparison; and a netCDF of the confidence field and the dust
tests it is derived from.

## Regions

| Key | Satellite | Domain | Published |
|---|---|---|---|
| `swus` | GOES-19 (East), `ABI-L1b-RadC` | 115–95 °W, 25–40 °N | every 30 min |
| `gobi` | Himawari-9, `AHI-L1b-FLDK` | 80–135 °E, 30–55 °N | every 30 min |

Regions live in [`regions.toml`](regions.toml); adding one is a config change.
`publish_every_minutes` is deliberately coarser than the sensor cadence: ABI
scans CONUS every five minutes, and publishing all of it would be 288 runs a
day per region.

The `gobi` domain reaches the western limb of the Himawari disk, so its
north-western corner has no data. Himawari-9 is at 140.7 °E, and that corner
falls outside the sensor's view.

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

`latest.json` is the entry point. It carries `latest`, the full `meta.json`
body of the newest published scan; `recent`, the last twelve scans as
`{scan_start, prefix, assets}`; and `retention`, the retained lifetime of each
kind of object. Imagery is immutable and cached indefinitely; `latest.json` is
cached for a minute and the month index for five. `meta.json` is cached for an
hour because it is the one scan object that changes: retention deletes a
scan's netCDF partway through the scan's retained period, and the manifest
must stop listing it.

Write order is what makes a rerun safe: scan objects, then `meta.json` (which
marks a scan published), then the month index, then `latest.json`. A reader
following the pointer never arrives at a half-written scan, and a job that
fails mid-publish leaves a scan that the next backfill redoes.

## Retention

The bucket has a 2 GB budget and the pipeline writes about 1.7 GB a day, so
nothing is kept indefinitely. `debra.nc` is four fifths of the bytes (7 MB
for `swus`, 20 MB for `gobi`, against 4 MB of imagery), so it is retained for
a shorter period than the imagery:

| Age | Retained objects |
|---|---|
| newest 12 scans (6 h) | everything, `debra.nc` included |
| back to 144 scans (3 days) | `debra.png`, `dustrgb.png`, `meta.json` |
| older | nothing; the scan is deleted |

Steady state is near 1.5 GB across both regions. Both numbers are per region
in `regions.toml` and are counts of scans rather than durations: a count
bounds the bytes regardless of elapsed time, so the backlog published after an
outage cannot take the bucket past its budget. Staged CAMEL months are kept
two deep, so that a backfill spanning a month boundary still finds the earlier
month.

Pruning is part of the scheduled job: `run` and `backfill` both prune after
publishing, whether or not they published anything, since a run that publishes
nothing can still leave the bucket over budget. It is a step in the pipeline
rather than an R2 lifecycle rule because a lifecycle expiry would delete
objects that `latest.json` and the month index still point at. `shachen-ops
prune --region <key> --dry-run` reports what would be deleted without deleting
anything.

A prune takes the bucket listing as its input: one `list_objects` over the
region prefix determines what exists. A prune is therefore idempotent, and a
half-finished publish cannot desynchronise the policy from the bucket.

Every `meta.json` records the inputs alongside the output: which GFS cycle and
forecast hour supplied the skin temperature, which CAMEL month supplied the
emissivity, which shachen version and constant set ran. Each output stays
traceable to the fields it was computed from.

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

**Skin temperature.** MERRA-2 TS is published two to four weeks after the
fact, so it cannot supply a near-real-time pipeline. GFS `TMP:surface` from
`noaa-gfs-bdp-pds` is used instead: the same quantity from a model that
publishes about four hours behind its cycle. Only one GRIB2 record is
downloaded. Each 0.25° file is around half a gigabyte and is accompanied by a
`.idx` sidecar, so the fetch reads the index and retrieves that one record
with a ranged GET, about a megabyte.

DEBRA's background is calibrated against MERRA-2, and GFS skin temperature
differs from it, with the largest differences expected over desert at night.
The difference has not been measured here; every scan records which field it
used.

**Emissivity.** The background here is the semianalytic one (CAMEL emissivity
times a Planck curve at the skin temperature), which requires no state carried
between scans. CAMEL is a monthly climatology behind Earthdata
authentication, so it is staged once per region and month into a private R2
prefix by the `bootstrap-ancillary` workflow; the operational path holds no
Earthdata credentials.

`CAM5K30EM` V003 ends in December 2023, so an operational month has no granule
of its own. The product is a climatology, so the substitution is the same
calendar month from the most recent year that has a granule; the year used is
recorded in each scan's `meta.json` as `emissivity.source_month`.

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

`backfill` is what the schedule runs. It queries the bucket for the scans in
the window that are missing and processes those, newest first: GitHub cron
triggers run late and sometimes not at all, and the next backfill covers
whatever a missed run skipped. Scans younger than three minutes are skipped,
since an AHI timeline's directory appears before every strip is in it.
`backfill` prunes when it finishes; `--no-prune` skips that step, and the
standalone `prune` subcommand runs it separately.

## Workflows

| Workflow | Trigger | Steps |
|---|---|---|
| `ci` | push, PR | ruff, pytest, region table parses |
| `process` | every 30 min, manual | backfill each region, then prune it, one job per region |
| `bootstrap-ancillary` | manual | stage a region-month of CAMEL into R2 |
| `web` | push touching `web/`, manual | deploy the viewer to Cloudflare Pages |

`process` builds its matrix from `regions.toml`, so a new region needs no
workflow edit. Each region's job takes a concurrency group of its own, with
`cancel-in-progress: false`, so overlapping runs queue instead of writing
that region's manifests concurrently.

Secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` for
everything that publishes; `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` only
in the `earthdata` environment that `bootstrap-ancillary` runs in;
`CLOUDFLARE_API_TOKEN` for the Pages deploy, which takes the account id from
`CLOUDFLARE_ACCOUNT_ID` or falls back to `R2_ACCOUNT_ID`, the same account.

## Viewer

`web/` is a Cloudflare Pages site that reads the layout above and nothing
else. It shows the newest scan of a region, steps or animates back through
every retained scan, and wipes between the DEBRA composite and the Dust RGB
over the same scene. Each scan's `meta.json` is shown beside it, so what is on
screen stays attached to the GFS cycle and CAMEL month it was computed from,
and the footer credits every source the imagery is derived from — the two
sensors, the two ancillary datasets, both published algorithms, and the
basemap — since a derived product that names none of them is unciteable.

```
web/wrangler.jsonc              Pages project; binds the R2 bucket as BUCKET
web/public/                     the site — no build step, no dependencies
web/functions/api/regions.js    GET /api/regions, a delimited list of the layout root
web/functions/r2/[[path]].js    GET /r2/{region}/...  streams one published object
```

The bucket stays private. The browser never holds a credential and never
talks to R2: `functions/r2/[[path]].js` is the only reader, and it builds its
key rather than accepting one, so nothing outside `shachen-ops/v1` and no
extension the pipeline does not publish is reachable. Objects keep the
`Content-Type` and `Cache-Control` they were uploaded with, which is what puts
the pipeline's own cache tiers in front of the browser; immutable bodies are
also kept in the edge cache, so the second viewer of a scan does not pull it
out of R2 again.

Adding a region needs no change here either. The region picker comes from a
delimited list of `shachen-ops/v1/`, and the timeline from the month indexes,
which retention rewrites — so what the tape draws still exists. Imagery is one
to three megabytes a frame, so nothing is fetched until it is about to be
shown: the player waits for the next frame to decode before advancing, and the
tape marks which frames are already in hand.

```sh
cd web
wrangler pages dev        # against a local R2; seed it with wrangler r2 object put --local
wrangler pages deploy     # or push to main and let the web workflow do it
```

`BUCKET` in `wrangler.jsonc` has to name the same bucket the pipeline writes
to — the `R2_BUCKET` repository variable, not the `shachen` default in
`shachen_ops` — or the deploy fails with `R2 bucket not found`. The Pages
project itself is created by the first deploy.

## Resource use

Measured on one scan of each region, on a laptop:

| | `swus` (ABI) | `gobi` (AHI) |
|---|---|---|
| L1b downloaded | 95 MB | 77 MB (3 of 10 strips) |
| Wall time, cold | ~3 min | ~2.5 min |
| Wall time, inputs cached | ~5 s | ~10 s |
| Peak RSS | 2.9 GB | 2.6 GB |
| Published per scan | ~11 MB | ~23 MB |
| of which `debra.nc` | 7 MB | 20 MB |

Fetching accounts for most of the wall time, and peak RSS stays within the
16 GB of a 4-vCPU runner. At 48 scans a day across both regions that is about
1.7 GB a day of new objects, against a 2 GB bucket, which is what the
retention above reconciles and why `debra.nc` has the shortest retention.
Steady state is near 1.5 GB, so R2 storage stays under one dollar a month and
the writes (six per scan, ~17k a month) stay within the free tier.

## Not implemented

- The composite background (a rolling 14-day cloud-cleared TIR store), which
  includes the split-window water-vapour depression that the semianalytic
  background omits.
- A reanalysis pass that reprocesses older scans with MERRA-2 TS, to quantify
  the GFS substitution.
- Any frontend. The layout above is intended to support one.
