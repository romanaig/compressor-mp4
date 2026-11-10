"""App folders and safe file names (never leave input/, never overwrite)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIDEO_EXTENSIONS = (".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm")


@dataclass(frozen=True)
class Folders:
    input: Path        # dropped videos (copies)
    output: Path       # compressed videos
    tmp: Path          # preview/estimate clips

    @classmethod
    def default(cls) -> "Folders":
        return cls(ROOT / "input", ROOT / "output", ROOT / "app" / "tmp")

    def create(self) -> None:
        for folder in (self.input, self.output, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)

    def clean_tmp(self) -> None:
        for old in self.tmp.glob("prev_*"):
            old.unlink(missing_ok=True)


def safe_name(name: str) -> str:
    """Name only (no folder), no characters Windows forbids, with an accepted video extension."""
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    if ext.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"format not accepted ({ext or 'no extension'}). Use: {', '.join(VIDEO_EXTENSIONS)}")
    return (stem[:120] or "video") + ext.lower()


def path_inside(folder: Path, name: str) -> Path:
    """A file that exists directly in `folder`: no subfolder, no '..'."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise FileNotFoundError(name)
    path = folder / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def free_name(target: Path, reserved: set[str] = frozenset()) -> Path:
    """target, or target_2, target_3… if it already exists on disk or is reserved (render in progress)."""
    candidate, i = target, 2
    while candidate.exists() or candidate.name in reserved:
        candidate = target.with_name(f"{target.stem}_{i}{target.suffix}")
        i += 1
    return candidate
