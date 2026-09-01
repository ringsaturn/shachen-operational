/**
 * What the functions share: where the pipeline's objects live, and how a
 * request path becomes a bucket key.
 *
 * The bucket holds more than this site should serve — staged CAMEL months,
 * and whatever else the account keeps there — so the key a request can reach
 * is built rather than accepted: everything is forced under ROOT, and only
 * the three extensions the pipeline publishes are allowed through.
 */

/** Root prefix every published object is under; mirrors shachen_ops.config. */
export const ROOT = "shachen-ops/v1";

/** Fallbacks for objects stored without the metadata the pipeline sets. */
const CONTENT_TYPES = {
  png: "image/png",
  json: "application/json",
  nc: "application/x-netcdf",
};

const SAFE_PATH = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/;

/**
 * The bucket key for a request path, or null if it is not one we publish.
 *
 * @param {string[]|string} path segments from the catch-all route
 * @returns {{key: string, extension: string}|null}
 */
export function objectKey(path) {
  const parts = Array.isArray(path) ? path : [path];
  const joined = parts.join("/");
  if (!joined || joined.length > 256) return null;
  if (!SAFE_PATH.test(joined) || joined.includes("..") || joined.includes("//")) return null;

  const extension = joined.slice(joined.lastIndexOf(".") + 1).toLowerCase();
  if (!(extension in CONTENT_TYPES)) return null;

  return { key: `${ROOT}/${joined}`, extension };
}

export function contentType(extension) {
  return CONTENT_TYPES[extension] || "application/octet-stream";
}

export function problem(status, message) {
  return new Response(JSON.stringify({ error: message }, null, 2) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}
