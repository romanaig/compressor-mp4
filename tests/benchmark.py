#!/usr/bin/env python3
"""
Compressor MP4 benchmark: how much of the machine each operation uses.

Measures through the real API (server in the same process, temporary folders):
  wall time · whole-machine CPU average and peak · RAM of this process + child ffmpegs · concurrent ffmpegs.
With --variants, also compares rendering with x264 (medium/faster/veryfast) and on the GPU (NVENC), with VMAF.

Usage: python tests/benchmark.py VIDEO.mp4 [--pct 70] [--format mp4] [--variants]
Needs: pip install psutil. Writes tests/history/benchmark_<date>.md and .json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import compression as cz              # noqa: E402
import server as sv                   # noqa: E402
from ffmpeg_engine import NO_WINDOW, find_ffmpeg  # noqa: E402
from files import Folders             # noqa: E402

THREADS = psutil.cpu_count(logical=True)


class Meter:
    """Samples whole-machine CPU and the RAM of the process tree (this + ffmpegs) every 100 ms."""

    def __init__(self, name: str):
        self.name = name
        self._stop = threading.Event()
        self.cpu, self.rss, self.ffmpegs = [], [], []

    def _sample(self):
        me = psutil.Process()
        psutil.cpu_percent(None)
        while not self._stop.wait(0.1):
            self.cpu.append(psutil.cpu_percent(None))
            tree = [me]
            try:
                tree += me.children(recursive=True)
            except psutil.Error:
                pass
            total, ffmpegs = 0, 0
            for p in tree:
                try:
                    total += p.memory_info().rss
                    ffmpegs += p.name().lower().startswith("ffmpeg")
                except psutil.Error:
                    pass
            self.rss.append(total)
            self.ffmpegs.append(ffmpegs)

    def __enter__(self):
        self.t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self.wall = time.perf_counter() - self.t0
        self._stop.set()
        self._thread.join()
        return False

    def summary(self, **extra) -> dict:
        cpu = self.cpu or [0.0]
        avg = sum(cpu) / len(cpu)
        return {"operation": self.name, "time_s": round(self.wall, 3), "cpu_avg_pct": round(avg, 1),
                "cpu_peak_pct": round(max(cpu), 1), "busy_threads": round(avg * THREADS / 100, 1),
                "ram_peak_mb": round(max(self.rss or [0]) / 1048576, 1), "concurrent_ffmpeg": max(self.ffmpegs or [0]),
                **extra}


def http(base, method, path, body=None, headers=None):
    data, h = None, dict(headers or {})
    if isinstance(body, dict):
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    elif isinstance(body, bytes):
        data = body
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method, headers=h), timeout=900) as r:
        raw = r.read()
        return json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw


def measure_app(video: Path, settings: dict, folders: Folders) -> tuple[list[dict], dict]:
    srv = sv.start(28765, folders=folders)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    res = []
    try:
        with Meter("idle (server waiting, 3 s)") as m:
            time.sleep(3)
        res.append(m.summary())

        body = video.read_bytes()
        with Meter("open video (copy + read)") as m:
            info = http(base, "POST", "/api/upload", body, {"X-Filename": urllib.parse.quote(video.name)})
        res.append(m.summary(mb=round(len(body) / 1048576, 2)))
        del body
        name, dur = info["name"], info["duration"]
        times = [dur * f for f in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95)]

        for label in ("raw PNG frame (10 requests)", "raw PNG frame (same 10, cached)"):
            latencies = []
            with Meter(label) as m:
                for t in times:
                    t0 = time.perf_counter()
                    png = http(base, "GET", f"/api/frame?v={urllib.parse.quote(name)}&t={t:.3f}")
                    latencies.append(time.perf_counter() - t0)
            res.append(m.summary(avg_latency_s=round(sum(latencies) / len(latencies), 3), png_kb=round(len(png) / 1024)))

        with Meter(f"preview {settings['pct']:.2f} %") as m:
            http(base, "POST", "/api/preview", {"v": name, "t": dur / 2, **settings})
        res.append(m.summary())

        with Meter(f"estimate {settings['pct']:.2f} % (first time)") as m:
            est = http(base, "POST", "/api/estimate", {"v": name, **settings})
        res.append(m.summary(estimate_mb=round(est["estimate_bytes"] / 1048576, 2)))

        with Meter(f"estimate {settings['pct']:.2f} % (cached)") as m:
            http(base, "POST", "/api/estimate", {"v": name, **settings})
        res.append(m.summary())

        with Meter(f"render {settings['pct']:.2f} % ({settings['format']})") as m:
            job = http(base, "POST", "/api/render", {"v": name, **settings})["job"]
            while (j := http(base, "GET", f"/api/render?job={job}"))["state"] == "running":
                time.sleep(0.25)
        if j["state"] != "done":
            raise RuntimeError(j["error"])
        res.append(m.summary(output_mb=round(j["bytes"] / 1048576, 2), frames_per_s=round(dur * info["fps"] / m.wall, 1),
                             estimate_error_pct=round((est["estimate_bytes"] / j["bytes"] - 1) * 100, 2)))
    finally:
        srv.shutdown()
    return res, info


def vmaf(exe: str, original: Path, compressed: Path, width: int, height: int) -> float | None:
    """VMAF of the compressed file (scaled back to the original size) against the original; 1 of every 5 frames."""
    graph = (f"[0:v]scale={width}:{height}:flags=bicubic,setpts=PTS-STARTPTS[d];"
             f"[1:v]setpts=PTS-STARTPTS[r];[d][r]libvmaf=n_subsample=5:n_threads={THREADS}")
    r = subprocess.run([exe, "-hide_banner", "-nostdin", "-i", str(compressed), "-i", str(original), "-lavfi", graph,
                        "-f", "null", "-"], capture_output=True, creationflags=NO_WINDOW)
    m = re.search(r"VMAF score:\s*([\d.]+)", r.stderr.decode("utf-8", "replace"))
    return round(float(m[1]), 2) if m else None


def measure_render_variants(exe: str, video: Path, info: dict, pct: float, tmp: Path) -> list[dict]:
    par = cz.params(pct, info["width"], info["height"])
    scale = f"scale={par.width}:{par.height}:flags=lanczos,format=yuv420p"
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", f"{round(par.crf)}", "-b:v", "0"]
    variants = [("x264 medium", [], ["-c:v", "libx264", "-preset", "medium", "-crf", f"{par.crf:.2f}"]),
                ("x264 faster (app)", [], ["-c:v", "libx264", "-preset", "faster", "-crf", f"{par.crf:.2f}"]),
                ("x264 veryfast", [], ["-c:v", "libx264", "-preset", "veryfast", "-crf", f"{par.crf:.2f}"]),
                ("NVENC H.264 p5 (GPU)", [], nvenc),
                ("NVENC + GPU decoding", ["-hwaccel", "cuda"], nvenc)]
    res = []
    for label, before, codec in variants:
        out = tmp / f"r_{uuid.uuid4().hex}.mp4"
        with Meter(f"render: {label}") as m:
            r = subprocess.run([exe, "-hide_banner", "-nostdin", "-y", *before, "-i", str(video), "-vf", scale, *codec,
                                "-c:a", "aac", "-b:a", f"{par.audio_kbps}k", str(out)], capture_output=True, creationflags=NO_WINDOW)
        if r.returncode != 0:
            res.append({"operation": f"render: {label}", "error": r.stderr.decode("utf-8", "replace")[-200:]})
            continue
        res.append(m.summary(output_mb=round(out.stat().st_size / 1048576, 2),
                             frames_per_s=round(info["duration"] * info["fps"] / m.wall, 1),
                             vmaf=vmaf(exe, video, out, info["width"], info["height"])))
        out.unlink(missing_ok=True)
    return res


def table(rows: list[dict]) -> str:
    columns = ["operation", "time_s", "cpu_avg_pct", "cpu_peak_pct", "busy_threads", "ram_peak_mb", "concurrent_ffmpeg"]
    extras = sorted({k for r in rows for k in r} - set(columns))
    head = columns + extras
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(r.get(c, "")) for c in head) + " |" for r in rows]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--pct", type=float, default=70)
    ap.add_argument("--format", default="mp4", choices=list(cz.FORMATS))
    ap.add_argument("--variants", action="store_true", help="compare x264 presets and NVENC (with VMAF)")
    a = ap.parse_args()
    video = Path(a.video)
    tmp = Path(tempfile.mkdtemp(prefix="compressor_bench_"))
    try:
        app, info = measure_app(video, {"pct": a.pct, "format": a.format}, Folders(tmp / "i", tmp / "o", tmp / "t"))
        variants = measure_render_variants(find_ffmpeg(), video, info, a.pct, tmp) if a.variants else []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    machine = (f"{psutil.cpu_count(False)} cores / {THREADS} threads · "
               f"{psutil.virtual_memory().total / 1024 ** 3:.1f} GB RAM · {sys.platform}")
    now = dt.datetime.now()
    text = (f"# Benchmark — {now:%Y-%m-%d %H:%M}\n\n- Machine: {machine}\n"
            f"- Video: {info['width']}×{info['height']} · {info['fps']} fps · "
            f"{info['duration']:.2f} s · {info['bytes'] / 1048576:.2f} MB\n- Settings: {a.pct:.2f} % · {a.format}\n\n"
            f"## App\n\n{table(app)}\n")
    if variants:
        text += f"\n## Render: alternatives\n\n{table(variants)}\n"
    print(text)
    history = ROOT / "tests" / "history"
    history.mkdir(parents=True, exist_ok=True)
    (history / f"benchmark_{now:%Y-%m-%d_%H%M}.md").write_text(text, encoding="utf-8")
    (history / f"benchmark_{now:%Y-%m-%d_%H%M}.json").write_text(
        json.dumps({"machine": machine, "video": info, "app": app, "variants": variants}, ensure_ascii=False, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
