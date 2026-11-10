"""
Everything that talks to the ffmpeg executable: find it, read video info, grab a frame, encode a clip.

No ffprobe: the imageio-ffmpeg package (installed by the .bat) only ships ffmpeg.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # Windows: no console window per ffmpeg call
FAST_PNG = ["-compression_level", "1"]    # benchmark: 31 % faster than default; PNG ~2× larger, only travels over 127.0.0.1


class FfmpegNotFound(RuntimeError):
    pass


class FfmpegError(RuntimeError):
    pass


def find_ffmpeg() -> str:
    """System ffmpeg (PATH) or the one from the imageio-ffmpeg package."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as e:
        raise FfmpegNotFound("ffmpeg not found. Run OPEN_COMPRESSOR.bat (it installs it) "
                             "or: python -m pip install imageio-ffmpeg") from e


def error_tail(stderr: bytes, limit: int = 400) -> str:
    return stderr.decode("utf-8", errors="replace")[-limit:].strip() or "ffmpeg failed"


# ---------------------------------------------------------------------------
# parsing `ffmpeg -i` output (pure functions, testable without ffmpeg)
# ---------------------------------------------------------------------------

_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_CODEC = re.compile(r"Video:\s*([\w-]+)(?:\s*\(([^)]*)\))?")
_RE_PIXEL = re.compile(r",\s*([a-z0-9_]+)(?:\(([^)]*)\))?\s*,\s*\d{2,5}x\d{2,5}")
_RE_SIZE = re.compile(r",\s*(\d{2,5})x(\d{2,5})")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_RE_TBR = re.compile(r"(\d+(?:\.\d+)?)\s*tbr")
_RE_ROTATION = (re.compile(r"rotation of (-?\d+(?:\.\d+)?)"), re.compile(r"rotate\s*:\s*(-?\d+)"))
DEFAULT_FPS = 25.0


def _chroma(pix_fmt: str) -> tuple[str, int]:
    """yuv420p → ('4:2:0', 8); yuv422p10le → ('4:2:2', 10)."""
    chroma = next((c for key, c in (("444", "4:4:4"), ("422", "4:2:2"), ("420", "4:2:0"), ("nv12", "4:2:0"),
                                    ("p010", "4:2:0"), ("rgb", "RGB"), ("gbr", "RGB"))
                   if key in pix_fmt), "")
    bits = re.search(r"(?:p|le|be)?(1[02]|9)(?:le|be)$", pix_fmt) or re.search(r"p0(1[02])", pix_fmt)
    return chroma, int(bits[1]) if bits else 8


def _color(details: str | None) -> tuple[str | None, str | None]:
    """'tv, bt709, progressive' → ('bt709', 'tv')."""
    space = color_range = None
    for item in (details or "").split(","):
        item = item.strip().split("/")[0]
        if item in ("tv", "pc"):
            color_range = item
        elif item.startswith(("bt", "smpte", "rgb")):
            space = space or item
    return space, color_range


def parse_info(text: str) -> dict:
    """Duration, size, fps, audio, codec and color from the `ffmpeg -i` text."""
    m = _RE_DURATION.search(text)
    if not m:
        raise ValueError("does not look like a video (no duration)")
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    line = next((l for l in text.splitlines() if "Video:" in l and "attached pic" not in l), None)
    size = _RE_SIZE.search(line or "")
    if not size:
        raise ValueError("video has no image track")
    width, height = int(size[1]), int(size[2])
    rate = _RE_FPS.search(line) or _RE_TBR.search(line)
    fps = float(rate[1]) if rate and float(rate[1]) > 0 else DEFAULT_FPS   # no fps in the header: assume 25
    rotation = next((r for r in (p.search(text) for p in _RE_ROTATION) if r), None)
    if rotation and abs(round(float(rotation[1]))) % 180 == 90:    # phone held upright: ffmpeg rotates on decode
        width, height = height, width
    codec = _RE_CODEC.search(line)
    profile = codec[2] if codec and codec[2] and "/" not in codec[2] else None
    pixel = _RE_PIXEL.search(line)
    pix_fmt = pixel[1] if pixel else ""
    chroma, bits = _chroma(pix_fmt)
    space, color_range = _color(pixel[2] if pixel else None)
    return {"duration": round(duration, 3), "width": width, "height": height, "fps": round(fps, 3),
            "audio": bool(re.search(r"Stream #.*Audio:", text)),
            "codec": codec[1] if codec else "?", "profile": profile, "pix_fmt": pix_fmt, "chroma": chroma, "bits": bits,
            "color_space": space, "color_range": color_range}


def parse_version(text: str) -> str:
    m = re.search(r"ffmpeg version (\S+)", text)
    return m[1] if m else "?"


# ---------------------------------------------------------------------------
# calls to the executable
# ---------------------------------------------------------------------------

class Ffmpeg:
    def __init__(self, exe: str):
        self.exe = exe

    def run(self, args: list[str], timeout: float | None = 600) -> subprocess.CompletedProcess:
        return subprocess.run([self.exe, "-hide_banner", "-nostdin", *args], capture_output=True,
                              timeout=timeout, creationflags=NO_WINDOW)

    def start(self, args: list[str]) -> subprocess.Popen:
        """Long-running process (render), with stdout/stderr piped."""
        return subprocess.Popen([self.exe, "-hide_banner", "-nostdin", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=NO_WINDOW)

    def version(self) -> str:
        r = subprocess.run([self.exe, "-version"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return parse_version(r.stdout.decode("utf-8", "replace"))

    def info(self, path: Path) -> dict:
        r = self.run(["-i", str(path)], timeout=60)
        info = parse_info(r.stderr.decode("utf-8", errors="replace"))
        info["bytes"] = path.stat().st_size
        return info

    def frame_png(self, path: Path, t: float, duration: float, fps: float) -> bytes:
        """The frame on screen at time t (number floor(t × fps)), as PNG. Near the end, steps back until one is found.

        ffmpeg's `-ss` returns the first frame with timestamp >= ss. Asking half a frame earlier keeps the
        3-decimal rounding of t from jumping to the next frame, and matches the frame number shown in the UI."""
        t = max(0.0, min(t, max(0.0, duration - 1.0 / fps)))
        t = max(0.0, t - 0.5 / fps)
        r = None
        for attempt in (t, t - 0.5, t - 1.0, 0.0):
            r = self.run(["-ss", f"{max(0.0, attempt):.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "image2pipe", "-c:v", "png", *FAST_PNG, "-"], timeout=60)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        raise FfmpegError("could not read the frame: " + error_tail(r.stderr, 300))

    @contextmanager
    def encoded_clip(self, source: Path, start: float, duration: float, encoding_args: list[str],
                     tmp_dir: Path, extension: str) -> Iterator[Path]:
        """Encodes a clip into a temporary file and deletes it when the `with` block ends."""
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out = tmp_dir / f"prev_{uuid.uuid4().hex}{extension}"
        try:
            r = self.run(["-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
                          *encoding_args, str(out)], timeout=300)
            if r.returncode != 0 or not out.exists():
                raise FfmpegError(error_tail(r.stderr))
            yield out
        finally:
            out.unlink(missing_ok=True)
