"""shachen-ops: the operational pipeline around shachen's DEBRA-Dust.

One region and one scan time is the unit of work. :mod:`shachen_ops.scans`
finds scans on the NOAA Open Data buckets, :mod:`shachen_ops.fetch` and
:mod:`shachen_ops.gfs` get their inputs onto disk, :mod:`shachen_ops.process`
runs DEBRA and renders, and :mod:`shachen_ops.publish` writes the objects and
the manifests that make the bucket readable. :mod:`shachen_ops.cli` is what
the GitHub Actions workflows call.
"""
