#!/usr/bin/env python3
"""Generate Open WebUI's branded static assets from the Zeo source marks.

Open WebUI serves every /static/* asset out of STATIC_DIR
(/app/backend/open_webui/static). ai/openwebui/docker-compose.openwebui.yml
bind-mounts the files this script writes over the upstream defaults, one file
at a time. See ai/openwebui/OPENWEBUI.md § Branding for which file drives
which surface, and why favicon.png is black while the in-app mark is white.

Inputs   assets/Zeo Favicon Black.png, assets/Zeo Favicon White.png
Output   assets/openwebui/

Re-run after a logo change, then `make up openwebui` to recreate the
container. Browsers cache favicons hard — verify in a private window.

    python ai/openwebui/bin/generate_branding.py
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "assets"
OUT = SRC / "openwebui"

# Opaque backgrounds. iOS composites alpha to black on the home screen, and a
# PWA install icon sits on an arbitrary launcher, so those two must not be
# transparent. #171717 is the app's own dark-theme meta theme-color.
DARK = (23, 23, 23, 255)
LIGHT = (255, 255, 255, 255)


def load(name: str) -> Image.Image:
    """Open a source mark and trim its transparent border.

    Trimming first makes `fill` mean the same thing for every output, whatever
    padding the source PNG happens to carry.
    """
    im = Image.open(SRC / name).convert("RGBA")
    return im.crop(im.getbbox())


def render(src: Image.Image, w: int, h: int, fill: float = 0.88, bg=None) -> Image.Image:
    """Fit `src` into a w*h canvas at `fill` of the smaller edge, centered."""
    canvas = Image.new("RGBA", (w, h), bg or (0, 0, 0, 0))
    scale = min(w * fill / src.width, h * fill / src.height)
    resized = src.resize(
        (max(1, round(src.width * scale)), max(1, round(src.height * scale))),
        Image.LANCZOS,
    )
    canvas.alpha_composite(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
    return canvas.convert("RGB") if bg else canvas


def save(im: Image.Image, name: str, **kwargs) -> None:
    path = OUT / name
    im.save(path, **kwargs)
    print(f"  {name:<24} {str(im.size):<12} {im.mode:<5} {path.stat().st_size:>7} b")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    black, white = load("Zeo Favicon Black.png"), load("Zeo Favicon White.png")

    print("browser tab (black, transparent)")
    fav = render(black, 512, 512)
    save(fav, "favicon.png")
    save(render(black, 96, 96), "favicon-96x96.png")
    save(render(black, 48, 48), "favicon.ico", format="ICO",
         sizes=[(16, 16), (32, 32), (48, 48)])

    # Upstream's own favicon.svg is a base64 PNG inside an <image>; match that
    # rather than tracing a vector, so the two stay visually identical.
    buf = io.BytesIO()
    fav.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
        'width="512" height="512" viewBox="0 0 512 512">'
        f'<image width="512" height="512" xlink:href="data:image/png;base64,{b64}"/>'
        "</svg>\n"
    )
    (OUT / "favicon.svg").write_text(svg, encoding="utf-8")
    print(f"  {'favicon.svg':<24} {'(512, 512)':<12} {'svg':<5} "
          f"{(OUT / 'favicon.svg').stat().st_size:>7} b")

    print("home screen / install (opaque)")
    save(render(black, 180, 180, fill=0.78, bg=LIGHT), "apple-touch-icon.png")
    save(render(white, 500, 500, fill=0.72, bg=DARK), "logo.png")

    print("splash (theme-paired, natural aspect — rendered at height:6rem)")
    width = round(500 * black.width / black.height)
    save(render(black, width, 500, fill=0.98), "splash.png")
    save(render(white, width, 500, fill=0.98), "splash-dark.png")

    print("in-app mark (white, transparent — target of custom.css)")
    save(render(white, 512, 512), "zeo-mark-white.png")

    print(f"\nwrote {OUT.relative_to(REPO)}  (custom.css is hand-maintained, not generated)")


if __name__ == "__main__":
    main()
