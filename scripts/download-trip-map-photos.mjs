#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = require(path.join(projectRoot, "trip-map-photos.js"));
const force = process.argv.includes("--force");
const stopFilterArgument = process.argv.find((argument) => argument.startsWith("--stop="));
const stopFilter = stopFilterArgument?.slice("--stop=".length) || "";
if (stopFilterArgument && !stopFilter) throw new Error("--stop requires a non-empty trip-map stop id");
if (stopFilter && !manifest.stops[stopFilter]) throw new Error(`Unknown trip-map stop: ${stopFilter}`);
const concurrency = 6;
const queue = Object.entries(manifest.stops)
  .filter(([stopId]) => !stopFilter || stopId === stopFilter)
  .flatMap(([stopId, photos]) => photos.map((photo) => ({ stopId, photo })));
const tempRoot = await mkdtemp(path.join(os.tmpdir(), "tundi-trip-map-photos-"));
const metadataByTitle = new Map();
const approvedOfficialDomains = new Set([
  "zamardikalandpark.hu",
  "balatonibob.hu",
  "bfnp.hu",
  "csodabogyos.hu",
  "muveszetekvolgye.hu",
  "szilvasvaradbob.hu",
  "zsolnaynegyed.hu",
  "egrivar.hu",
  "zemplen723.eu",
  "zemplenkalandpark.hu",
]);
const approvedEditorialDomains = new Set([
  "showcaves.com",
]);
const approvedWikimediaDomains = new Set([
  "commons.wikimedia.org",
  "upload.wikimedia.org",
]);

function isApprovedOfficialHost(hostname) {
  return [...approvedOfficialDomains].some((domain) => hostname === domain || hostname.endsWith(`.${domain}`));
}

function isApprovedEditorialHost(hostname) {
  return [...approvedEditorialDomains].some((domain) => hostname === domain || hostname.endsWith(`.${domain}`));
}

function isApprovedWikimediaHost(hostname) {
  return approvedWikimediaDomains.has(hostname);
}

function assertSafePhoto(item) {
  const { stopId, photo } = item;
  const expectedPrefix = `images/trip-map/${manifest.assetRevision}/${stopId}/`;
  if (!photo.src.startsWith(expectedPrefix) || path.isAbsolute(photo.src) || photo.src.includes("..")) {
    throw new Error(`Unsafe output path for ${photo.sourceTitle}: ${photo.src}`);
  }
  if (photo.sourceType === "wikimedia" && !/^File:.+/.test(photo.commonsTitle || "")) throw new Error(`Missing Commons file title for ${photo.src}`);
  if (photo.sourceType === "flickr") {
    const asset = new URL(photo.assetUrl);
    if (asset.protocol !== "https:" || asset.hostname !== "live.staticflickr.com") throw new Error(`Unsafe Flickr asset URL for ${photo.src}`);
  }
  if (photo.sourceType === "official") {
    const source = new URL(photo.sourceUrl);
    const asset = new URL(photo.assetUrl);
    if (source.protocol !== "https:" || !isApprovedOfficialHost(source.hostname)) throw new Error(`Unsafe official source URL for ${photo.src}`);
    if (asset.protocol !== "https:" || !isApprovedOfficialHost(asset.hostname)) throw new Error(`Unsafe official asset URL for ${photo.src}`);
    if (source.hostname.replace(/^www\./, "") !== asset.hostname.replace(/^www\./, "")) throw new Error(`Official source and asset hosts differ for ${photo.src}`);
  }
  if (photo.sourceType === "editorial") {
    const source = new URL(photo.sourceUrl);
    const asset = new URL(photo.assetUrl);
    if (source.protocol !== "https:" || !isApprovedEditorialHost(source.hostname)) throw new Error(`Unsafe editorial source URL for ${photo.src}`);
    if (asset.protocol !== "https:" || !isApprovedEditorialHost(asset.hostname)) throw new Error(`Unsafe editorial asset URL for ${photo.src}`);
    if (source.hostname.replace(/^www\./, "") !== asset.hostname.replace(/^www\./, "")) throw new Error(`Editorial source and asset hosts differ for ${photo.src}`);
  }
}

async function exists(file) {
  try {
    await stat(file);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

async function isValidWebp(file) {
  if (!await exists(file)) return false;
  const bytes = await readFile(file);
  return bytes.length >= 12_000
    && bytes.subarray(0, 4).toString("ascii") === "RIFF"
    && bytes.subarray(8, 12).toString("ascii") === "WEBP";
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const titleKey = (title) => String(title).replaceAll("_", " ").trim().toLocaleLowerCase("en");

async function fetchWithRetry(url, label, { redirect = "follow" } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: { "User-Agent": "MatijaTundiTripMap/1.0 (https://github.com/Pero122/matija-tundi-trip)" },
        redirect,
        signal: AbortSignal.timeout(30_000),
      });
      if (redirect === "manual" && response.status >= 300 && response.status < 400) return response;
      if (response.ok) return response;
      lastError = new Error(`${label} returned ${response.status}`);
      const transient = response.status === 408 || response.status === 425 || response.status === 429 || response.status >= 500;
      if (!transient || attempt === 5) throw lastError;
      const retrySeconds = response.status === 429 ? Number(response.headers.get("retry-after")) || attempt * 10 : attempt * 2;
      await response.body?.cancel();
      await sleep((retrySeconds + 1) * 1000);
    } catch (error) {
      lastError = error;
      if (attempt === 5 || (error.message?.includes(" returned ") && !/ returned (408|425|429|5\d\d)$/.test(error.message))) throw error;
      await sleep(attempt * 2_000);
    }
  }
  throw lastError || new Error(`${label} exhausted its retry budget`);
}

async function fetchWithApprovedRedirects(initialUrl, label, isAllowed) {
  let currentUrl = new URL(initialUrl);
  for (let redirectCount = 0; redirectCount <= 5; redirectCount += 1) {
    if (!isAllowed(currentUrl)) throw new Error(`${label} resolved to an unsafe URL: ${currentUrl.href}`);
    const response = await fetchWithRetry(currentUrl.href, label, { redirect: "manual" });
    if (response.status < 300 || response.status >= 400) return response;
    const location = response.headers.get("location");
    await response.body?.cancel();
    if (!location) throw new Error(`${label} returned a redirect without a Location header`);
    currentUrl = new URL(location, currentUrl);
  }
  throw new Error(`${label} exceeded five approved redirect hops`);
}

async function loadCommonsMetadata() {
  const titles = queue.filter(({ photo }) => photo.sourceType === "wikimedia").map(({ photo }) => photo.commonsTitle);
  for (let offset = 0; offset < titles.length; offset += 50) {
    const batch = titles.slice(offset, offset + 50);
    const params = new URLSearchParams({
      action: "query",
      format: "json",
      formatversion: "2",
      prop: "imageinfo",
      iiprop: "url|mime|size",
      iiurlwidth: "1280",
      redirects: "1",
      titles: batch.join("|"),
    });
    const response = await fetchWithApprovedRedirects(
      `https://commons.wikimedia.org/w/api.php?${params}`,
      "Commons metadata request",
      (candidate) => candidate.protocol === "https:" && isApprovedWikimediaHost(candidate.hostname),
    );
    const result = await response.json();
    for (const page of result.query?.pages || []) {
      const imageInfo = page?.imageinfo?.[0];
      if (!page?.missing && imageInfo?.url) metadataByTitle.set(titleKey(page.title), imageInfo);
    }
    for (const redirect of result.query?.redirects || []) {
      const target = metadataByTitle.get(titleKey(redirect.to));
      if (target) metadataByTitle.set(titleKey(redirect.from), target);
    }
    if (offset + 50 < titles.length) await sleep(1_500);
  }
  for (const title of titles) {
    if (!metadataByTitle.has(titleKey(title))) throw new Error(`Commons file was not found: ${title}`);
  }
}

async function fetchCommonsImage(photo) {
  if (["flickr", "official", "editorial"].includes(photo.sourceType)) {
    const label = `${photo.sourceType} image download for ${photo.sourceTitle}`;
    const sourceHostname = new URL(photo.sourceUrl).hostname.replace(/^www\./, "");
    const imageResponse = await fetchWithApprovedRedirects(photo.assetUrl, label, (candidate) => {
      if (candidate.protocol !== "https:") return false;
      if (photo.sourceType === "flickr") return candidate.hostname === "live.staticflickr.com";
      const hostIsApproved = photo.sourceType === "official"
        ? isApprovedOfficialHost(candidate.hostname)
        : isApprovedEditorialHost(candidate.hostname);
      return hostIsApproved && candidate.hostname.replace(/^www\./, "") === sourceHostname;
    });
    const bytes = Buffer.from(await imageResponse.arrayBuffer());
    if (bytes.length < 8_000) throw new Error(`Downloaded image is unexpectedly small: ${photo.sourceTitle}`);
    const contentType = imageResponse.headers.get("content-type") || "";
    return { bytes, extension: contentType.includes("png") ? ".png" : ".jpg" };
  }
  const imageInfo = metadataByTitle.get(titleKey(photo.commonsTitle));
  const imageUrl = imageInfo.thumburl || imageInfo.url;
  const imageResponse = await fetchWithApprovedRedirects(
    imageUrl,
    `Image download for ${photo.commonsTitle}`,
    (candidate) => candidate.protocol === "https:" && isApprovedWikimediaHost(candidate.hostname),
  );
  const bytes = Buffer.from(await imageResponse.arrayBuffer());
  if (bytes.length < 8_000) throw new Error(`Downloaded image is unexpectedly small: ${photo.commonsTitle}`);
  const extension = imageInfo.mime === "image/png" ? ".png" : imageInfo.mime === "image/tiff" ? ".tif" : ".jpg";
  return { bytes, extension };
}

async function buildPhoto(item, index) {
  assertSafePhoto(item);
  const output = path.join(projectRoot, item.photo.src);
  if (!force && await isValidWebp(output)) return `kept ${item.photo.src}`;

  const { bytes, extension } = await fetchCommonsImage(item.photo);
  const input = path.join(tempRoot, `${index}${extension}`);
  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(input, bytes);
  const converted = spawnSync("cwebp", ["-quiet", "-q", "80", "-m", "6", input, "-o", output], { encoding: "utf8" });
  if (converted.status !== 0) throw new Error(`cwebp failed for ${item.photo.sourceTitle}: ${converted.stderr || converted.stdout}`);
  const outputBytes = await readFile(output);
  if (outputBytes.subarray(0, 4).toString("ascii") !== "RIFF" || outputBytes.subarray(8, 12).toString("ascii") !== "WEBP") {
    throw new Error(`Invalid WebP output for ${item.photo.sourceTitle}`);
  }
  return `built ${item.photo.src} (${Math.round(outputBytes.length / 1024)} KiB)`;
}

try {
  const cwebp = spawnSync("cwebp", ["-version"], { encoding: "utf8" });
  if (cwebp.status !== 0) throw new Error("cwebp is required to build the trip-map photo set.");
  await loadCommonsMetadata();
  let next = 0;
  async function worker() {
    while (next < queue.length) {
      const index = next++;
      console.log(await buildPhoto(queue[index], index));
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, worker));
  console.log(`Trip-map photo set ready: ${queue.length} images.`);
} finally {
  await rm(tempRoot, { recursive: true, force: true });
}
