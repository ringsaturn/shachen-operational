/**
 * The site's one door into R2: GET /r2/{region}/... streams the object the
 * pipeline published at shachen-ops/v1/{region}/...
 *
 * The bucket stays private. Objects keep the Content-Type and Cache-Control
 * the pipeline stored with them, so the tiers it chose — imagery immutable,
 * latest.json a minute, the month index five — are what the browser and the
 * edge see, rather than something this function invents.
 *
 * Range and If-None-Match are passed through to R2 so an interrupted image
 * resumes and a revisit revalidates instead of re-downloading. Immutable
 * bodies are also put in the edge cache: a scan's PNG is a megabyte or three,
 * and the second viewer of a scan should not pull it out of R2 again.
 */

import { contentType, objectKey, problem } from "../_shared.js";

const IMMUTABLE = "public, max-age=31536000, immutable";

export const onRequestGet = (context) => serve(context, true);
export const onRequestHead = (context) => serve(context, false);

async function serve({ request, env, params, waitUntil }, withBody) {
  const target = objectKey(params.path);
  if (!target) return problem(400, "not a path this site publishes");

  const ranged = request.headers.has("range");
  const cache = caches.default;
  const cacheable = withBody && !ranged;
  if (cacheable) {
    const hit = await cache.match(request);
    if (hit) return hit;
  }

  // Only pass a range when one was asked for: given a Headers object with no
  // Range in it, R2 still reports a range covering the whole object, which
  // would turn every plain GET into a 206.
  const object = withBody
    ? await env.BUCKET.get(target.key, {
        ...(ranged ? { range: request.headers } : {}),
        onlyIf: request.headers,
      })
    : await env.BUCKET.head(target.key);

  if (!object) {
    // Either never published, or aged out of retention. Both are 404s, and
    // the client distinguishes them from the manifest it already holds.
    return problem(404, "no such object; it may have passed out of retention");
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  if (!headers.has("content-type")) headers.set("content-type", contentType(target.extension));
  if (!headers.has("cache-control")) headers.set("cache-control", IMMUTABLE);
  headers.set("etag", object.httpEtag);
  headers.set("last-modified", object.uploaded.toUTCString());
  headers.set("accept-ranges", "bytes");

  if (!withBody) {
    headers.set("content-length", String(object.size));
    return new Response(null, { status: 200, headers });
  }

  // R2 returns metadata without a body when onlyIf fails: a revalidation that
  // matched (304), or a precondition that did not (412).
  if (!object.body) {
    const revalidating = request.headers.has("if-none-match") || request.headers.has("if-modified-since");
    return new Response(null, { status: revalidating ? 304 : 412, headers });
  }

  let status = 200;
  if (ranged && object.range && "offset" in object.range) {
    const start = object.range.offset;
    const length = object.range.length ?? object.size - start;
    headers.set("content-range", `bytes ${start}-${start + length - 1}/${object.size}`);
    headers.set("content-length", String(length));
    status = 206;
  } else {
    headers.set("content-length", String(object.size));
  }

  const response = new Response(object.body, { status, headers });
  if (cacheable && status === 200 && /immutable/.test(headers.get("cache-control") || "")) {
    waitUntil(cache.put(request, response.clone()));
  }
  return response;
}
