// Timeline drawing: time marks, size-estimate samples and the played portion.
import { timecode } from "./format.js";

const STEPS_S = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const MAX_MARKS = 6;

const pct = (t, duration) => `${duration > 0 ? (t / duration) * 100 : 0}%`;

/** 00:00 · 00:10 · … · end. The step grows with the video so there are never more than ~6 marks. */
export function drawTimeMarks(box, duration) {
  box.replaceChildren();
  if (!(duration > 0)) return;
  const step = STEPS_S.find((s) => duration / s <= MAX_MARKS) || 3600;
  const times = [];
  for (let t = 0; t < duration; t += step) times.push(t);
  if (duration - times[times.length - 1] < step * 0.4 && times.length > 1) times.pop();   // keep away from the end
  for (const t of [...times, duration]) {
    const mark = document.createElement("span");
    mark.textContent = timecode(t);
    mark.style.left = pct(t, duration);
    if (t === duration) mark.classList.add("end");
    box.appendChild(mark);
  }
}

/** One marker per clip used by the size estimate (width = clip length). */
export function drawSamples(box, info) {
  box.replaceChildren();
  for (const center of info.samples || []) {
    const m = document.createElement("i");
    m.style.left = pct(center, info.duration);
    m.style.width = pct(info.sample_seconds, info.duration);
    box.appendChild(m);
  }
}

/** Index (from 0) of the sample that contains time t, or -1. */
export function sampleAt(t, info) {
  const half = (info.sample_seconds || 0) / 2;
  return (info.samples || []).findIndex((c) => t >= c - half && t <= c + half);
}

export function markActiveSample(box, index) {
  [...box.children].forEach((m, i) => m.classList.toggle("active", i === index));
}

export function showPlayed(bar, t, duration) {
  bar.style.width = pct(t, duration);
}
