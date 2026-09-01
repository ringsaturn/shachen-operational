/**
 * GET /api/regions — which regions the bucket carries, newest scan first.
 *
 * The listing is the source of truth, as it is for the pipeline's own prune:
 * a delimited list of the layout root gives the region keys, so a region
 * added to regions.toml appears here the first time it publishes, with no
 * change to this site.
 *
 * Each region's latest.json supplies the label and the freshness the picker
 * shows; the page then fetches the pointer itself for the region it lands on.
 */

import { ROOT, problem } from "../_shared.js";

const POINTER_CACHE = "public, max-age=60";

export async function onRequestGet({ env }) {
  let listing;
  try {
    listing = await env.BUCKET.list({ prefix: `${ROOT}/`, delimiter: "/" });
  } catch (error) {
    return problem(502, `could not list the bucket: ${error.message}`);
  }

  const keys = (listing.delimitedPrefixes || [])
    .map((prefix) => prefix.slice(ROOT.length + 1).replace(/\/$/, ""))
    .filter((key) => key && !key.includes("/"));

  const regions = (await Promise.all(keys.map((key) => summarise(env, key)))).filter(Boolean);

  // Freshest first, so the page opens on the region that just published.
  regions.sort((a, b) => (b.scan_start || "").localeCompare(a.scan_start || "") || a.key.localeCompare(b.key));

  return new Response(JSON.stringify({ regions }, null, 2) + "\n", {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": POINTER_CACHE,
    },
  });
}

async function summarise(env, key) {
  const object = await env.BUCKET.get(`${ROOT}/${key}/latest.json`);
  if (!object) return null;

  let pointer;
  try {
    pointer = await object.json();
  } catch {
    // A pointer mid-write is not worth failing the whole picker over.
    return null;
  }

  const latest = pointer.latest || {};
  return {
    key,
    description: latest.description || "",
    satellite: latest.satellite || "",
    product: latest.product || "",
    scan_start: latest.scan_start || null,
    updated_at: pointer.updated_at || null,
  };
}
