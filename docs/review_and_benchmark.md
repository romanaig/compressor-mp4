# Code review and benchmark

This document records the clean-code review of the first version, the performance measurements, and the decisions taken for version 2.

## Decisions for v2

| Topic | Decision |
|---|---|
| Review findings | Fixed together with the new design |
| Performance | Only gains that do not change the output file, plus x264 `preset faster` after measuring it |
| ⏪ ▶ ⏩ | ▶ plays the original on the Raw screen; ⏪ ⏩ step one frame |
| "FORMAT 1080p…144p" in the top bar | Resolution **cap**: output = smallest of original, cap and slider rule |
| Frame rate | From 1 fps up to the original; never raised |
| WebM | VP9 + Opus |
| Slider | 0–100 % |

---

## 1. Code review

### Kept (already good)

- Slider rules isolated in a pure module (no ffmpeg, no files): easy to read and test.
- Comments explain **why** (slider rule, `SO_REUSEADDR` on Windows, reading stderr in parallel).
- Path safety: nothing leaves `input/`; atomic writes (`.partial` + `os.replace`); files are never overwritten.
- A solid regression suite.

### Findings and fixes

| # | Where | Problem | Fix |
|---|---|---|---|
| 1 | server (469 lines) | One file did everything: ffmpeg, files, caches, preview/estimate/render, render queue, HTTP and CLI | Split by responsibility: `ffmpeg_engine.py`, `files.py`, `cache.py`, `operations.py`; `server.py` only handles HTTP |
| 2 | server | Mutable globals (`FFMPEG = None`; tests patched the folder paths from outside) | `Folders` (dataclass), `Ffmpeg` and `Compressor` created in `start()` and passed down. No globals |
| 3 | preview / estimate | The same "encode clip → check error → delete" block written twice | `Ffmpeg.encoded_clip()` context manager |
| 4 | upload / render | Two different "find a free file name" implementations | One: `free_name(target, reserved)` |
| 5 | info / estimate / jobs | Hand-made caches with no limit: grew as long as the app stayed open | `LRUCache` with a limit; `Renders` keeps the last 50 |
| 6 | `do_GET` / `do_POST` | Long `if/elif` chains and duplicated error handling | Route table + a single `_dispatch()` |
| 7 | Names | `L`, `k`, `par_` (next to a function `par()`), `inf`, `q`, `r` | Descriptive names (`sample length`, `starts`, `round_even`, `Params.to_dict`…) |
| 8 | ffmpeg lookup | `sys.exit` inside a library function; broad `except Exception` | Raises `FfmpegNotFound`; only `main()` decides to exit |
| 9 | Video info | Parsing mixed with the process call | `parse_info(text)` is pure and tested with ProRes 4:2:2 10-bit and rotated-phone samples |
| 10 | Slider rules | CRF, resolution and audio in one function; magic numbers (160/128/96, 50/80) | `_crf`, `_dimensions`, `_audio_kbps`, `_output_fps`; steps as constants |
| 11 | UI (409-line `index.html`) | CSS + HTML + JS in one file; lists copied from Python (extensions, steps) | `app/web/` with `index.html`, `style.css`, `js/` (`app`, `api`, `format`, `timeline`). Lists come from `/api/config` |
| 12 | JS | Loose variables; state fields created mid-file; polling loop inside the click handler; a fake event to leave zoom | Single state object declared at the top; `followRender()`; `exitZoom()` |
| 13 | Security | Any website open in the browser could POST to `127.0.0.1:8765` (start a render, open a folder). No DNS-rebinding defense | `Host` and `Origin` are checked: foreign requests get 403 |
| 14 | Frame / timecode | `-ss` with 3 decimals could jump to the next frame (23.976 fps); the new `mm:ss:frame` timecode had to match the frame number | Seek half a frame early; timecode derived from the frame number, identical in Python and JS (a test compares both) |
| 15 | Tests | UI tests only on Playwright's bundled Chromium | Uses system Edge/Chrome; 36 → 55 tests |

### Left out (noted, not urgent)

- No "cancel render" button.
- The preview sends the PNG as base64 inside JSON (+33 % bytes). On `127.0.0.1` the gain from changing it is small.
- Uploading copies the video into `input/` because browsers do not expose file paths. For multi-GB files, an "open directly from input/" mode would avoid the copy.

---

## 2. Benchmark

- **Machine:** 8-core / 16-thread desktop CPU, 32 GB RAM, NVIDIA RTX 2060, Windows 11, ffmpeg 9.0.2.
- **Video:** 55 s, 1920×1080, 23.98 fps, 101.71 MB. Setting: 70 % (output 1280×720, CRF 33.40).
- **Method:** `tests/benchmark.py` — whole-machine CPU and RAM of the server + ffmpegs, sampled every 100 ms. CPU includes other running programs (5–14 % at idle), so ±10 % differences in render time are noise.

### How much of the machine it uses (final version)

| Operation | Time | CPU (16 threads) | Peak RAM | Notes |
|---|---|---|---|---|
| Idle | — | ~0 % from the app | 30 MB | No ffmpeg running |
| Open video (101 MB) | 0.23 s | 14 % | 150 MB | Copy to `input/` + read |
| Timeline frame | 0.18 s | 28 % (4 threads) | 185 MB | One ffmpeg per frame; repeats come from cache (0.002–0.007 s) |
| Preview | 0.46 s | 47 % (7 threads) | 370 MB | |
| Estimate (first time per setting) | 1.35 s | **93–100 %** | **1.5–2.1 GB** | 6 ffmpegs in parallel, on purpose |
| Render | 5.9 s | 79 % (13 threads) | 520 MB | ~225 frames/s → 10 min of 1080p ≈ 1 min |

Idle, the app costs nothing. It uses the whole machine in short bursts (that is what makes preview and estimate fast). The heaviest point is the estimate's RAM (up to ~2 GB for ~1.5 s).

### Before × after

| Operation | v1 | v2 | Why |
|---|---|---|---|
| Timeline frame | 0.27 s | **0.18 s (−33 %)** | PNG compression level 1 (file ~2× larger, only travels over 127.0.0.1) |
| Same frame again | 0.27 s | **0.002 s** | Cache of the last 48 frames |
| Preview | 0.57 s · 3 ffmpeg | **0.46 s (−19 %)** · 2 ffmpeg | One useless `ffmpeg -i` removed + `preset faster` |
| Estimate | 1.57 s · 12 ffmpeg | **1.35 s (−14 %)** · 6 ffmpeg | Six useless `ffmpeg -i` removed + `preset faster` |
| Repeat estimate | instant | instant | The cache now has a limit (it used to grow forever) |
| Render | 6.6–8.4 s · 2.18 MB | **5.9 s (−27 %) · 2.02 MB (−7 %)** | `preset faster`. The only gain that changes the file: VMAF 66.64 → 65.42 |
| Estimate error | +4.51 % | +6.67 % | Same method; still within the ±10 % shown in the UI |

Tried and dropped: capping each estimate ffmpeg's threads (`-threads 2` or `5`) was 8–10 % **slower**, and RAM barely moved.

### Render alternatives measured

| Render (55 s, 70 %) | Time | CPU | File | VMAF* |
|---|---|---|---|---|
| x264 medium (v1) | 8.4 s | 72 % | 2.18 MB | 66.64 |
| **x264 faster (v2)** | 5.9 s (**−30 %**) | 85 % | 2.02 MB (−7 %) | 65.42 (−1.2) |
| x264 veryfast | 4.7 s (−44 %) | 77 % | 1.63 MB | 55.06 (−11.6) |
| NVENC on the GPU | 2.9 s (**−65 %**) | 69 % | 3.41 MB (+56 %) | 79.77 |
| NVENC + GPU decoding | 2.9 s | **28 %** | 3.41 MB | 79.77 |

\*VMAF: 0–100 score of how close the compressed video looks to the original (1 of every 5 frames, scaled back to 1080p).

### Where there is still room (changes the output: needs a decision)

- **GPU (NVENC)** is ~2× faster than `faster` and, decoding on the GPU too, leaves 72 % of the CPU free. But its quality scale is different: with the same number (CQ 33) the file came out 56 % larger and better looking. The slider table must be calibrated for the GPU before offering it.
- **Estimate RAM:** encoding 3 clips at a time instead of 6 halves the peak, with a ~2× slower estimate (~3 s).
- **x264 veryfast:** not worth it (−11.6 VMAF).
