/*
 * The viewer.
 *
 * Everything on the page comes from the manifests the pipeline already
 * writes: /api/regions lists what the bucket holds, latest.json is the entry
 * point for a region, and the month indexes give the rest of the retained
 * scans. Nothing here knows how a scan is produced — only where it landed.
 *
 * Imagery is a megabyte or three a frame, so nothing is fetched until it is
 * about to be shown: the player waits for the next frame to decode before it
 * advances, and the tape marks which frames are already in hand.
 */

const R2 = "/r2";
const FRAME_MS = 550;
const PRELOAD_AHEAD = 3;
const REFRESH_MS = 60_000;

/** system follows the OS; the other two are a reader overruling it. */
const THEMES = { system: "light", light: "dark", dark: "system" };
const THEME_KEY = "shachen-theme";

const el = {
  html: document.documentElement,
  regions: document.getElementById("regions"),
  freshness: document.getElementById("freshness"),
  freshnessText: document.getElementById("freshnessText"),
  theme: document.getElementById("theme"),
  themeLabel: document.getElementById("themeLabel"),
  frame: document.getElementById("frame"),
  layerBase: document.getElementById("layerBase"),
  layerTop: document.getElementById("layerTop"),
  layerWipe: document.getElementById("layerWipe"),
  chipTime: document.getElementById("chipTime"),
  chipRegion: document.getElementById("chipRegion"),
  tags: document.getElementById("tags"),
  handle: document.getElementById("handle"),
  frameNote: document.getElementById("frameNote"),
  play: document.getElementById("play"),
  playLabel: document.getElementById("playLabel"),
  stepBack: document.getElementById("stepBack"),
  stepForward: document.getElementById("stepForward"),
  latest: document.getElementById("latest"),
  modeDebra: document.getElementById("modeDebra"),
  modeRgb: document.getElementById("modeRgb"),
  modeCompare: document.getElementById("modeCompare"),
  tape: document.getElementById("tape"),
  tapeCursor: document.getElementById("tapeCursor"),
  tapeAxis: document.getElementById("tapeAxis"),
  tapeCount: document.getElementById("tapeCount"),
  manifest: document.getElementById("manifest"),
  manifestToggle: document.getElementById("manifestToggle"),
  manifestBody: document.getElementById("manifestBody"),
  manifestWhen: document.getElementById("manifestWhen"),
  manifestAge: document.getElementById("manifestAge"),
  manifestSummary: document.getElementById("manifestSummary"),
  manifestMoreLabel: document.getElementById("manifestMoreLabel"),
  manifestFields: document.getElementById("manifestFields"),
  manifestDownloads: document.getElementById("manifestDownloads"),
  stage: document.querySelector(".stage"),
  footRetention: document.getElementById("footRetention"),
};

const state = {
  regions: [],
  region: null,
  pointer: null,
  scans: [],
  cursor: -1,
  mode: "debra",
  playing: false,
  wipe: 50,
  theme: "system",
  detailsOpen: true,
  facts: null,
  metaCache: new Map(),
  loaded: new Map(),
  ready: new Set(),
  metaTimer: null,
  playTimer: null,
  generation: 0,
};

// ------------------------------------------------------------ formatting

const pad = (n) => String(n).padStart(2, "0");

function stampToPath(region, iso, prefix) {
  // The manifest's own prefix is authoritative when it is there; the layout
  // root is stripped because /r2 supplies it.
  if (prefix) {
    const at = prefix.indexOf(`/${region}/`);
    if (at !== -1) return prefix.slice(at + 1);
    if (prefix.startsWith(`${region}/`)) return prefix;
  }
  const t = new Date(iso);
  return [
    region,
    t.getUTCFullYear(),
    pad(t.getUTCMonth() + 1),
    pad(t.getUTCDate()),
    `${pad(t.getUTCHours())}${pad(t.getUTCMinutes())}${pad(t.getUTCSeconds())}`,
  ].join("/");
}

function clock(iso) {
  const t = new Date(iso);
  return `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}Z`;
}

function stamp(iso) {
  const t = new Date(iso);
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())} ${clock(iso)}`;
}

function age(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${minutes % 60} min ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

/** Manifest values are the pipeline's own, but they still land in innerHTML. */
function esc(value) {
  return String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function degrees(value, positive, negative) {
  const rounded = Math.abs(Math.round(value * 10) / 10);
  return `${rounded}°${value < 0 ? negative : positive}`;
}

function bboxLabel(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return null;
  const [west, south, east, north] = bbox;
  return `${degrees(west, "E", "W")} to ${degrees(east, "E", "W")}, ${degrees(south, "N", "S")} to ${degrees(north, "N", "S")}`;
}

// ------------------------------------------------------------ image cache

function imageUrl(scan, name) {
  return `${R2}/${scan.path}/${name}`;
}

function fetchImage(url) {
  if (!state.loaded.has(url)) {
    state.loaded.set(
      url,
      new Promise((resolve) => {
        const image = new Image();
        image.decoding = "async";
        image.onload = () => {
          state.ready.add(url);
          resolve(image);
        };
        image.onerror = () => resolve(null);
        image.src = url;
      }),
    );
  }
  return state.loaded.get(url);
}

/** Which files a mode needs, so a DEBRA-only session never pulls the RGB. */
function layersFor(mode) {
  if (mode === "rgb") return ["dustrgb.png"];
  if (mode === "compare") return ["debra.png", "dustrgb.png"];
  return ["debra.png"];
}

function isReady(scan, mode = state.mode) {
  return layersFor(mode).every((name) => state.ready.has(imageUrl(scan, name)));
}

/**
 * Warm the neighbours of the current scan, forwards first.
 *
 * Play runs forwards, but the newest scan is where the page opens and
 * stepping back is what happens there, so both directions are warmed.
 */
function prefetch(from) {
  for (let step = 1; step <= PRELOAD_AHEAD; step += 1) {
    [from + step, from - step].forEach((index) => {
      const scan = state.scans[index];
      if (!scan) return;
      layersFor(state.mode).forEach((name) => fetchImage(imageUrl(scan, name)));
      loadMeta(scan);
    });
  }
}

// --------------------------------------------------------------- fetching

async function getJSON(url) {
  const response = await fetch(url, { headers: { accept: "application/json" } });
  if (!response.ok) return null;
  return response.json();
}

async function loadRegions() {
  const payload = await getJSON("/api/regions");
  if (!payload || !payload.regions.length) {
    fail("The bucket has no published regions yet.");
    return false;
  }
  state.regions = payload.regions;
  renderRegions();
  return true;
}

async function loadRegion(key) {
  const generation = ++state.generation;
  stopPlaying();
  state.region = key;
  state.metaCache.clear();
  state.facts = null;
  renderRegions();

  const pointer = await getJSON(`${R2}/${key}/latest.json`);
  if (generation !== state.generation) return;
  if (!pointer || !pointer.latest) {
    fail(`No scans published for ${key} yet.`);
    return;
  }
  state.pointer = pointer;

  const scans = await buildTimeline(key, pointer);
  if (generation !== state.generation) return;
  state.scans = scans;

  el.footRetention.textContent = pointer.retention ? pointer.retention.note : "";
  renderTape();
  renderFreshness();

  const wanted = new URL(location.href).searchParams.get("scan");
  const index = wanted ? scans.findIndex((scan) => scan.iso === wanted) : -1;
  select(index === -1 ? scans.length - 1 : index);
}

/**
 * Every retained scan for a region, oldest first.
 *
 * The month indexes are the full list — prune rewrites them, so what they
 * carry still exists — and latest.json's recent entries fill in the asset
 * lists for the newest scans. For older ones the retention policy says
 * whether the field file survived.
 */
async function buildTimeline(key, pointer) {
  const newest = new Date(pointer.latest.scan_start);
  const months = new Set();
  for (const back of [0, 1]) {
    const when = new Date(Date.UTC(newest.getUTCFullYear(), newest.getUTCMonth() - back, 1));
    months.add(`${when.getUTCFullYear()}/${pad(when.getUTCMonth() + 1)}`);
  }

  const listings = await Promise.all(
    [...months].map((month) => getJSON(`${R2}/${key}/index/${month}.json`)),
  );

  const stamps = new Set();
  listings.forEach((listing) => (listing?.scans || []).forEach((iso) => stamps.add(iso)));
  (pointer.recent || []).forEach((entry) => stamps.add(entry.scan_start));
  stamps.add(pointer.latest.scan_start);

  const recent = new Map((pointer.recent || []).map((entry) => [entry.scan_start, entry]));
  const keepFields = pointer.retention?.keep_field_scans ?? 0;
  const ordered = [...stamps].sort();

  return ordered.map((iso, position) => {
    const entry = recent.get(iso);
    const assets = entry?.assets || null;
    const rankFromNewest = ordered.length - 1 - position;
    return {
      iso,
      at: new Date(iso),
      path: stampToPath(key, iso, entry?.prefix),
      assets,
      hasField: assets ? assets.includes("debra.nc") : rankFromNewest < keepFields,
    };
  });
}

async function loadMeta(scan) {
  if (state.metaCache.has(scan.iso)) return state.metaCache.get(scan.iso);
  const meta = await getJSON(`${R2}/${scan.path}/meta.json`);
  if (meta) state.metaCache.set(scan.iso, meta);
  return meta;
}

// ----------------------------------------------------------------- theme

/**
 * Only an explicit choice is written to the root; "system" removes the
 * attribute and hands the page back to prefers-color-scheme, which is what
 * light-dark() in the stylesheet is reading.
 */
function applyTheme() {
  if (state.theme === "system") delete el.html.dataset.theme;
  else el.html.dataset.theme = state.theme;
  el.themeLabel.textContent = state.theme;
  el.theme.title = `Theme: ${state.theme}. Click to switch.`;
  el.theme.setAttribute("aria-label", `Theme: ${state.theme}. Click to switch.`);
}

function readTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored && Object.hasOwn(THEMES, stored)) return stored;
  } catch (error) {
    /* Blocked storage is not an error here; the system preference stands. */
  }
  return "system";
}

function cycleTheme() {
  state.theme = THEMES[state.theme];
  try {
    localStorage.setItem(THEME_KEY, state.theme);
  } catch (error) {
    /* The choice then lasts for this page view only. */
  }
  applyTheme();
}

// -------------------------------------------------------------- rendering

/** "GOES-19 · ABI" — the product code carries the sensor the key does not. */
function regionSubtitle(region) {
  const sensor = (region.product || "").split("-")[0];
  if (region.satellite && sensor) return `${region.satellite} · ${sensor}`;
  return region.satellite || region.description || "";
}

function renderRegions() {
  el.regions.innerHTML = "";
  state.regions.forEach((region) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "region";
    button.setAttribute("aria-pressed", String(region.key === state.region));
    button.innerHTML = `<span class="region-key"></span><span class="region-where"></span>`;
    button.querySelector(".region-key").textContent = region.key;
    button.querySelector(".region-where").textContent = regionSubtitle(region);
    button.title = region.description;
    button.addEventListener("click", () => {
      if (region.key !== state.region) loadRegion(region.key);
    });
    el.regions.append(button);
  });
}

function renderFreshness() {
  const scan = state.scans[state.scans.length - 1];
  if (!scan) return;
  const cadence = state.pointer?.retention?.publish_every_minutes || 30;
  const minutes = (Date.now() - scan.at.getTime()) / 60000;
  const late = minutes > cadence * 3;
  el.freshness.dataset.state = late ? "late" : "current";
  el.freshnessText.textContent = late
    ? `newest ${age(scan.iso)} · behind the ${cadence} min cadence`
    : `newest ${age(scan.iso)}`;
}

function renderTape() {
  el.tape.querySelectorAll(".tick").forEach((tick) => tick.remove());
  el.tapeAxis.innerHTML = "";

  const scans = state.scans;
  el.tapeCount.textContent = scans.length
    ? `${scans.length} scan${scans.length === 1 ? "" : "s"} retained · ${stamp(scans[0].iso)} to ${stamp(scans[scans.length - 1].iso)}`
    : "";
  el.latest.disabled = scans.length === 0 || state.cursor === scans.length - 1;
  // One scan still gets a tick. Production regions retain a hundred and more,
  // but a local bucket with a single scan should read as a short timeline
  // rather than as a tape that failed to draw.
  if (!scans.length) return;

  const first = scans[0].at.getTime();
  const span = scans[scans.length - 1].at.getTime() - first || 1;
  // A tick is 2px wide, centred on its position, and the tape clips what
  // overflows it — so the track is inset by half a tick at each end. Without
  // it the oldest and the newest scan, which are the two worth pointing at,
  // are each drawn half outside the box.
  const alongTape = (time) => `calc(1px + (100% - 2px) * ${(time - first) / span})`;
  const positionOf = (scan) => ((scan.at.getTime() - first) / span) * 100;

  const fragment = document.createDocumentFragment();
  scans.forEach((scan, index) => {
    const tick = document.createElement("span");
    tick.className = "tick";
    if (isReady(scan)) tick.classList.add("is-ready");
    if (index === state.cursor) tick.classList.add("is-current");
    tick.style.left = alongTape(scan.at.getTime());
    tick.dataset.index = String(index);
    fragment.append(tick);
  });
  el.tape.append(fragment);

  // A label at each UTC midnight the window crosses: the gaps between ticks
  // are real, so the axis has to be time, not scan number.
  const day = 86_400_000;
  const start = Math.ceil(first / day) * day;
  for (let t = start; t <= first + span; t += day) {
    const label = document.createElement("span");
    label.className = "axis-label";
    label.style.left = alongTape(t);
    const when = new Date(t);
    label.textContent = `${when.getUTCFullYear()}-${pad(when.getUTCMonth() + 1)}-${pad(when.getUTCDate())}`;
    el.tapeAxis.append(label);
  }

  const current = scans[state.cursor];
  if (current) {
    const at = positionOf(current);
    el.tapeCursor.textContent = clock(current.iso);
    el.tapeCursor.style.left = alongTape(current.at.getTime());
    // Near an end the label would hang off the tape, so it anchors instead.
    el.tapeCursor.style.translate = at > 92 ? "-100% 0" : at < 8 ? "0 0" : "-50% 0";
  }
  el.tape.setAttribute("aria-valuemax", String(scans.length - 1));
  el.tape.setAttribute("aria-valuenow", String(Math.max(state.cursor, 0)));
  if (current) el.tape.setAttribute("aria-valuetext", `${stamp(current.iso)} UTC`);
}

/** The frame carries its own caption, so a screenshot of it is still dated. */
function renderChips(scan) {
  el.chipTime.textContent = `${stamp(scan.iso)} UTC`;
  el.chipRegion.textContent = state.region || "";
}

function renderManifest(scan, meta) {
  const pointer = state.pointer || {};
  const own = meta || (scan.iso === pointer.latest?.scan_start ? pointer.latest : null);
  if (own) state.facts = own;

  // What the region is stays put while scrubbing; what a scan was computed
  // from comes from that scan's own manifest and waits for it, because
  // showing the previous scan's GFS cycle here would be a lie.
  const common = own || state.facts;
  const pending = own ? null : "reading meta.json…";

  el.manifestWhen.textContent = `${stamp(scan.iso)} UTC`;
  el.manifestAge.textContent = age(scan.iso);

  // Collapsed, the head still has to answer what this scan was computed from,
  // so the three fields that move scan to scan are repeated there.
  const summary = [
    own?.skin_temperature
      ? `GFS ${clock(own.skin_temperature.cycle)} +${own.skin_temperature.forecast_hour} h`
      : pending,
    own?.emissivity ? `CAMEL ${own.emissivity.month}` : pending,
    common ? `shachen ${common.shachen_version}` : null,
  ].filter(Boolean);
  el.manifestSummary.innerHTML = summary.map((item) => `<span>${esc(item)}</span>`).join("");

  const fields = [];
  const add = (label, value, note) => {
    if (!value) return;
    fields.push(
      `<div class="field"><dt class="field-label">${label}</dt><dd class="field-value">${esc(value)}</dd>${
        note ? `<dd class="field-note">${esc(note)}</dd>` : ""
      }</div>`,
    );
  };

  if (common) {
    add("Satellite", common.satellite, `${common.product} · ${common.sensor.toUpperCase()}`);
    add("Domain", bboxLabel(common.bbox), common.description);
  }

  if (own?.skin_temperature) {
    add(
      "Skin temperature",
      `GFS ${clock(own.skin_temperature.cycle)} +${own.skin_temperature.forecast_hour} h`,
      `valid ${clock(own.skin_temperature.valid_time)} · ${own.skin_temperature.source}`,
    );
  } else {
    add("Skin temperature", pending);
  }

  if (own?.emissivity) {
    add(
      "Emissivity",
      `CAMEL ${own.emissivity.month}`,
      own.emissivity.source_month && own.emissivity.source_month !== own.emissivity.month
        ? `climatology from ${own.emissivity.source_month}`
        : null,
    );
  } else {
    add("Emissivity", pending);
  }

  if (common) {
    add("Constants", common.tuning, common.background ? `${common.background} background` : null);
    add(
      "Produced by",
      `shachen ${common.shachen_version}`,
      `ops ${common.shachen_ops_version} · layout ${common.layout_version}`,
    );
  }

  add(
    "Published",
    own?.published_at ? stamp(own.published_at) : pending,
    own?.processing_seconds ? `${Math.round(own.processing_seconds)} s of compute` : null,
  );

  el.manifestFields.innerHTML = fields.join("");

  const assets = own?.assets || scan.assets;
  const hasField = assets ? assets.includes("debra.nc") : scan.hasField;

  el.manifestDownloads.innerHTML = `
    <a class="download" href="${imageUrl(scan, "debra.png")}" download>DEBRA png</a>
    <a class="download" href="${imageUrl(scan, "dustrgb.png")}" download>Dust RGB png</a>
    <a class="download" href="${imageUrl(scan, "debra.nc")}" download ${
      hasField ? "" : 'aria-disabled="true" title="Field data for this scan has passed out of retention"'
    }>Field netCDF</a>`;
}

function setDetails(open) {
  state.detailsOpen = open;
  el.manifestToggle.setAttribute("aria-expanded", String(open));
  el.manifestBody.hidden = !open;
  el.manifestMoreLabel.textContent = open ? "Hide details" : "Details";
}

function fail(message) {
  el.html.dataset.state = "failed";
  const existing = el.stage.querySelector(".notice");
  if (existing) existing.remove();
  const notice = document.createElement("p");
  notice.className = "notice";
  notice.textContent = message;
  el.stage.prepend(notice);
}

// -------------------------------------------------------------- selection

async function select(index, options = {}) {
  const scan = state.scans[index];
  if (!scan) return;
  state.cursor = index;

  el.frame.dataset.mode = state.mode;
  el.frameNote.textContent = isReady(scan) ? "" : "Loading…";

  const names = layersFor(state.mode);
  const generation = state.generation;
  const images = await Promise.all(names.map((name) => fetchImage(imageUrl(scan, name))));
  if (generation !== state.generation || state.cursor !== index) return;

  if (images.some((image) => image === null)) {
    el.frameNote.textContent = "This scan's imagery is no longer retained.";
    el.frame.dataset.empty = "true";
  } else {
    el.frameNote.textContent = "";
    el.frame.dataset.empty = "false";
    const [first] = images;
    if (first.naturalWidth) {
      el.frame.style.setProperty("--plate-ratio", first.naturalWidth / first.naturalHeight);
    }
    if (state.mode === "rgb") {
      el.layerBase.src = imageUrl(scan, "dustrgb.png");
    } else {
      el.layerTop.src = imageUrl(scan, "debra.png");
      if (state.mode === "compare") {
        el.layerBase.src = imageUrl(scan, "dustrgb.png");
      } else {
        // An img with alt and no src can paint its alt text over the plate.
        el.layerBase.removeAttribute("src");
        el.layerBase.alt = "";
      }
    }
    el.layerTop.alt = `DEBRA-Dust composite, ${state.region}, ${stamp(scan.iso)} UTC`;
    if (el.layerBase.getAttribute("src")) {
      el.layerBase.alt = `Dust RGB, ${state.region}, ${stamp(scan.iso)} UTC`;
    }
  }

  renderChips(scan);
  renderTape();
  renderManifest(scan, state.metaCache.get(scan.iso));
  syncUrl(scan);
  prefetch(index);

  // The manifest is one small object per scan; scrubbing should not fetch one
  // per frame, so it waits until the selection settles.
  clearTimeout(state.metaTimer);
  state.metaTimer = setTimeout(async () => {
    const meta = await loadMeta(scan);
    if (meta && state.scans[state.cursor]?.iso === scan.iso) renderManifest(scan, meta);
  }, options.fromPlayer ? 900 : 250);
}

function syncUrl(scan) {
  const url = new URL(location.href);
  url.searchParams.set("region", state.region);
  const newest = state.scans[state.scans.length - 1];
  if (scan.iso === newest?.iso) url.searchParams.delete("scan");
  else url.searchParams.set("scan", scan.iso);
  history.replaceState(null, "", url);
}

function step(delta) {
  const next = state.cursor + delta;
  if (next < 0 || next >= state.scans.length) return;
  select(next);
}

function setMode(mode) {
  state.mode = mode;
  el.modeDebra.setAttribute("aria-pressed", String(mode === "debra"));
  el.modeRgb.setAttribute("aria-pressed", String(mode === "rgb"));
  el.modeCompare.setAttribute("aria-pressed", String(mode === "compare"));
  el.tags.hidden = mode !== "compare";
  el.handle.hidden = mode !== "compare";
  el.frame.dataset.mode = mode;
  if (state.cursor >= 0) select(state.cursor);
}

function setWipe(percent) {
  state.wipe = Math.min(100, Math.max(0, percent));
  el.frame.style.setProperty("--wipe", `${state.wipe}%`);
  el.handle.setAttribute("aria-valuenow", String(Math.round(state.wipe)));
  el.handle.setAttribute(
    "aria-valuetext",
    `${Math.round(state.wipe)}% DEBRA, ${100 - Math.round(state.wipe)}% Dust RGB`,
  );
}

// ----------------------------------------------------------------- player

function stopPlaying() {
  state.playing = false;
  clearTimeout(state.playTimer);
  el.play.setAttribute("aria-pressed", "false");
  el.playLabel.textContent = "Play";
}

function startPlaying() {
  if (state.scans.length < 2) return;
  state.playing = true;
  el.play.setAttribute("aria-pressed", "true");
  el.playLabel.textContent = "Pause";
  tick();
}

async function tick() {
  if (!state.playing) return;
  const next = state.cursor >= state.scans.length - 1 ? 0 : state.cursor + 1;
  const scan = state.scans[next];
  const generation = state.generation;

  if (!isReady(scan)) el.frameNote.textContent = "Loading…";
  await Promise.all(layersFor(state.mode).map((name) => fetchImage(imageUrl(scan, name))));
  if (!state.playing || generation !== state.generation) return;

  await select(next, { fromPlayer: true });
  if (!state.playing) return;
  state.playTimer = setTimeout(tick, FRAME_MS);
}

// ------------------------------------------------------------- interaction

function scanFromPointer(event) {
  const box = el.tape.getBoundingClientRect();
  const fraction = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  const first = state.scans[0].at.getTime();
  const span = state.scans[state.scans.length - 1].at.getTime() - first || 1;
  const target = first + fraction * span;

  let best = 0;
  let distance = Infinity;
  state.scans.forEach((scan, index) => {
    const gap = Math.abs(scan.at.getTime() - target);
    if (gap < distance) {
      distance = gap;
      best = index;
    }
  });
  return best;
}

function wireTape() {
  let scrubbing = false;
  el.tape.addEventListener("pointerdown", (event) => {
    if (!state.scans.length) return;
    scrubbing = true;
    stopPlaying();
    el.tape.setPointerCapture(event.pointerId);
    select(scanFromPointer(event));
  });
  el.tape.addEventListener("pointermove", (event) => {
    if (scrubbing) select(scanFromPointer(event));
  });
  const release = () => {
    scrubbing = false;
  };
  el.tape.addEventListener("pointerup", release);
  el.tape.addEventListener("pointercancel", release);
}

function wireHandle() {
  let dragging = false;
  const move = (event) => {
    const box = el.frame.getBoundingClientRect();
    setWipe(((event.clientX - box.left) / box.width) * 100);
  };
  const start = (event) => {
    if (state.mode !== "compare") return;
    dragging = true;
    el.frame.setPointerCapture(event.pointerId);
    move(event);
  };
  el.frame.addEventListener("pointerdown", start);
  el.frame.addEventListener("pointermove", (event) => {
    if (dragging) move(event);
  });
  const release = () => {
    dragging = false;
  };
  el.frame.addEventListener("pointerup", release);
  el.frame.addEventListener("pointercancel", release);

  el.handle.addEventListener("keydown", (event) => {
    const nudge = event.shiftKey ? 10 : 2;
    if (event.key === "ArrowLeft") setWipe(state.wipe - nudge);
    else if (event.key === "ArrowRight") setWipe(state.wipe + nudge);
    else return;
    event.preventDefault();
    event.stopPropagation();
  });
}

function wireKeys() {
  document.addEventListener("keydown", (event) => {
    // Controls keep their own keyboard behaviour — Space on a focused Play
    // button is already a click, and handling it again here would cancel it.
    // The tape and the wipe handle are not buttons, so they fall through to
    // the shortcuts below.
    const target = event.target;
    if (target instanceof Element && target.closest("input, textarea, select, button, a")) return;
    switch (event.key) {
      case "ArrowLeft":
        stopPlaying();
        step(-1);
        break;
      case "ArrowRight":
        stopPlaying();
        step(1);
        break;
      case " ":
        event.preventDefault();
        state.playing ? stopPlaying() : startPlaying();
        break;
      case "d":
        setMode("debra");
        break;
      case "r":
        setMode("rgb");
        break;
      case "c":
        setMode("compare");
        break;
      default:
        return;
    }
  });
}

function wireButtons() {
  el.play.addEventListener("click", () => (state.playing ? stopPlaying() : startPlaying()));
  el.stepBack.addEventListener("click", () => {
    stopPlaying();
    step(-1);
  });
  el.stepForward.addEventListener("click", () => {
    stopPlaying();
    step(1);
  });
  el.latest.addEventListener("click", () => {
    stopPlaying();
    select(state.scans.length - 1);
  });
  el.modeDebra.addEventListener("click", () => setMode("debra"));
  el.modeRgb.addEventListener("click", () => setMode("rgb"));
  el.modeCompare.addEventListener("click", () => setMode("compare"));
  el.theme.addEventListener("click", cycleTheme);
  el.manifestToggle.addEventListener("click", () => setDetails(!state.detailsOpen));
}

/**
 * A published scan should appear without a reload: re-read the pointer, and
 * follow it only if the viewer is already sitting on the newest scan.
 */
async function refresh() {
  if (document.hidden || !state.region || state.playing) return;
  const wasNewest = state.cursor === state.scans.length - 1;
  const pointer = await getJSON(`${R2}/${state.region}/latest.json`);
  if (!pointer?.latest || pointer.latest.scan_start === state.pointer?.latest?.scan_start) {
    renderFreshness();
    return;
  }
  state.pointer = pointer;
  state.scans = await buildTimeline(state.region, pointer);
  renderTape();
  renderFreshness();
  if (wasNewest) select(state.scans.length - 1);
}

// -------------------------------------------------------------------- boot

async function main() {
  state.theme = readTheme();
  applyTheme();
  setDetails(state.detailsOpen);

  wireTape();
  wireHandle();
  wireKeys();
  wireButtons();
  setWipe(50);

  if (!(await loadRegions())) return;
  const wanted = new URL(location.href).searchParams.get("region");
  const known = state.regions.find((region) => region.key === wanted);
  await loadRegion(known ? known.key : state.regions[0].key);
  el.html.dataset.state = "ready";

  setInterval(refresh, REFRESH_MS);
  setInterval(renderFreshness, 30_000);
}

main().catch((error) => fail(`Couldn't load the bucket: ${error.message}`));
