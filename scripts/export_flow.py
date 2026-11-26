"""Export the agent flow from docs/agent-flow.html into standalone SVG images for the README.

The interactive page is the single source: this script lifts its flow diagram, inlines the
colour tokens (light, plus dark through prefers-color-scheme) and writes

  docs/img/flow.svg           the whole flow at natural size
  docs/img/flow-tour.svg      an animated camera tour: overview, then each phase, then back
  docs/img/flow-<n>-<id>.svg  one readable frame per phase

GitHub shows SVG through <img>: no scripts and no web fonts, but inline CSS, CSS animation and
media queries work. Run after any change to the diagram:

  uv run python scripts/export_flow.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "agent-flow.html"
OUT = ROOT / "docs" / "img"

# Phase frames in diagram coordinates: x, y, width, height.
PHASES = [
    ("read", "Чтение файла", "s0–s1", (4, 8, 1064, 516)),
    ("base", "База и план", "s2–s4", (1056, 8, 654, 516)),
    ("cells", "Проверка ячеек", "s5–s6", (1698, 8, 1098, 516)),
    ("output", "Доказательство и выдача", "s7–s9", (2784, 8, 872, 516)),
]

# Rules from the page stylesheet that the diagram uses.
RULE = re.compile(
    r"^(svg text|\.(n-|t |s |k |lbl|tag|tg-|pt |edge|dash|ah |band|lane|ph |phk|cellbox|"
    r"cellcopy|cn |opt))")


def read_page() -> tuple[str, str, str, int, int]:
    html = PAGE.read_text(encoding="utf-8")
    m = re.search(r'<div class="scroll flow"[^>]*>\s*<svg viewBox="0 0 (\d+) (\d+)"[^>]*>(.*?)</svg>',
                  html, re.S)
    if not m:
        raise SystemExit("flow diagram not found in docs/agent-flow.html")
    width, height, body = int(m.group(1)), int(m.group(2)), m.group(3)
    label = re.search(r'<div class="scroll flow".*?aria-label="([^"]*)"', html, re.S).group(1)

    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    light = re.search(r":root \{(.*?)\}", style, re.S).group(1)
    dark = re.search(r':root\[data-theme="dark"\] \{(.*?)\}', style, re.S).group(1)
    rules = "\n".join(line for line in style.splitlines() if RULE.match(line.strip()))
    css = (f":root {{{light}}}\n"
           f"@media (prefers-color-scheme: dark) {{ :root {{{dark}}} }}\n"
           f".bg {{ fill: var(--surface); }}\n{rules}\n")
    return css, body, label, width, height


def svg(view: tuple[float, float, float, float], css: str, content: str, title: str) -> str:
    x, y, w, h = view
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" width="{w}" '
            f'height="{h}" role="img">\n<title>{title}</title>\n<style>\n{css}</style>\n'
            f'<rect class="bg" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>\n'
            f"{content}\n</svg>\n")


# --- the tour ---------------------------------------------------------------------------

FRAME_W, FRAME_H, BAR = 1200, 600, 48
HOLD = {"overview": 3.5, "read": 4.0, "base": 3.5, "cells": 4.5, "output": 4.0}
ZOOM, PAN = 1.4, 1.2


def camera(rect: tuple[float, float, float, float]) -> tuple[float, float, float]:
    x, y, w, h = rect
    area = FRAME_H - BAR
    s = min(1.0, FRAME_W / w, area / h)
    tx = (FRAME_W - w * s) / 2 - x * s
    ty = BAR + (area - h * s) / 2 - y * s
    return round(tx, 2), round(ty, 2), round(s, 4)


def tour(css: str, body: str, width: int, height: int) -> str:
    stops = [("overview", (0, 0, width, height))] + [(key, rect) for key, _, _, rect in PHASES]
    # timeline: hold, move, hold, move ... back to the overview
    times, t = [], 0.0
    for i, (key, rect) in enumerate(stops):
        times.append((t, key, rect))
        t += HOLD[key]
        times.append((t, key, rect))
        t += ZOOM if i in (0, len(stops) - 1) else PAN
    total = round(t, 2)
    times.append((total, "overview", stops[0][1]))

    def pct(sec: float) -> str:
        return f"{sec / total * 100:.2f}%"

    frames = []
    for sec, _, rect in times:
        tx, ty, s = camera(rect)
        frames.append(f"  {pct(sec)} {{ transform: translate({tx}px, {ty}px) scale({s}); }}")
    tx0, ty0, s0 = camera(stops[0][1])

    # the active chip switches halfway through each move
    keys = [k for k, _ in stops]
    switch = {}
    for i in range(len(stops)):
        end_hold = times[2 * i + 1][0]
        nxt = times[2 * i + 2][0]
        switch[keys[i]] = (end_hold + nxt) / 2
    starts = {"overview": 0.0}
    for i in range(1, len(keys)):
        starts[keys[i]] = switch[keys[i - 1]]
    chip_css = []
    for i, key in enumerate(keys):
        a, b = starts[key], switch[key]
        eps = 0.15
        if key == "overview":
            # active at the start and again after the last phase, on the way back
            back = switch[keys[-1]]
            kf = (f"0%, {pct(b - eps)} {{ opacity: 1; }} {pct(b + eps)}, {pct(back - eps)} "
                  f"{{ opacity: 0; }} {pct(back + eps)}, 100% {{ opacity: 1; }}")
        else:
            kf = (f"0%, {pct(a - eps)} {{ opacity: 0; }} {pct(a + eps)}, {pct(b - eps)} "
                  f"{{ opacity: 1; }} {pct(b + eps)}, 100% {{ opacity: 0; }}")
        chip_css.append(f"@keyframes hl{i} {{ {kf} }}\n.hl{i} {{ animation: hl{i} {total}s linear "
                        f"infinite; opacity: {1 if i == 0 else 0}; }}")

    chips = [("Весь поток", "")] + [(name, codes) for _, name, codes, _ in PHASES]
    widths = [104, 172, 160, 180, 252]
    chip_svg, x = [], 16
    for i, ((name, codes), w) in enumerate(zip(chips, widths)):
        label = (f'<text class="chip" x="{x + 14}" y="29">{name}'
                 + (f'<tspan class="chipk" dx="6">{codes}</tspan>' if codes else "") + "</text>")
        hl_label = label.replace('class="chip"', 'class="chip on"')
        chip_svg.append(
            f'<rect class="chipbg" x="{x}" y="11" width="{w}" height="26" rx="13"/>{label}\n'
            f'<g class="hl{i}"><rect class="chipon" x="{x}" y="11" width="{w}" height="26" '
            f'rx="13"/>{hl_label}</g>')
        x += w + 8

    tour_css = css + f"""
.cam {{ transform: translate({tx0}px, {ty0}px) scale({s0}); animation: cam {total}s ease-in-out infinite; }}
@keyframes cam {{
{chr(10).join(frames)}
}}
.bar {{ fill: var(--surface); }}
.barline {{ stroke: var(--line); stroke-width: 1; }}
.chipbg {{ fill: var(--surface); stroke: var(--line); }}
.chipon {{ fill: var(--fg); }}
.chip {{ font-family: var(--font-body); font-size: 13px; font-weight: 500; fill: var(--fg); }}
.chip.on {{ fill: var(--bg); }}
.chipk {{ font-family: var(--font-mono); font-size: 11.5px; fill: var(--muted); }}
.chip.on .chipk {{ fill: var(--bg); opacity: .7; }}
.brand {{ font-family: var(--font-body); font-size: 12.5px; fill: var(--muted); }}
.prog {{ fill: var(--agent-line); transform: scaleX(0); animation: prog {total}s linear infinite; }}
@keyframes prog {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
{chr(10).join(chip_css)}
@media (prefers-reduced-motion: reduce) {{
  .cam, .prog, [class^="hl"] {{ animation: none; }}
}}
"""
    content = (f'<g class="cam">\n{body}\n</g>\n'
               f'<rect class="bar" x="0" y="0" width="{FRAME_W}" height="{BAR}"/>\n'
               f'<line class="barline" x1="0" y1="{BAR}" x2="{FRAME_W}" y2="{BAR}"/>\n'
               f'<rect class="prog" x="0" y="{BAR - 2}" width="{FRAME_W}" height="2"/>\n'
               + "\n".join(chip_svg) + "\n"
               f'<text class="brand" x="{FRAME_W - 16}" y="29" text-anchor="end">'
               f"reqaudit · путь документа до отчёта</text>")
    return svg((0, 0, FRAME_W, FRAME_H), tour_css, content,
               "Облёт схемы агента reqaudit: весь поток, затем каждая фаза по очереди")


def main() -> None:
    css, body, label, width, height = read_page()
    OUT.mkdir(parents=True, exist_ok=True)
    written = {}
    written["flow.svg"] = svg((0, 0, width, height), css, body, label)
    written["flow-tour.svg"] = tour(css, body, width, height)
    for n, (key, name, codes, rect) in enumerate(PHASES, start=1):
        written[f"flow-{n}-{key}.svg"] = svg(rect, css, body, f"{name}, этапы {codes}")
    for name, text in written.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"{name:24} {len(text.encode()) // 1024:4d} KB")


if __name__ == "__main__":
    main()
