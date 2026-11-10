// Number and time formats. Same as app/compression.py (the regression test compares both).

const twoDigits = (n) => String(n).padStart(2, "0");

/** Frame number (from 0) on screen at time t. */
export const frameNumber = (t, fps) => Math.floor(Math.max(0, Number(t) || 0) * fps + 1e-6);

/** 00:12 (or 1:02:05). With fps: 00:12:07 = minute:second:frame of the frame on screen. */
export function timecode(seconds, fps = null) {
  const total = Math.max(0, Number(seconds) || 0);
  let s, frame;
  if (fps) {
    const n = frameNumber(total, fps);
    s = Math.floor(n / fps + 1e-9);
    frame = n - Math.ceil(s * fps - 1e-9);                  // first frame of second s = ceil(s × fps)
  } else {
    s = Math.floor(total);
  }
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const base = h ? `${h}:${twoDigits(m)}:${twoDigits(s)}` : `${twoDigits(m)}:${twoDigits(s)}`;
  return fps ? `${base}:${twoDigits(frame)}` : base;
}

/** 0–100 with two decimals; invalid becomes 0. */
export function clampPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.round(Math.min(100, Math.max(0, n)) * 100) / 100;
}

export const mb = (bytes) => (bytes / 1048576).toFixed(2) + " MB";

/** −93.39 % (shrank) or +4.10 % (grew). */
export const reduction = (pct) => `${pct >= 0 ? "−" : "+"}${Math.abs(pct).toFixed(2)} %`;

/** 25 → "25"; 23.976 → "23.98". */
export const shortFps = (fps) => (Number.isInteger(fps) ? String(fps) : fps.toFixed(2));

const COLOR_NAMES = { bt709: "Rec.709", bt2020nc: "Rec.2020", bt2020c: "Rec.2020", bt2020: "Rec.2020",
                      smpte170m: "Rec.601", bt470bg: "Rec.601", smpte240m: "SMPTE 240M", rgb: "RGB" };
const RANGE_NAMES = { tv: "TV (limited)", pc: "PC (full)" };

export function colorInfo(info) {
  const space = info.color_space ? (COLOR_NAMES[info.color_space] || info.color_space) : "not tagged";
  const range = info.color_range ? RANGE_NAMES[info.color_range] : "not tagged";
  return `Color space: ${space} · Range: ${range}`;
}

/** "H.264 High · 4:2:0" or "ProRes HQ · 4:2:2 10-bit". */
export function describeCodec(info) {
  const names = { h264: "H.264", hevc: "H.265", prores: "ProRes", vp9: "VP9", av1: "AV1", mpeg4: "MPEG-4" };
  const codec = [names[info.codec] || info.codec, info.profile].filter(Boolean).join(" ");
  const chroma = [info.chroma, info.bits > 8 ? `${info.bits}-bit` : ""].filter(Boolean).join(" ");
  return [codec, chroma].filter(Boolean).join(" · ");
}
