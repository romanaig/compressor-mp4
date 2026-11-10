#!/usr/bin/env python3
"""
Test with real videos: drops them through the UI, previews 3 points, renders,
checks size, resolution, duration and audio, and saves screenshots.

Usage: python tests/test_real_videos.py VIDEO1 [VIDEO2 ...] [--pct 70] [--format mp4|mov|webm] [--fps 24]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import server as sv          # noqa: E402
from files import Folders    # noqa: E402


def open_browser(pw):
    for attempt in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            return pw.chromium.launch(**attempt)
        except Exception:
            continue
    raise SystemExit("no browser available for Playwright")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--pct", type=float, default=70)
    ap.add_argument("--format", default="mp4", choices=["mp4", "mov", "webm"])
    ap.add_argument("--fps", type=float, help="output frame rate (default: the original's)")
    ap.add_argument("--screenshots", default=str(ROOT / "tests" / "history" / "screenshots"))
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="compressor_real_"))
    folders = Folders(tmp / "input", tmp / "output", tmp / "tmp")
    srv = sv.start(19765, folders=folders)
    ff = srv.compressor.ff
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shots = Path(a.screenshots)
    shots.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    failures = []
    with sync_playwright() as pw:
        browser = open_browser(pw)
        page = browser.new_page(viewport={"width": 1440, "height": 860})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/")
        page.wait_for_function("document.querySelector('#formats').children.length === 3")
        for v in map(Path, a.videos):
            print(f"\n== {v.name} ({v.stat().st_size / 1048576:.2f} MB)")
            # large files go through a hidden input (same path as dropping: compressor.send(File))
            page.evaluate("""() => { if (!document.querySelector('#_test')) { const i = document.createElement('input');
                i.type = 'file'; i.id = '_test'; i.hidden = true; i.onchange = () => window.compressor.send(i.files[0]);
                document.body.appendChild(i); } }""")
            t0 = time.time()
            page.set_input_files("#_test", str(v))
            page.wait_for_function(f"document.querySelector('#fileInfo').textContent.startsWith({v.name!r})", timeout=300000)
            print(f"  opened in {time.time() - t0:.1f}s: {page.text_content('#fileInfo')}")
            dur = float(page.eval_on_selector("#timeline", "e => e.max"))
            page.fill("#pctNum", str(a.pct))
            page.eval_on_selector("#pctNum", "e => e.blur()")
            page.click(f"#formats .chip:text-is('.{a.format.upper()}')")
            if a.fps:
                page.eval_on_selector("#fps", f"e => {{ e.value = {a.fps}; e.dispatchEvent(new Event('input')); }}")
            for frac in (0.1, 0.5, 0.9):
                t = round(dur * frac, 2)
                page.eval_on_selector("#timeline", f"e => {{ e.value = {t}; e.dispatchEvent(new Event('input')); e.dispatchEvent(new Event('change')); }}")
                page.wait_for_function("document.querySelector('#rawImg').complete && document.querySelector('#rawTime').textContent === document.querySelector('#timecode').textContent.split(' / ')[0]", timeout=60000)
                t0 = time.time()
                page.click("#btPreview")
                page.wait_for_function("document.querySelector('#msg').textContent === 'Preview ready.' || document.querySelector('#msg').className === 'error'", timeout=300000)
                m = page.text_content("#msg")
                t_img = time.time() - t0
                if "failed" in m:
                    failures.append(f"{v.name} preview {t}: {m}")
                    continue
                page.wait_for_function("document.querySelector('#estSize').textContent.includes('≈') || document.querySelector('#estNote').textContent.includes('failed')", timeout=600000)
                print(f"  preview {page.text_content('#compTime')}{page.text_content('#compRes')}: image {t_img:.1f}s, "
                      f"size {time.time() - t0:.1f}s → {page.text_content('#estSize')}")
                page.screenshot(path=str(shots / f"{v.stem}_{int(frac * 100)}.png"))
                if frac == 0.5:
                    box = page.query_selector("#rawImg").bounding_box()
                    page.mouse.click(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.35)
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(shots / f"{v.stem}_zoom.png"))
                    page.click("#compImg")
            t0 = time.time()
            page.click("#btRender")
            page.wait_for_function("document.querySelector('#msg').textContent.startsWith('Done') || document.querySelector('#msg').className === 'error'", timeout=1800000)
            m = page.text_content("#msg")
            print(f"  render in {time.time() - t0:.1f}s → {m}")
            if not m.startswith("Done"):
                failures.append(f"{v.name} render: {m}")
                continue
            name = m.split("output/", 1)[1].split(" · ")[0]
            out = ff.info(folders.output / name)
            orig = ff.info(folders.input / v.name)
            print(f"  output: {out['codec']} {out['width']}x{out['height']} {out['fps']} fps {out['duration']}s "
                  f"audio={out['audio']} {out['bytes'] / 1048576:.2f} MB (orig {orig['bytes'] / 1048576:.2f} MB, {orig['duration']}s)")
            est = float(re.search(r"≈ ([\d.]+) MB", page.text_content("#estSize"))[1])
            error = (est / (out["bytes"] / 1048576) - 1) * 100
            print(f"  estimate {est:.2f} MB vs real {out['bytes'] / 1048576:.2f} MB → error {error:+.2f} %")
            if abs(error) > 35:
                failures.append(f"{v.name}: estimate off by {error:+.2f} %")
            if abs(out["duration"] - orig["duration"]) > 0.25:
                failures.append(f"{v.name}: duration changed {orig['duration']} → {out['duration']}")
            if out["audio"] != orig["audio"]:
                failures.append(f"{v.name}: audio disappeared/appeared")
            if out["bytes"] >= orig["bytes"]:
                failures.append(f"{v.name}: did not shrink")
        browser.close()
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    failures += [f"JS error: {e}" for e in errors]
    print("\nRESULT:", "OK" if not failures else "FAILURES")
    for f in failures:
        print("  -", f)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
