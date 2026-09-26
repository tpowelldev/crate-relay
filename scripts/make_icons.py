#!/usr/bin/env python3
"""
Generate every icon asset from code, so the artwork is reproducible and editable.

  assets/AppIcon.svg / AppIcon.png (1024) / AppIcon.icns   - the app icon
  src/crate_relay/resources/*.png                           - menu bar template icons

Requires: pip install cairosvg pillow
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import cairosvg
from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
RES = ROOT / "src" / "crate_relay" / "resources"


def render(svg: str, size: int) -> Image.Image:
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def polar(cx: float, cy: float, r: float, deg: float) -> tuple:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def arrow_paths(cx, cy, r, stroke, head_w, head_len, arcs, rotate=0.0):
    """Clockwise arc arrows. Angles in degrees, 0 = 3 o'clock, clockwise (SVG y-down)."""
    out = []
    for start, end in arcs:
        start, end = start + rotate, end + rotate
        x0, y0 = polar(cx, cy, r, start)
        x1, y1 = polar(cx, cy, r, end)
        large = 1 if (end - start) % 360 > 180 else 0
        arc = f'<path d="M{x0:.2f},{y0:.2f} A{r},{r} 0 {large} 1 {x1:.2f},{y1:.2f}" ' \
              f'fill="none" stroke="{{stroke}}" stroke-width="{stroke}" stroke-linecap="round"/>'
        a = math.radians(end)
        nx, ny = math.cos(a), math.sin(a)          # radial
        tx, ty = -math.sin(a), math.cos(a)         # clockwise tangent
        bx, by = x1 - tx * 0.5, y1 - ty * 0.5      # tiny overlap with the arc
        p1 = (bx + nx * head_w / 2, by + ny * head_w / 2)
        p2 = (bx - nx * head_w / 2, by - ny * head_w / 2)
        tip = (x1 + tx * head_len, y1 + ty * head_len)
        head = f'<path d="M{p1[0]:.2f},{p1[1]:.2f} L{tip[0]:.2f},{tip[1]:.2f} L{p2[0]:.2f},{p2[1]:.2f} Z" ' \
               f'fill="{{fill}}" stroke="{{fill}}" stroke-width="{stroke * 0.18:.2f}" stroke-linejoin="round"/>'
        out.append((arc, head))
    return out


# --------------------------------------------------------------------------- app icon
ARCS = [(198, 328), (18, 148)]


def app_icon_svg() -> str:
    cx = cy = 512
    grooves = "\n".join(
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#fff" stroke-opacity="{0.035 + 0.02 * (i % 3 == 0):.3f}" stroke-width="1.6"/>'
        for i, r in enumerate(range(118, 244, 7))
    )

    def wedge(a0, a1, r=246):
        x0, y0 = polar(cx, cy, r, a0)
        x1, y1 = polar(cx, cy, r, a1)
        return f'M{cx},{cy} L{x0:.1f},{y0:.1f} A{r},{r} 0 0 1 {x1:.1f},{y1:.1f} Z'

    arrows = "\n".join(
        arc.format(stroke="url(#ring)") + "\n" + head.format(fill="url(#ring)")
        for arc, head in arrow_paths(cx, cy, 332, 40, 104, 62, ARCS)
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2A2470"/>
      <stop offset="0.55" stop-color="#171544"/>
      <stop offset="1" stop-color="#0B0B26"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.18" r="0.75">
      <stop offset="0" stop-color="#8B5CF6" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#8B5CF6" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="vinyl" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#2B2B33"/>
      <stop offset="0.7" stop-color="#15151B"/>
      <stop offset="1" stop-color="#08080B"/>
    </radialGradient>
    <radialGradient id="sheen" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0.3" stop-color="#fff" stop-opacity="0.16"/>
      <stop offset="1" stop-color="#fff" stop-opacity="0.02"/>
    </radialGradient>
    <linearGradient id="label" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#FF9A3D"/>
      <stop offset="1" stop-color="#FF2F7E"/>
    </linearGradient>
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#5EF2D0"/>
      <stop offset="1" stop-color="#22B8F0"/>
    </linearGradient>
    <linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fff" stop-opacity="0.10"/>
      <stop offset="0.5" stop-color="#fff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect x="100" y="100" width="824" height="824" rx="186" fill="url(#bg)"/>
  <rect x="100" y="100" width="824" height="824" rx="186" fill="url(#glow)"/>
  <rect x="100" y="100" width="824" height="824" rx="186" fill="url(#gloss)"/>
  <rect x="101.5" y="101.5" width="821" height="821" rx="185" fill="none" stroke="#fff" stroke-opacity="0.10" stroke-width="3"/>
  <circle cx="{cx}" cy="{cy + 10}" r="252" fill="#000" fill-opacity="0.35"/>
  <circle cx="{cx}" cy="{cy}" r="250" fill="url(#vinyl)"/>
  {grooves}
  <path d="{wedge(-68, -22)}" fill="url(#sheen)"/>
  <path d="{wedge(112, 158)}" fill="url(#sheen)"/>
  <circle cx="{cx}" cy="{cy}" r="248.5" fill="none" stroke="#fff" stroke-opacity="0.14" stroke-width="3"/>
  <circle cx="{cx}" cy="{cy}" r="94" fill="url(#label)"/>
  <circle cx="{cx}" cy="{cy}" r="94" fill="none" stroke="#000" stroke-opacity="0.25" stroke-width="4"/>
  <circle cx="{cx}" cy="{cy}" r="62" fill="none" stroke="#fff" stroke-opacity="0.22" stroke-width="3"/>
  <circle cx="{cx}" cy="{cy}" r="12" fill="#0B0B26"/>
  {arrows}
</svg>'''


def build_app_icon() -> None:
    svg = app_icon_svg()
    (ASSETS / "AppIcon.svg").write_text(svg)
    art = render(svg, 1024)

    # macOS-style drop shadow under the rounded square
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rounded_rectangle((100, 100, 924, 924), radius=186, fill=255)
    shadow_alpha = ImageChops.offset(mask, 0, 12).filter(ImageFilter.GaussianBlur(22)).point(lambda v: int(v * 0.45))
    shadow = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha)
    icon = Image.alpha_composite(shadow, art)
    icon.save(ASSETS / "AppIcon.png")

    iconset = ASSETS / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            icon.resize((px, px), Image.LANCZOS).save(iconset / name)
    icon.save(ASSETS / "AppIcon.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])


# --------------------------------------------------------------------------- menu bar icons
MB = 40          # 20pt @2x; rumps draws status icons at 20x20pt
MB_R = 14.2      # arrow ring radius
MB_ARCS = [(200, 318), (20, 138)]


def glyph_svg(rotate: float = 0.0, arrow_opacity: float = 1.0) -> str:
    c = MB / 2
    arrows = "\n".join(
        arc.format(stroke="#000") + "\n" + head.format(fill="#000")
        for arc, head in arrow_paths(c, c, MB_R, 3.2, 8.6, 4.6, MB_ARCS, rotate)
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {MB} {MB}">
  <circle cx="{c}" cy="{c}" r="8.6" fill="#000"/>
  <g opacity="{arrow_opacity}">{arrows}</g>
</svg>'''


def render_glyph(rotate: float = 0.0, arrow_opacity: float = 1.0) -> Image.Image:
    """The glyph with the record's groove ring and spindle hole cut out."""
    img = render(glyph_svg(rotate, arrow_opacity), MB)
    c = MB / 2
    img = knockout(img, c, c, 6.1, inner=4.8)  # groove
    return knockout(img, c, c, 1.9)            # spindle hole


def knockout(img: Image.Image, cx: float, cy: float, r: float, inner: float = 0.0) -> Image.Image:
    s = 8
    hole = Image.new("L", (MB * s, MB * s), 255)
    d = ImageDraw.Draw(hole)
    d.ellipse(((cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s), fill=0)
    if inner:
        d.ellipse(((cx - inner) * s, (cy - inner) * s, (cx + inner) * s, (cy + inner) * s), fill=255)
    hole = hole.resize((MB, MB), Image.LANCZOS)
    out = img.copy()
    out.putalpha(ImageChops.multiply(img.getchannel("A"), hole))
    return out


def badge(img: Image.Image, kind: str) -> Image.Image:
    cx, cy, r = (31.5, 31.5, 7.2) if kind == "pause" else (32.0, 8.0, 6.2)
    out = knockout(img, cx, cy, r + 2.2)
    if kind == "pause":
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {MB} {MB}">
          <circle cx="{cx}" cy="{cy}" r="{r}" fill="#000"/>
          <rect x="{cx - 3.4}" y="{cy - 3.6}" width="2.4" height="7.2" rx="0.6" fill="#fff"/>
          <rect x="{cx + 1.0}" y="{cy - 3.6}" width="2.4" height="7.2" rx="0.6" fill="#fff"/></svg>'''
        b = render(svg, MB)
        # the white bars must become transparent in a template image
        a = ImageChops.subtract(b.getchannel("A"), b.convert("L"))
        b = Image.new("RGBA", (MB, MB), (0, 0, 0, 0))
        b.putalpha(a)
    else:
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {MB} {MB}"><circle cx="{cx}" cy="{cy}" r="{r}" fill="#000"/></svg>'
        b = render(svg, MB)
    return Image.alpha_composite(out, b)


def build_menubar_icons() -> None:
    RES.mkdir(parents=True, exist_ok=True)
    base = render_glyph()
    base.save(RES / "menubar.png")
    badge(base, "pause").save(RES / "menubar-waiting.png")
    badge(base, "dot").save(RES / "menubar-attention.png")
    render_glyph(arrow_opacity=0.35).save(RES / "menubar-off.png")
    for i in range(8):  # arrows are 2-fold symmetric, so 180 degrees loops seamlessly
        render_glyph(rotate=i * 22.5).save(RES / f"menubar-sync-{i}.png")


if __name__ == "__main__":
    ASSETS.mkdir(exist_ok=True)
    build_app_icon()
    build_menubar_icons()
    print("Icons written to", ASSETS, "and", RES)
