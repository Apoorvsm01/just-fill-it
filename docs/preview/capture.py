"""Regenerate the README screenshots from the preview harness (real content.js, stubbed chrome API).

    uv run python docs/preview/capture.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "images"
SHOTS = [
    ("m3", "done", "light"), ("m3", "done", "dark"), ("m3", "busy", "light"),
    ("glass", "done", "light"), ("glass", "done", "dark"), ("glass", "busy", "dark"),
    ("m3", "min", "light"), ("glass", "min", "light"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    url = (HERE / "index.html").as_uri()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme, state, scheme in SHOTS:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2,
                                    color_scheme=scheme)
            page.goto(f"{url}?theme={theme}&state={state}")
            page.wait_for_timeout(400)
            name = OUT / f"panel-{theme}-{state}-{scheme}.png"
            page.screenshot(path=str(name))
            print(name.name)
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
