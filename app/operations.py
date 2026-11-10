"""
What the app does with a video from input/: info, frame, preview, size estimate and render.

The HTTP server only translates requests into these methods.
"""
from __future__ import annotations

import base64
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import compression as cz
from cache import LRUCache
from ffmpeg_engine import Ffmpeg, FfmpegError, error_tail
from files import Folders, free_name, path_inside

PREVIEW_CLIP_SECONDS = 2.0        # the preview frame comes from the middle of a 2 s clip (the encoder sees before and after)


class Renders:
    """Running and finished renders. Keeps only the latest ones (the browser polls the state every 0.5 s)."""

    KEEP = 50

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def reserve(self, target: Path) -> tuple[str, Path]:
        """Picks a free name (not on disk, not used by another running render) and registers the job."""
        with self._lock:
            running = {j["output"] for j in self._jobs.values() if j["state"] == "running"}
            target = free_name(target, running)
            job = uuid.uuid4().hex[:12]
            self._jobs[job] = {"state": "running", "progress": 0.0, "output": target.name, "bytes": 0,
                               "reduction_pct": None, "error": None, "started": time.time()}
            self._prune()
        return job, target

    def update(self, job: str, **fields) -> None:
        with self._lock:
            self._jobs[job].update(fields)

    def state(self, job: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job)
            return dict(j) if j else None

    def _prune(self) -> None:
        finished = [k for k, j in self._jobs.items() if j["state"] != "running"]
        for k in finished[:max(0, len(self._jobs) - self.KEEP)]:
            del self._jobs[k]


class Compressor:
    def __init__(self, ffmpeg: Ffmpeg, folders: Folders):
        self.ff = ffmpeg
        self.folders = folders
        self.renders = Renders()
        self._infos = LRUCache(64)
        self._estimates = LRUCache(256)       # depends only on the video and the settings: repeating is instant
        self._frames = LRUCache(48)           # 1080p PNG ~1.5 MB each: ~70 MB at most

    # ---------- input video ----------

    def path(self, name: str) -> Path:
        return path_inside(self.folders.input, name)

    def info_for(self, path: Path) -> dict:
        key = (str(path), path.stat().st_mtime, path.stat().st_size)
        return self._infos.get(key, lambda: self._read_info(path))

    def _read_info(self, path: Path) -> dict:
        info = self.ff.info(path)
        length, starts = cz.sample_starts(info["duration"])
        info["samples"] = [round(s + length / 2, 3) for s in starts]   # center of each clip (timeline markers)
        info["sample_seconds"] = length
        return info

    def info(self, name: str) -> dict:
        path = self.path(name)
        return {"name": path.name, **self.info_for(path)}

    def params(self, name: str, settings: cz.Settings) -> cz.Params:
        return settings.for_video(self.info_for(self.path(name)))

    def frame(self, name: str, t: float) -> bytes:
        path = self.path(name)
        info = self.info_for(path)
        t = t if math.isfinite(t) else 0.0
        key = (str(path), path.stat().st_mtime, round(t, 3))
        return self._frames.get(key, lambda: self.ff.frame_png(path, t, info["duration"], info["fps"]))

    # ---------- preview: one compressed frame ----------

    def preview(self, name: str, t: float, settings: cz.Settings) -> dict:
        path = self.path(name)
        info = self.info_for(path)
        par = settings.for_video(info)
        dur, fps = info["duration"], info["fps"]
        clip = min(PREVIEW_CLIP_SECONDS, dur)
        t = max(0.0, min(float(t), max(0.0, dur - 1.0 / fps)))
        start = max(0.0, min(t - clip / 2, dur - clip))
        extension = cz.FORMATS[par.format].extension
        with self.ff.encoded_clip(path, start, clip, cz.encoding_args(par), self.folders.tmp, extension) as encoded:
            png = self.ff.frame_png(encoded, t - start, clip, fps)
        return {"img": "data:image/png;base64," + base64.b64encode(png).decode(),
                "t": round(t, 3), "pct": par.pct, "params": par.to_dict(), "original_bytes": info["bytes"]}

    # ---------- size estimate ----------

    def estimate(self, name: str, settings: cz.Settings) -> dict:
        """Encodes clips spread over the video (in parallel) and extrapolates to the whole video."""
        path = self.path(name)
        info = self.info_for(path)
        par = settings.for_video(info)
        key = (str(path), path.stat().st_mtime, par)
        return self._estimates.get(key, lambda: self._estimate(path, info, par))

    def _estimate(self, path: Path, info: dict, par: cz.Params) -> dict:
        dur = info["duration"]
        length, starts = cz.sample_starts(dur)
        args = cz.encoding_args(par)
        extension = cz.FORMATS[par.format].extension

        def sample_size(start: float) -> int:
            with self.ff.encoded_clip(path, start, length, args, self.folders.tmp, extension) as encoded:
                return encoded.stat().st_size

        # automatic threads in each ffmpeg: the benchmark showed that capping threads is slower
        with ThreadPoolExecutor(max_workers=max(1, min(len(starts), os.cpu_count() or 2))) as ex:
            sizes = list(ex.map(sample_size, starts))
        estimated = int(sum(sizes) / (length * len(sizes)) * dur)
        return {"pct": par.pct, "original_bytes": info["bytes"], "estimate_bytes": estimated,
                "reduction_pct": cz.reduction_pct(info["bytes"], estimated), "samples": len(starts),
                "params": par.to_dict()}

    # ---------- render the whole file ----------

    def start_render(self, name: str, settings: cz.Settings) -> str:
        path = self.path(name)
        info = self.info_for(path)
        par = settings.for_video(info)
        self.folders.output.mkdir(parents=True, exist_ok=True)
        job, target = self.renders.reserve(self.folders.output / cz.output_name(path.stem, par))
        threading.Thread(target=self._render, args=(job, path, info, par, target), daemon=True).start()
        return job

    def _render(self, job: str, source: Path, info: dict, par: cz.Params, target: Path) -> None:
        partial = target.with_name(target.stem + ".partial" + target.suffix)
        try:
            proc = self.ff.start(["-y", "-i", str(source), *cz.encoding_args(par),
                                  "-progress", "pipe:1", "-nostats", str(partial)])
            errors: list[bytes] = []
            reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
            reader.start()                                   # read stderr alongside: if the pipe fills up, ffmpeg hangs
            for line in iter(proc.stdout.readline, b""):
                done = re.match(rb"out_time_(?:us|ms)=(\d+)", line)
                if done and info["duration"] > 0:
                    seconds = int(done[1]) / 1_000_000
                    self.renders.update(job, progress=round(min(99.99, seconds / info["duration"] * 100), 2))
            proc.wait()
            reader.join(timeout=5)                           # wait for stderr: without this the error came back empty
            if proc.returncode != 0:
                raise FfmpegError(error_tail(errors[0] if errors else b""))
            os.replace(partial, target)
            size = target.stat().st_size
            self.renders.update(job, state="done", progress=100.0, bytes=size,
                                reduction_pct=cz.reduction_pct(info["bytes"], size))
        except Exception as e:                               # any failure becomes an "error" state in the UI
            partial.unlink(missing_ok=True)
            self.renders.update(job, state="error", error=str(e))

    def render_state(self, job: str) -> dict | None:
        return self.renders.state(job)
