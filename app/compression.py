"""
Compression rules: turn the on-screen settings into ffmpeg parameters.

Pure rules only (no ffmpeg, no files): easy to read and to test.

The slider controls quality + size:
  0 %   → CRF 18 (near lossless), original resolution
  60 %  → starts downscaling: up to 720p
  80 %  → up to 540p
  100 % → CRF 40, up to 540p, 96 kbps audio
"720p" applies to the SHORTER side (portrait 1080×1920 → 720×1280).
Never upscales resolution or frame rate. Dimensions are always even (H.264 requires it).

Also:
  - Resolution cap (1080p…144p buttons in the top bar): limits beyond the slider rule.
  - Frame rate: from 1 fps up to the original.
  - Output format: MP4 and MOV (H.264 + AAC) or WebM (VP9 + Opus).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

CRF_H264 = (18.0, 40.0)                       # x264: 0 % → 18, 100 % → 40
CRF_VP9 = (24, 54)                            # VP9 has its own (integer) scale; range picked for similar file sizes
HEIGHT_STEPS = ((80.0, 540), (60.0, 720))     # (from %, max shorter side)
AAC_STEPS = ((80.0, 96), (50.0, 128), (0.0, 160))   # (from %, kbps)
OPUS_FOR_AAC = {160: 128, 128: 96, 96: 64}    # Opus sounds the same with ~25 % less bitrate
RESOLUTION_CAPS = (1080, 720, 480, 360, 240, 144)   # top bar buttons (shorter side)
MIN_FPS = 1.0
SAMPLES, SAMPLE_SECONDS = 6, 2.0              # size estimate: 6 clips of 2 s spread over the video

# Benchmark (55 s 1080p at 70 %): "faster" rendered 30 % quicker than "medium",
# with a 7 % smaller file and only 1.2 VMAF points lower (66.64 → 65.42).
X264_PRESET = "faster"


@dataclass(frozen=True)
class Format:
    extension: str
    video_codec: str
    audio_codec: str
    label: str           # shown in the UI
    preset: str          # encoder speed (same for preview and render: the preview must predict the render)


FORMATS = {
    "mp4": Format(".mp4", "libx264", "aac", "H.264 / AAC", X264_PRESET),
    "mov": Format(".mov", "libx264", "aac", "H.264 / AAC", X264_PRESET),
    "webm": Format(".webm", "libvpx-vp9", "libopus", "VP9 / OPUS", "good · cpu-used 4"),
}
DEFAULT_FORMAT = "mp4"


# ---------------------------------------------------------------------------
# input: what the user picked
# ---------------------------------------------------------------------------

def clamp_pct(pct) -> float:
    """Accepts a number or text; returns 0–100 with two decimals. Invalid values become 0."""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(p):
        return 0.0
    return round(min(100.0, max(0.0, p)), 2)


def _parse_cap(value) -> int | None:
    if value in (None, "", "orig", "auto"):
        return None
    cap = int(float(value))
    if cap not in RESOLUTION_CAPS:
        raise ValueError(f"invalid resolution cap ({value}). Use: {', '.join(map(str, RESOLUTION_CAPS))}")
    return cap


def _parse_fps(value) -> float | None:
    if value in (None, "", "orig"):
        return None
    fps = float(value)
    if math.isnan(fps) or fps <= 0:
        raise ValueError(f"invalid fps ({value})")
    return max(MIN_FPS, round(fps, 3))


def _parse_format(value) -> str:
    fmt = str(value or DEFAULT_FORMAT).lower().lstrip(".")
    if fmt not in FORMATS:
        raise ValueError(f"invalid format ({value}). Use: {', '.join(FORMATS)}")
    return fmt


@dataclass(frozen=True)
class Settings:
    """On-screen settings, already validated. cap/fps None = keep the original."""
    pct: float = 50.0
    cap: int | None = None
    fps: float | None = None
    format: str = DEFAULT_FORMAT

    @classmethod
    def from_request(cls, data: dict) -> "Settings":
        """Reads the browser's JSON or query string. Invalid % becomes 0; invalid cap/fps/format raise (HTTP 400)."""
        return cls(pct=clamp_pct(data.get("pct", 0)), cap=_parse_cap(data.get("cap")),
                   fps=_parse_fps(data.get("fps")), format=_parse_format(data.get("format")))

    def for_video(self, info: dict) -> "Params":
        return params(self.pct, info["width"], info["height"], cap=self.cap, fps=self.fps,
                      source_fps=info.get("fps"), format=self.format)


# ---------------------------------------------------------------------------
# output: ffmpeg parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Params:
    pct: float
    crf: float            # x264 accepts fractional values; VP9 integers only
    width: int            # always even
    height: int           # always even
    audio_kbps: int
    fps: float | None     # None = keep the original
    format: str
    preset: str

    def to_dict(self) -> dict:
        return {**asdict(self), "codec": FORMATS[self.format].label, "extension": FORMATS[self.format].extension}


def round_even(n: float) -> int:
    """Rounds to the nearest even number, minimum 2."""
    return max(2, int(round(n / 2.0)) * 2)


def _crf(pct: float, fmt: str) -> float:
    if FORMATS[fmt].video_codec == "libvpx-vp9":
        lo, hi = CRF_VP9
        return float(round(lo + (hi - lo) * pct / 100.0))
    lo, hi = CRF_H264
    return round(lo + (hi - lo) * pct / 100.0, 2)


def _target_short_side(pct: float, short_side: int, cap: int | None) -> int:
    target = short_side
    for threshold, maximum in HEIGHT_STEPS:
        if pct >= threshold:
            target = min(target, maximum)
            break
    if cap:
        target = min(target, cap)
    return target


def _dimensions(pct: float, width: int, height: int, cap: int | None) -> tuple[int, int]:
    short = min(width, height)
    target = _target_short_side(pct, short, cap)
    if target >= short:
        return round_even(width), round_even(height)
    factor = target / short
    return round_even(width * factor), round_even(height * factor)


def _audio_kbps(pct: float, fmt: str) -> int:
    aac = next(kbps for threshold, kbps in AAC_STEPS if pct >= threshold)
    return OPUS_FOR_AAC[aac] if FORMATS[fmt].audio_codec == "libopus" else aac


def _output_fps(fps: float | None, source_fps: float | None) -> float | None:
    """Never above the original. Equal (or nearly equal) to the original = None (left untouched)."""
    if fps is None or not source_fps:
        return fps
    return None if fps >= source_fps - 0.01 else fps


def params(pct, width: int, height: int, *, cap: int | None = None, fps: float | None = None,
           source_fps: float | None = None, format: str = DEFAULT_FORMAT) -> Params:
    p = clamp_pct(pct)
    w, h = _dimensions(p, width, height, cap)
    return Params(pct=p, crf=_crf(p, format), width=w, height=h, audio_kbps=_audio_kbps(p, format),
                  fps=_output_fps(fps, source_fps), format=format, preset=FORMATS[format].preset)


def encoding_args(par: Params) -> list[str]:
    """ffmpeg video/audio arguments (no input or output). The same for preview, estimate and render."""
    filters = [f"fps={par.fps:g}"] if par.fps else []           # fps before scaling: fewer frames to scale
    filters.append(f"scale={par.width}:{par.height}:flags=lanczos,format=yuv420p")
    fmt = FORMATS[par.format]
    if fmt.video_codec == "libvpx-vp9":
        video = ["-c:v", "libvpx-vp9", "-crf", f"{par.crf:.0f}", "-b:v", "0",
                 "-deadline", "good", "-cpu-used", "4", "-row-mt", "1"]
        audio = ["-c:a", "libopus", "-b:a", f"{par.audio_kbps}k"]
        container = []
    else:
        video = ["-c:v", "libx264", "-preset", par.preset, "-crf", f"{par.crf:.2f}"]
        audio = ["-c:a", "aac", "-b:a", f"{par.audio_kbps}k"]
        container = ["-movflags", "+faststart"]                 # plays on the web before fully downloaded
    return ["-vf", ",".join(filters), *video, *audio, *container]


def output_name(stem: str, par: Params) -> str:
    """clip_c70.00.mp4; with reduced fps: clip_c70.00_12fps.webm."""
    fps = f"_{par.fps:g}fps" if par.fps else ""
    return f"{stem}_c{par.pct:.2f}{fps}{FORMATS[par.format].extension}"


# ---------------------------------------------------------------------------
# numbers shown in the UI
# ---------------------------------------------------------------------------

def sample_starts(duration: float) -> tuple[float, list[float]]:
    """(length of each sample, start times) of the clips used by the size estimate. Short video = 1 clip."""
    length = min(SAMPLE_SECONDS, duration)
    n = 1 if duration <= length * 1.5 else SAMPLES
    return length, [max(0.0, (duration - length) * (i + 0.5) / n) for i in range(n)]


def reduction_pct(original_bytes: int, new_bytes: int) -> float:
    """How much the file shrank, in %, two decimals. Negative = it grew."""
    if original_bytes <= 0:
        return 0.0
    return round((1 - new_bytes / original_bytes) * 100, 2)


def frame_number(seconds: float, fps: float) -> int:
    """Frame (from 0) on screen at time t."""
    return int(math.floor(max(0.0, float(seconds)) * fps + 1e-6))


def timecode(seconds: float, fps: float | None = None) -> str:
    """Same format as the UI. Without fps: 00:00 (or 0:00:00 above 1 h).
    With fps: 00:00:00 = minute:second:frame of the frame on screen (at 23.976 fps, t = 2.0 s is still 00:01:23)."""
    total = max(0.0, float(seconds))
    if fps:
        n = frame_number(total, fps)
        s = int(math.floor(n / fps + 1e-9))
        frame = n - math.ceil(s * fps - 1e-9)        # first frame of second s = ceil(s × fps)
    else:
        s = int(total)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{base}:{frame:02d}" if fps else base
