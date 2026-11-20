#!/usr/bin/env node
// The demo's three artefacts from one recorded take (scripts/record_demo.mjs):
//
//   docs/demo/flightops-demo.mp4   h264, yuv420p, 30 fps, 1280x720, faststart  (not committed)
//   docs/demo/flightops-demo.gif   12 fps, 900 px wide, palette-optimised, at most 5 MB
//   docs/demo/poster.png           the approaching list with the globe in view
//
//   node scripts/make_demo_video.mjs             # a replayed take, encoded
//   node scripts/make_demo_video.mjs --live      # a new recording first, then the same
//   node scripts/make_demo_video.mjs --encode    # encode the take already in docs/demo/raw
//
// --live spends OpenSky credits (about 10) and needs credentials in the .env of DEMO_ENV_DIR
// (default: the repository root). Use it when the board's requests have changed; a replay that
// meets a request the recording does not hold fails and says so.
//
// Nothing is burned in: no title, no watermark. The page is the artefact. Prints each file's
// size and duration and the frames ffmpeg dropped, and fails when the GIF is over 5 MB.

import { spawnSync } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const RAW = resolve(REPO_ROOT, process.env.DEMO_OUT || "docs/demo/raw");
const DEMO = join(REPO_ROOT, "docs", "demo");
const WEBM = join(RAW, "flightops-demo.webm");
const MP4 = join(DEMO, "flightops-demo.mp4");
const GIF = join(DEMO, "flightops-demo.gif");
const POSTER = join(DEMO, "poster.png");
const GIF_LIMIT = 5 * 1024 * 1024;

function run(command, args, { quiet = false } = {}) {
  const result = spawnSync(command, args, { cwd: REPO_ROOT, encoding: "utf8", stdio: quiet ? "pipe" : "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited with ${result.status}${quiet ? `:\n${result.stderr.slice(-2000)}` : ""}`);
  }
  return result;
}

// ffmpeg's own count of what it dropped and duplicated, from its last progress line.
function ffmpeg(args) {
  const result = run("ffmpeg", ["-hide_banner", "-nostdin", "-y", ...args], { quiet: true });
  const stats = [...result.stderr.matchAll(/dup=(\d+)\s+drop=(\d+)/g)].at(-1);
  return stats ? { dup: Number(stats[1]), drop: Number(stats[2]) } : { dup: 0, drop: 0 };
}

function probe(file) {
  const { stdout } = run(
    "ffprobe",
    ["-v", "error", "-show_entries", "format=duration:stream=width,height,codec_name,pix_fmt,r_frame_rate,nb_frames",
      "-of", "json", file],
    { quiet: true },
  );
  return JSON.parse(stdout);
}

function mark(timeline, name) {
  const found = timeline.marks.find((entry) => entry.name === name);
  if (!found) throw new Error(`the timeline has no "${name}" mark: re-record the take`);
  return found.at_s;
}

function report(file, extra = "") {
  const bytes = statSync(file).size;
  const info = probe(file);
  const duration = Number(info.format.duration);
  const stream = info.streams[0];
  const shape = `${stream.width}x${stream.height} ${stream.codec_name}${stream.pix_fmt ? ` ${stream.pix_fmt}` : ""}`;
  const time = Number.isFinite(duration) && duration > 0 ? `${duration.toFixed(2)} s` : "still";
  console.log(`${relative(REPO_ROOT, file).replaceAll("\\", "/").padEnd(30)} ${(bytes / 1024 / 1024).toFixed(2).padStart(6)} MB  ${time.padStart(8)}  ${shape}${extra}`);
  return bytes;
}

function main() {
  const wall = Date.now();
  const mode = process.argv[2];
  if (mode && !["--live", "--encode"].includes(mode)) throw new Error(`unknown option ${mode}`);
  if (mode === "--live") run("node", [join("scripts", "record_demo.mjs"), "--live"]);
  if (mode !== "--encode") run("node", [join("scripts", "record_demo.mjs")]);
  const timeline = JSON.parse(readFileSync(join(RAW, "timeline.json"), "utf8"));
  // The take starts at page load; the video starts when the tour does, and ends with it.
  const start = mark(timeline, "start");
  const length = mark(timeline, "end") - start;
  const poster = mark(timeline, "poster") - start + 1.5;
  const cut = ["-ss", start.toFixed(3), "-t", length.toFixed(3), "-i", WEBM];

  const mp4 = ffmpeg([
    ...cut, "-an", "-vf", "fps=30,scale=1280:720:flags=lanczos,format=yuv420p",
    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-profile:v", "high",
    "-movflags", "+faststart", MP4,
  ]);
  // From the MP4, so the GIF and the poster show exactly the frames the video does.
  // The board holds still between actions, so most 12 fps frames repeat the one before:
  // mpdecimate merges a repeat into the previous frame's delay, which keeps the timing and
  // took the GIF from 7.6 MB to 3.2 MB. Its thresholds are low enough to keep every frame
  // of the 400 ms click ring. The palette is built from whole frames, so what holds still
  // (the map's land, nearly the colour of the water) is counted as much as what moves.
  // Dithering added size and no legibility.
  const gif = ffmpeg([
    "-i", MP4, "-filter_complex",
    "fps=12,mpdecimate=hi=256:lo=128:frac=0.05,scale=900:-2:flags=lanczos,split[a][b];"
      + "[a]palettegen=max_colors=128:stats_mode=full[p];"
      + "[b][p]paletteuse=dither=none:diff_mode=rectangle",
    "-fps_mode", "vfr", "-loop", "0", GIF,
  ]);
  ffmpeg(["-ss", poster.toFixed(3), "-i", MP4, "-frames:v", "1", "-update", "1", POSTER]);

  report(MP4, `  30 fps, dropped ${mp4.drop}, duplicated ${mp4.dup}`);
  // ffmpeg's drop count does not see frames merged inside a filter, so count what is left.
  const { stdout: kept } = run(
    "ffprobe", ["-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", GIF],
    { quiet: true },
  );
  const gifBytes = report(
    GIF,
    `  12 fps timeline, ${kept.trim()} distinct frames, dropped ${gif.drop}, duplicated ${gif.dup}`,
  );
  report(POSTER, `  at ${poster.toFixed(2)} s`);
  console.log(`done in ${((Date.now() - wall) / 1000).toFixed(1)} s`);
  if (gifBytes > GIF_LIMIT) {
    throw new Error(`the GIF is ${(gifBytes / 1024 / 1024).toFixed(2)} MB, over the 5 MB limit`);
  }
}

try {
  main();
} catch (error) {
  console.error(`FAILED: ${error.message}`);
  process.exitCode = 1;
}
