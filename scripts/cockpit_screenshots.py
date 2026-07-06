"""Capture headless des pages du cockpit → SVG + PNG (qlmanage).

Usage : uv run python scripts/cockpit_screenshots.py [outdir]
"""
import asyncio
import subprocess
import sys
from pathlib import Path

from trader.interfaces.cockpit.app import CockpitApp

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "state/screenshots")
OUT.mkdir(parents=True, exist_ok=True)

_PAGES = [
    ("2", "portfolio"),
    ("3", "decisions"),
    ("4", "plans"),
    ("5", "health"),
    ("6", "logs"),
    ("7", "universe"),
    ("8", "settings"),
]


async def main() -> None:
    app = CockpitApp()
    async with app.run_test(size=(200, 52)) as pilot:
        await pilot.pause()
        await asyncio.sleep(3.0)
        app.save_screenshot(filename="home.svg", path=str(OUT))
        for key, name in _PAGES:
            await pilot.press(key)
            await asyncio.sleep(0.8)
            app.save_screenshot(filename=f"{name}.svg", path=str(OUT))
        await pilot.press("1")
        await pilot.press("question_mark")
        await asyncio.sleep(0.5)
        app.save_screenshot(filename="help.svg", path=str(OUT))


if __name__ == "__main__":
    asyncio.run(main())
    for svg in OUT.glob("*.svg"):
        subprocess.run(["qlmanage", "-t", "-s", "2000", "-o", str(OUT), str(svg)],
                       capture_output=True)
    print(f"captures dans {OUT}")
