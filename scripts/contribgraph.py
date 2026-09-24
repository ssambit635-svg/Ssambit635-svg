#!/usr/bin/env python3
"""
contribgraph.py - turn the contribution grid into things nobody else has.

Four animated "lenses" over your real commit history, each a standalone SVG
that animates inside a GitHub README (pure CSS/SMIL - no JS, because GitHub
renders README SVGs as images and scripts are disabled there):

    night    a star chart: active days are stars, streaks are constellations,
             your best day goes nova, meteors cross the sky
    climate  a weather system: rain falls on empty weeks, heat flares on big
             days, lightning strikes the day a streak died
    pulse    a cardiac monitor: the year as an ECG trace, one beat per day,
             gaps are flatlines, a sweep highlights the leading edge
    life     Conway's Game of Life, seeded by your real commits, evolving
             forever and reseeding itself when it goes extinct

Usage
    python scripts/contribgraph.py --user ssambit635-svg --out assets
    python scripts/contribgraph.py --modes night,pulse --weeks 53
    python scripts/contribgraph.py --json assets/contributions.json --offline

Data comes from github.com/users/<user>/contributions (no token needed) and is
cached to assets/contributions.json, so a failed fetch never breaks the README -
the previous grid is reused and the script exits 0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #

CELL = 13.0
GAP = 3.0
PITCH = CELL + GAP
LEFT = 40.0
TOP = 74.0
ROWS = 7
PAD_R = 26.0
PAD_B = 46.0

# every animation is switched off for people who ask for less motion
REDUCED = "@media (prefers-reduced-motion: reduce){*{animation:none!important;opacity:1!important}}"


UA = {"User-Agent": "Mozilla/5.0 (contribgraph.py)", "Accept-Language": "en"}

LEVELS = {
    "dark": ["#151a21", "#1a7f37", "#2ea043", "#56d364", "#e6ffed"],
    "light": ["#dfe5ec", "#aceebb", "#4ac26b", "#2da44e", "#033a1a"],
}

# a level-0 cell has to survive on plain white, not just inside GitHub's table
INK0 = {"dark": "#151a21", "light": "#dfe5ec"}
DOT = {"dark": "#2f3640", "light": "#b9c3ce"}

THEMES = {
    "dark": {
        "bg": "#0d1117", "bg2": "#010409", "grid": "#30363d", "line": "#21262d",
        "title": "#e6edf3", "label": "#8b949e", "dim": "#6e7681",
        "accent": "#39d353", "gold": "#ffd479", "sky": "#d8f7ff",
        "dust": "#7ee787", "halo": "#39d353", "card": "#0d1117",
    },
    "light": {
        "bg": "#ffffff", "bg2": "#f6f8fa", "grid": "#d0d7de", "line": "#e6eaef",
        "title": "#1f2328", "label": "#57606a", "dim": "#6e7781",
        "accent": "#1a7f37", "gold": "#bf8700", "sky": "#0969da",
        "dust": "#1a7f37", "halo": "#2da44e", "card": "#ffffff",
    },
}


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def n(x: float) -> str:
    """shortest number that still looks clean - keeps the files small"""
    v = round(float(x), 2)
    return str(int(v)) if v == int(v) else f"{v:g}"


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #

def parse_contributions(html: str) -> dict:
    """pull {iso date: {'count': int, 'level': int}} out of the graph markup"""
    out: dict[str, dict] = {}
    for m in re.finditer(r'data-date="(\d{4}-\d{2}-\d{2})"([^>]*)>', html):
        iso, rest = m.group(1), m.group(2)
        lvl = re.search(r'data-level="(\d)"', rest)
        tail = html[m.end():m.end() + 900]
        tip = re.search(r">((?:[\d,]+)\s+contributions|No contributions)\s+on\s", tail)
        count = 0
        if tip:
            head = tip.group(1)
            if head[0].isdigit():
                count = int(head.split()[0].replace(",", ""))
        level = int(lvl.group(1)) if lvl else 0
        if count and not level:  # markup drift: recompute the level ourselves
            level = level_for(count)
        out[iso] = {"count": count, "level": level}
    return out


def level_for(count: int) -> int:
    return 0 if count <= 0 else 1 if count <= 3 else 2 if count <= 6 else 3 if count <= 9 else 4


def fetch(user: str, year: int, timeout: float = 25.0) -> dict:
    url = f"https://github.com/users/{urllib.parse.quote(user)}/contributions?from={year}-01-01&to={year}-12-31"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_contributions(r.read().decode("utf-8", "replace"))


def load_days(user: str, today: dt.date, json_path: Path, offline: bool = False,
              refresh: bool = False) -> tuple[dict, str]:
    """current year + previous year, merged. returns (days, source)

    The cache is what protects the profile: it is only ever overwritten by a
    successful fetch, and it is what we fall back to when github.com is down.
    """
    cached: dict[str, dict] = {}
    if json_path.exists():
        try:
            cached = json.loads(json_path.read_text()).get("days") or {}
            if not cached:
                raise ValueError("empty cache")
        except Exception as exc:
            print(f"note: cache unusable ({exc})", file=sys.stderr)
            cached = {}
    if offline or not user:
        if not cached:
            raise SystemExit(f"no usable cache at {json_path} - run once without --offline")
        return cached, "cache"
    if cached and not refresh and dt.date.fromisoformat(max(cached)) >= today:
        return cached, "cache(fresh)"          # already drew this year today
    days = dict(cached)
    got = 0
    for year in (today.year, today.year - 1):
        try:
            days.update(fetch(user, year))
            got += 1
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"warn: could not fetch {year} ({exc})", file=sys.stderr)
    if not got:
        if days:
            return days, "cache(stale)"
        raise SystemExit("github.com unreachable and no cache - previous SVGs stay as they are")
    return days, "live"


def build_window(days: dict, today: dt.date, weeks: int):
    """columns of 7 days (Sun first) ending on the week that contains today"""
    end_week_start = today - dt.timedelta(days=today.weekday() + 1)  # Sunday
    start = end_week_start - dt.timedelta(weeks=weeks - 1)
    grid = []
    for c in range(weeks):
        col = []
        for r in range(ROWS):
            d = start + dt.timedelta(weeks=c, days=r)
            rec = days.get(d.isoformat())
            if rec is None or d > today:
                col.append({"date": d, "count": 0, "level": -1 if d > today else 0, "future": d > today})
            else:
                col.append({"date": d, "count": rec["count"],
                            "level": rec.get("level", level_for(rec["count"])), "future": False})
        grid.append(col)
    return grid


def stats_of(grid) -> dict:
    flat = [d for col in grid for d in col if not d["future"]]
    total = sum(d["count"] for d in flat)
    active = sum(1 for d in flat if d["count"] > 0)
    seq = sorted(flat, key=lambda d: d["date"])
    best = run = 0
    prev = None
    for d in seq:
        if d["count"] > 0:
            run = run + 1 if prev and (d["date"] - prev).days == 1 else 1
            best = max(best, run)
            prev = d["date"]
        else:
            run = 0
            prev = None
    cur = 0
    for d in reversed(seq):
        if d["count"] > 0:
            cur += 1
        elif cur == 0 and d is seq[-1]:
            continue
        else:
            break
    top = max(seq, key=lambda d: d["count"]) if seq else None
    gaps = 0
    in_gap = 0
    for d in seq:
        if d["count"] == 0:
            in_gap += 1
        else:
            if in_gap >= 7:
                gaps += 1
            in_gap = 0
    if in_gap >= 7:
        gaps += 1
    return {
        "total": total, "active": active, "streak": best, "current": cur,
        "best_day": top["date"].isoformat() if top else "", "best_count": top["count"] if top else 0,
        "days": len(seq), "flatlines": gaps,
        "rate": round(total / max(1, len(seq)) * 7, 1),
    }


# --------------------------------------------------------------------------- #
# svg scaffolding
# --------------------------------------------------------------------------- #

def open_svg(w, h, theme, style, defs="", title="", desc="", bg=None):
    t = THEMES[theme]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n(w)}" height="{n(h)}" '
        f'viewBox="0 0 {n(w)} {n(h)}" role="img" font-family="ui-sans-serif,-apple-system,\'Segoe UI\','
        f'Helvetica,Arial,sans-serif">'
        f"<title>{esc(title)}</title><desc>{esc(desc)}</desc>"
        f'<defs>{defs}</defs>'
        f'<style>{style}</style>'
        f'<rect width="{n(w)}" height="{n(h)}" fill="{bg or t["bg"]}"/>'
    )


def close_svg():
    return "</svg>"


def header(w, theme, title, sub, chips, t=None):
    t = t or THEMES[theme]
    out = [f'<text x="{n(LEFT)}" y="30" font-size="15" font-weight="700" fill="{t["title"]}">{esc(title)}</text>']
    if sub:
        out.append(f'<text x="{n(LEFT)}" y="49" font-size="11.5" fill="{t["label"]}">{esc(sub)}</text>')
    x = w - PAD_R
    for label, value in reversed(chips):
        out.append(f'<text x="{n(x)}" y="30" font-size="13" font-weight="700" fill="{t["accent"]}" '
                   f'text-anchor="end">{esc(value)}</text>')
        out.append(f'<text x="{n(x)}" y="47" font-size="9" fill="{t["dim"]}" text-anchor="end" '
                   f'text-transform="uppercase" letter-spacing="1">{esc(label)}</text>')
        x -= 118
    return "".join(out)


def month_labels(grid, w, theme, t=None):
    t = t or THEMES[theme]
    out, last = [], None
    for c, col in enumerate(grid):
        d = col[0]["date"]
        if d.month != last and d.day <= 7:
            last = d.month
            out.append(f'<text x="{n(LEFT + c * PITCH)}" y="{n(TOP - 10)}" font-size="9.5" '
                       f'fill="{t["label"]}">{d.strftime("%b")}</text>')
    out.append(f'<text x="{n(LEFT - 8)}" y="{n(TOP + 12)}" font-size="8.5" fill="{t["dim"]}" '
               f'text-anchor="end">S</text>')
    out.append(f'<text x="{n(LEFT - 8)}" y="{n(TOP + 3 * PITCH + 4)}" font-size="8.5" fill="{t["dim"]}" '
               f'text-anchor="end">W</text>')
    out.append(f'<text x="{n(LEFT - 8)}" y="{n(TOP + 6 * PITCH + 4)}" font-size="8.5" fill="{t["dim"]}" '
               f'text-anchor="end">F</text>')
    return "".join(out)


def footer(w, h, theme, text, t=None, swatch=None, legend=True):
    """caption on the left, the less/more ramp on the right, pinned to the panel floor"""
    t = t or THEMES[theme]
    y = h - 20
    out = [f'<text x="{n(LEFT)}" y="{n(y + 4)}" font-size="9.5" fill="{t["dim"]}">{esc(text)}</text>']
    if not legend:
        return "".join(out)
    lx = w - PAD_R - 130
    out.append(f'<text x="{n(lx - 8)}" y="{n(y + 3)}" font-size="9" fill="{t["dim"]}" '
               f'text-anchor="end">less</text>')
    for i, col in enumerate(swatch or LEVELS[theme]):
        out.append(f'<rect x="{n(lx + i * 18)}" y="{n(y - 7)}" width="13" height="13" rx="3" '
                   f'fill="{col}" stroke="{t["grid"]}" stroke-width=".5"/>')
    out.append(f'<text x="{n(lx + 5 * 18 + 6)}" y="{n(y + 3)}" font-size="9" '
               f'fill="{t["dim"]}">more</text>')
    return "".join(out)


def base_grid(grid, theme, opacity=1.0, rx=3.2, sweep=0.0, cls="cell", skip_active=False, mode=""):
    """every past day as a rounded tile, with a left-to-right reveal on load"""
    t = THEMES[theme]
    body = []
    for c, col in enumerate(grid):
        delay = f' style="animation-delay:{n(c * sweep)}s"' if sweep else ""
        for r, d in enumerate(col):
            if d["future"] or (skip_active and d["count"] > 0):
                continue
            body.append(f'<rect class="{cls}" x="{n(LEFT + c * PITCH)}" y="{n(TOP + r * PITCH)}" '
                        f'width="{n(CELL)}" height="{n(CELL)}" rx="{rx}" '
                        f'fill="{level_fill(theme, d["level"], mode)}" opacity="{n(opacity)}" '
                        f'stroke="{t["grid"]}" stroke-width=".4"{delay}/>')
    return "".join(body)


def dot_grid(grid, theme, r=2.1, sweep=0.0, skip_active=True, ink=None):
    """the same year as dust: tiny dots, active days left to the renderer"""
    ink = ink or DOT[theme]
    body = []
    for c, col in enumerate(grid):
        for rr, d in enumerate(col):
            if d["future"] or (skip_active and d["count"] > 0):
                continue
            cx, cy = LEFT + c * PITCH + CELL / 2, TOP + rr * PITCH + CELL / 2
            delay = f' style="animation-delay:{n(c * sweep)}s"' if sweep else ""
            body.append(f'<circle class="cell" cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" '
                        f'fill="{ink}"{delay}/>')
    return "".join(body)


LEVEL_TINT = {
    "climate": {"dark": {4: "#ffd479"}, "light": {4: "#bf8700"}},
}


def level_fill(theme, level, mode=""):
    """climate runs hot on the biggest days, everything else keeps the GitHub ramp"""
    if level <= 0:
        return INK0[theme]
    tint = LEVEL_TINT.get(mode, {}).get(theme, {})
    return tint.get(level, LEVELS[theme][level])


def palette(theme, mode=""):
    """a star chart only works on a dark plate, so `night` prints the light theme
    as a navy astronomy plate instead of white-on-white dust"""
    t = dict(THEMES[theme])
    ramp = list(LEVELS[theme])
    if mode == "night":
        if theme == "light":
            t.update(bg="#0e1a2b", bg2="#050a12", grid="#2b4c70", line="#1a3049",
                     title="#eaf2fb", label="#9db6d1", dim="#7691ad", accent="#4cc47c",
                     gold="#ffd479", sky="#cfe7ff", dust="#9db6d1", halo="#4cc47c")
            ramp = ["#1b3249", "#1f8a44", "#3fb950", "#7ee787", "#ffffff"]
        else:
            ramp = ["#151a21", "#1a7f37", "#2ea043", "#7ee787", "#ffffff"]
    return t, ramp


def past_days(grid):
    return [(c, r, d) for c, col in enumerate(grid) for r, d in enumerate(col) if not d["future"]]


def active_runs(past):
    """[[date, ...], ...] - calendar-consecutive runs of days with commits"""
    runs, cur, prev = [], [], None
    for c, r, d in past:
        if d["count"] > 0 and prev is not None and (d["date"] - prev).days == 1:
            cur.append(d["date"])
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = [d["date"]] if d["count"] > 0 else []
        prev = d["date"] if d["count"] > 0 else None
    if len(cur) >= 2:
        runs.append(cur)
    return runs


# --------------------------------------------------------------------------- #
# mode: night  -  a star chart of the year
# --------------------------------------------------------------------------- #

def render_night(grid, theme, stats, user, weeks, anim=True):
    t, ramp = palette(theme, "night")
    w = LEFT + weeks * PITCH + PAD_R
    h = TOP + ROWS * PITCH + PAD_B
    maxc = max(1, stats["best_count"])
    rng = 20260924

    def rand():
        nonlocal rng
        rng = (1103515245 * rng + 12345) % 2147483648
        return rng / 2147483648

    dust = []
    for i in range(90):
        x = LEFT + rand() * (w - LEFT - PAD_R)
        y = 58 + rand() * (h - 78)
        dust.append(f'<circle cx="{n(x)}" cy="{n(y)}" r="{n(0.5 + rand() * 0.8)}" fill="{t["dust"]}" '
                    f'opacity="{n(0.06 + rand() * 0.13)}" class="tw" '
                    f'style="animation-delay:{n(rand() * 6)}s"/>')

    past = past_days(grid)
    stars, centers = [], {}
    for i, (c, r, d) in enumerate(past):
        cx, cy = LEFT + c * PITCH + CELL / 2, TOP + r * PITCH + CELL / 2
        centers[d["date"]] = (cx, cy)
        if d["count"] <= 0:
            continue
        size = 1.9 + (d["count"] / maxc) * 3.3
        stars.append(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(size + 3.4)}" fill="{t["halo"]}" '
                     f'opacity=".16" class="tw" style="animation-delay:{n((i * 0.17) % 4)}s"/>')
        stars.append(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(size)}" fill="{ramp[d["level"]]}"/>')
        if d["level"] >= 4:
            stars.append(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(size + 7)}" fill="none" '
                         f'stroke="{t["halo"]}" stroke-width=".6" opacity=".35"/>')

    # Saturday -> next Sunday jumps back to the top row; breaking the line there
    # keeps it a constellation instead of a stock chart
    # GitHub stacks a week vertically (Sun on top), so two consecutive days only share
    # an edge inside one column. The Sat -> Sun jump breaks the line there, which is
    # what keeps this reading as a constellation instead of a stock chart.
    pos = {d["date"]: (c, r) for c, r, d in past}
    chunks = []
    for run in active_runs(past)[:80]:
        part = [run[0]]
        for k in range(1, len(run)):
            (c0, r0), (c1, r1) = pos[run[k - 1]], pos[run[k]]
            if c0 == c1 and r1 == r0 + 1:
                part.append(run[k])
            else:
                if len(part) >= 2:
                    chunks.append(part)
                part = [run[k]]
        if len(part) >= 2:
            chunks.append(part)
    lines = []
    for part in chunks:
        pts = " ".join(f"{n(centers[x][0])},{n(centers[x][1])}" for x in part)
        lines.append(f'<polyline points="{pts}" pathLength="100" fill="none" stroke="{t["accent"]}" '
                     f'stroke-width="1.1" opacity=".6" stroke-linecap="round" class="draw"/>')

    nova = []
    if stats["best_day"]:
        target = dt.date.fromisoformat(stats["best_day"])
        for c, r, d in past:
            if d["date"] != target:
                continue
            cx, cy = centers[target]
            for delay in (0, 1.6, 3.2):
                nova.append(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="2" fill="none" stroke="{t["gold"]}" '
                            f'stroke-width="1.3" opacity="0">'
                            f'<animate attributeName="r" values="2;30" dur="4.8s" begin="{n(delay)}s" '
                            f'repeatCount="indefinite" calcMode="spline" keySplines="0.05 0.6 0.2 1" '
                            f'keyTimes="0;1"/>'
                            f'<animate attributeName="opacity" values=".8;0" dur="4.8s" '
                            f'begin="{n(delay)}s" repeatCount="indefinite"/></circle>')
            nova.append(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="3" fill="{t["title"]}"/>')
            ly = cy + 25 if cy < TOP + 44 else cy - 12
            if cx + 78 > w - PAD_R:
                lx, anchor = cx - 13, "end"
            elif cx - 78 < LEFT:
                lx, anchor = cx + 13, "start"
            else:
                lx, anchor = cx, "middle"
            nova.append(f'<text x="{n(lx)}" y="{n(ly)}" font-size="8.5" fill="{t["gold"]}" '
                        f'text-anchor="{anchor}" font-family="ui-monospace,Consolas,monospace">'
                        f'{target.strftime("%d %b")} · {d["count"]} commits</text>')
            break

    MSP = 11.0
    meteors = []
    for y0, delay in (((80, 0.8), (128, 4.6), (176, 7.9)) if anim else ()):
        meteors.append(f'<g class="meteor" style="animation-delay:{n(delay)}s">'
                       f'<path d="M{n(w + 30)} {n(y0)} l-86 28" stroke="{t["sky"]}" stroke-width="1.2" '
                       f'stroke-linecap="round" opacity=".7"/>'
                       f'<circle cx="{n(w + 30)}" cy="{n(y0)}" r="1.6" fill="{t["title"]}"/></g>')

    anim_css = ""
    if anim:
        anim_css = (
            "@keyframes tw{0%,100%{opacity:.05}50%{opacity:.4}}"
            "@keyframes draw{0%{stroke-dashoffset:100;opacity:0}8%{opacity:.6}42%{stroke-dashoffset:0}"
            "80%{stroke-dashoffset:0;opacity:.6}100%{stroke-dashoffset:-100;opacity:0}}"
            "@keyframes reveal{from{opacity:0}to{opacity:1}}"
            f"@keyframes fly{{from{{transform:translate(0,0)}}"
            f"to{{transform:translate(-{n(w + 200)}px,{n((w + 200) * 0.32)}px)}}}}"
            "@keyframes flyfade{0%,4%{opacity:0}9%{opacity:.85}66%{opacity:.85}100%{opacity:0}}"
            ".tw{animation:tw 4.4s ease-in-out infinite}"
            ".draw{stroke-dasharray:100;animation:draw 10s ease-in-out infinite}"
            ".meteor{animation:fly 11s linear infinite,flyfade 11s linear infinite;opacity:0}"
            ".cell{animation:reveal .9s ease-out both}"
            + REDUCED
        )
    defs = (f'<radialGradient id="vg" cx="50%" cy="45%" r="72%">'
            f'<stop offset="55%" stop-color="{t["bg"]}" stop-opacity="0"/>'
            f'<stop offset="100%" stop-color="{t["bg2"]}" stop-opacity=".85"/></radialGradient>')
    chips = [("lit days", str(stats["active"])), ("longest run", f'{stats["streak"]}d'),
             ("commits", str(stats["total"])), ("brightest", f'{stats["best_count"]}x')]
    body = [
        f'<rect width="{n(w)}" height="{n(h)}" fill="url(#vg)"/>',
        "".join(dust),
        dot_grid(grid, theme, r=1.9, sweep=0.012, ink=t["grid"]),
        "".join(lines), "".join(stars), "".join(nova), "".join(meteors),
        header(w, theme, "contribution star chart",
               f'{user} · {weeks} weeks · brightness = commits that day', chips, t),
        month_labels(grid, w, theme, t),
        footer(w, h, theme, "every lit cell is a day you shipped · the lines trace your streaks",
               t, swatch=ramp),
    ]
    return (open_svg(w, h, theme, anim_css, defs, "contribution star chart",
                     f'{stats["total"]} contributions over {weeks} weeks', bg=t["bg"]),
            "".join(body), w, h)


# --------------------------------------------------------------------------- #
# mode: climate  -  weather over the grid
# --------------------------------------------------------------------------- #

def render_climate(grid, theme, stats, user, weeks, anim=True):
    """a living weather system over the grid - the flagship lens.

    everything is derived from the real calendar:
      rain    weeks with zero commits, plus a ripple where the drops land
      snow    quiet days in Nov-Feb, so a winter break finally looks like one
      flare   your hottest days (level 4) get a corona and heat shimmer
      storm   the exact day a 3+ day streak died: cloud, bolt, ground flash
      wind    sky puffs over your wettest (busiest) weeks
      lamp    a sun crossing the panel on a slow arc, marking the year
      gauge   commits per month, drawn as a rainfall strip under the grid
    """
    t = THEMES[theme]
    w = LEFT + weeks * PITCH + PAD_R
    ground = TOP + ROWS * PITCH - 1
    strip_y = ground + 16
    h = TOP + ROWS * PITCH + 94
    fall = ground - TOP + 34
    past = past_days(grid)

    weekly = [sum(d["count"] for d in col if not d["future"]) for col in grid]
    dry = [i for i, v in enumerate(weekly) if v == 0]
    wet = [i for i, v in enumerate(weekly) if v >= 3]
    rain_cols = dry[:: max(1, len(dry) // 18)][:18]

    # ---------------- rain, and the ripple it makes ----------------
    rain, splash = [], []
    for k, c in enumerate(rain_cols):
        for j in range(4):
            x = LEFT + c * PITCH + 1.5 + ((j * 5 + k * 3) % (CELL + 1))
            dur = 1.35 + ((j * 3 + k) % 10) * 0.13
            delay = (j * 1.31 + k * 0.73) % 3.2
            y0 = TOP - 18 if anim else TOP + ((j * 31 + k * 17) % max(20, int(fall) - 34))
            rain.append(f'<line class="drop" x1="{n(x)}" y1="{n(y0)}" x2="{n(x - 1.4)}" '
                        f'y2="{n(y0 - 7)}" stroke="{t["sky"]}" stroke-width="1" opacity=".75" '
                        f'style="animation-duration:{n(dur)}s;animation-delay:{n(delay)}s"/>')
        rx = LEFT + c * PITCH + CELL / 2
        splash.append(f'<ellipse class="splash" cx="{n(rx)}" cy="{n(ground + 2)}" rx="4" ry="1.6" '
                      f'fill="none" stroke="{t["sky"]}" stroke-width=".9" opacity="0" '
                      f'style="animation-delay:{n((k * 0.37) % 1.9)}s"/>')

    # ---------------- snow over a quiet winter ----------------
    snow = []
    winter = [(c, r, d) for c, r, d in past if d["count"] == 0 and d["date"].month in (11, 12, 1, 2)]
    for i, (c, r, d) in enumerate(winter[:70]):
        dur = 7 + (i % 6) * 1.3
        fy = TOP + r * PITCH + CELL / 2
        x = LEFT + c * PITCH + CELL / 2
        snow.append(f'<circle class="flake" cx="{n(x)}" cy="{n(fy)}" r="1.1" '
                    f'fill="{t["sky"]}" opacity=".55" '
                    f'style="animation-duration:{n(dur)}s;animation-delay:{n(-(i * 0.41) % 9)}s"/>')

    # ---------------- clouds and wind ----------------
    clouds = []
    for y, sp, op, wd, delay in ((58, 42, 0.17, 180, 0), (98, 64, 0.12, 250, -18),
                                 (146, 84, 0.10, 320, -40), (184, 56, 0.11, 200, -10)):
        clouds.append(f'<g class="cl" style="animation-duration:{n(sp)}s;animation-delay:{n(delay)}s">'
                      f'<ellipse cx="0" cy="{n(y)}" rx="{n(wd / 2)}" ry="15" fill="{t["dust"]}" '
                      f'opacity="{n(op)}"/>'
                      f'<ellipse cx="{n(wd * 0.18)}" cy="{n(y - 9)}" rx="{n(wd / 3.4)}" ry="11" '
                      f'fill="{t["dust"]}" opacity="{n(op * 1.35)}"/></g>')

    # wind: three high cirrus streaks, and the speed IS your weekly commit rate
    wind, wbase = [], TOP - 33
    for i in range(3):
        dur = max(9.0, 46.0 - stats["rate"] * 2.2 + i * 7)
        wind.append(f'<g class="cl" style="animation-duration:{n(dur)}s;'
                    f'animation-delay:{n(-i * dur / 3)}s">'
                    f'<path d="M0 {n(wbase + i * 7)} h{n(120 + i * 60)} q14 0 10 -4" fill="none" '
                    f'stroke="{t["sky"]}" stroke-width=".9" opacity="{n(0.12 - i * 0.02)}"/></g>'
                    .replace('<g class="cl"', '<g class="cl" opacity="0"'))

    # ---------------- heat: corona + shimmer on the hottest days ----------------
    big = [(c, r, d) for c, r, d in past if d["level"] >= 4]
    sun = []
    for c, r, d in big[:12]:
        cx, cy = LEFT + c * PITCH + CELL / 2, TOP + r * PITCH + CELL / 2
        rays = []
        for a in range(8):
            ang = a * math.pi / 4
            rays.append(f'<line x1="{n(cx + math.cos(ang) * 7)}" y1="{n(cy + math.sin(ang) * 7)}" '
                        f'x2="{n(cx + math.cos(ang) * 15)}" y2="{n(cy + math.sin(ang) * 15)}" '
                        f'stroke="{t["gold"]}" stroke-width="1.3" opacity=".85"/>')
        origin = f'{n(cx)}px {n(cy)}px'
        sun.append(f'<g class="spin" style="transform-origin:{origin};'
                   f'animation-delay:{n((c * 0.21) % 3)}s">{"".join(rays)}</g>')
        sun.append(f'<rect class="shimmer" x="{n(LEFT + c * PITCH - 3)}" y="{n(cy - 26)}" '
                   f'width="{n(CELL + 6)}" height="28" rx="3" fill="{t["gold"]}" opacity="0" '
                   f'style="animation-delay:{n((r * 0.3) % 2)}s"/>')

    # ---------------- storms: the day a streak died ----------------
    bolts, storm_cells, flashes = [], [], []
    for i, (c, r, d) in enumerate(past):
        if d["count"]:
            continue
        back = 0
        for j in range(i - 1, -1, -1):
            if past[j][2]["count"] > 0:
                back += 1
            else:
                break
        if back < 3:
            continue
        cx = LEFT + c * PITCH + CELL / 2
        cy = TOP + r * PITCH + CELL
        delay = n((i * 0.37) % 7)
        d_part = cy - TOP
        bolt_d = (f"M{n(cx)} {n(TOP - 4)} l-6 {n(d_part * 0.45)} l7 0 l-7 {n(d_part * 0.4)} "
                  f"l13 {n(-d_part * 0.5)} l-9 0 l8 {n(-d_part * 0.3)} z")
        bolts.append(f'<path class="bolt" style="animation-delay:{delay}s" d="{bolt_d}" '
                     f'fill="{t["gold"]}"/>')
        cy0 = TOP - 11
        cloud = (f'<ellipse cx="{n(cx)}" cy="{n(cy0)}" rx="22" ry="7" fill="{t["dim"]}" '
                 f'opacity=".45"/>'
                 f'<ellipse cx="{n(cx + 9)}" cy="{n(cy0 - 4)}" rx="12" ry="5" fill="{t["dim"]}" '
                 f'opacity=".35"/>')
        storm_cells.append(f'<g>{cloud}<circle class="lit" cx="{n(cx)}" cy="{n(cy0)}" r="26" '
                           f'fill="url(#lit)" opacity=".06" style="animation-delay:{delay}s"/></g>')
        flashes.append(f'<ellipse class="flash" cx="{n(cx)}" cy="{n(cy)}" rx="44" ry="27" '
                       f'fill="{t["gold"]}" opacity="0" style="animation-delay:{delay}s"/>')
        if len(bolts) >= 7:
            break
    flashes.append(f'<rect class="scene" width="{n(w)}" height="{n(h)}" fill="{t["gold"]}" opacity="0"/>')

    # ---------------- the lamp that measures the year ----------------
    arc = (w - 2 * LEFT) / 2
    lamp = ""
    if anim:
        lamp = (f'<g class="lamp" opacity="0">'
                f'<circle cx="{n(LEFT)}" cy="{n(TOP - 27)}" r="6.5" fill="{t["gold"]}" opacity=".9"/>'
                f'<circle cx="{n(LEFT)}" cy="{n(TOP - 27)}" r="13" fill="{t["gold"]}" opacity=".13"/></g>')

    # ---------------- rain gauge ----------------
    months = {}
    for c, r, d in past:
        k = d["date"].year * 100 + d["date"].month
        months[k] = months.get(k, 0) + d["count"]
    keys = sorted(months)
    mx = max(months.values()) if months else 1
    mx = max(1, mx)
    seg_w = (w - LEFT - PAD_R) / max(1, len(keys))
    base = strip_y + 20
    gauge = [f'<line x1="{n(LEFT)}" y1="{n(base)}" x2="{n(w - PAD_R)}" y2="{n(base)}" '
             f'stroke="{t["grid"]}" stroke-width=".7"/>']
    for i, k in enumerate(keys):
        v = months[k]
        d0 = dt.date(k // 100, k % 100, 1)
        if i:
            prev = dt.date(keys[i - 1] // 100, keys[i - 1] % 100, 1)
            if d0.year != prev.year:        # the new-year tick, like a weather front
                gauge.append(f'<line x1="{n(LEFT + i * seg_w - 2)}" y1="{n(base - 20)}" '
                             f'x2="{n(LEFT + i * seg_w - 2)}" y2="{n(base + 2)}" '
                             f'stroke="{t["label"]}" stroke-width=".7" stroke-dasharray="2 2"/>')
        label = d0.strftime("%b") + (" \u2019%02d" % (d0.year % 100) if d0.month in (1, 7) else "")
        bh = 3 + (v / mx) * 16
        x = LEFT + i * seg_w
        bw = max(2, seg_w - 4)
        op = 0.95 if v >= mx * 0.6 else 0.45
        gauge.append(f'<rect x="{n(x)}" y="{n(base - bh)}" width="{n(bw)}" height="{n(bh)}" '
                     f'rx="2.5" fill="{t["accent"]}" opacity="{n(op)}"/>')
        gauge.append(f'<text x="{n(x + 1)}" y="{n(base + 9)}" font-size="7" fill="{t["dim"]}">'
                     f'{label}</text>')
        if v:
            gauge.append(f'<text x="{n(x + bw / 2)}" y="{n(base - bh - 3)}" font-size="6.5" '
                         f'fill="{t["label"]}" text-anchor="middle">{v}</text>')

    humidity = round(100 * stats["active"] / max(1, stats["days"]))
    pressure = 980 + stats["streak"] * 4
    chips = [("humidity", f"{humidity}%"), ("pressure", f"{pressure} hPa"),
             ("dry weeks", str(len(dry)))]

    style = (
        "@keyframes drift{from{transform:translateX(-170px)}"
        f"to{{transform:translateX({n(w + 320)}px)}}}}"
        "@keyframes spin{to{transform:rotate(360deg)}}"
        "@keyframes reveal{from{opacity:0}to{opacity:1}}"
        f"@keyframes drop{{0%{{transform:translateY(0);opacity:0}}14%{{opacity:.75}}78%{{opacity:.5}}"
        f"100%{{transform:translateY({n(fall)}px);opacity:0}}}}"
        "@keyframes splash{0%,70%{opacity:0;transform:scale(.35)}76%{opacity:.55}"
        "100%{opacity:0;transform:scale(1.5)}}"
        "@keyframes flake{0%{transform:translate(-3px,-150px);opacity:0}14%{opacity:.7}"
        "55%{transform:translate(4px,-60px);opacity:.55}"
        "86%{transform:translate(0,3px);opacity:.75}100%{transform:translate(0,5px);opacity:0}}"
        "@keyframes windpuff{0%{opacity:0;transform:translateX(-12px)}25%{opacity:.5}"
        "100%{opacity:0;transform:translateX(28px)}}"
        "@keyframes shimmer{0%,100%{opacity:0}50%{opacity:.11}}"
        "@keyframes strike{0%,92%{opacity:0}93%{opacity:1}94.5%{opacity:.12}96%{opacity:.9}"
        "100%{opacity:0}}"
        "@keyframes zap{0%,92%{opacity:0}93%{opacity:.2}94.5%{opacity:.05}96%{opacity:.16}"
        "100%{opacity:0}}"
        "@keyframes glow{0%,90%{opacity:.06}93%{opacity:.55}96%{opacity:.14}100%{opacity:.06}}"
        "@keyframes scene{0%,91.5%{opacity:0}93%{opacity:.05}95%{opacity:.03}97%{opacity:.045}"
        "100%{opacity:0}}"
        f"@keyframes lamp{{0%{{transform:translate(0,8px);opacity:0}}6%{{opacity:.85}}"
        f"50%{{transform:translate({n(arc)},-18px);opacity:.9}}"
        f"94%{{opacity:.85}}100%{{transform:translate({n(2 * arc)},8px);opacity:0}}}}"
        ".drop{animation-name:drop;animation-timing-function:linear;animation-iteration-count:infinite;"
        "animation-fill-mode:both;opacity:0}"
        ".splash{animation:splash 1.9s ease-out infinite;transform-box:fill-box;transform-origin:center}"
        ".flake{animation:flake 8s linear infinite;opacity:0}"
        ".wind{animation:windpuff 4s linear infinite;opacity:0}"
        ".shimmer{animation:shimmer 2.6s ease-in-out infinite}"
        ".cl{animation:drift linear infinite}"
        ".spin{animation:spin 10s linear infinite}"
        ".bolt{animation:strike 7s steps(1,end) infinite;opacity:0}"
        ".lit{animation:glow 7s steps(1,end) infinite}"
        ".flash{animation:zap 7s steps(1,end) infinite}"
        ".scene{animation:scene 7s steps(1,end) infinite}"
        ".lamp{animation:lamp 66s linear infinite}"
        ".cell{animation:reveal .9s ease-out both}"
    ) + REDUCED
    if not anim:
        style = ""
    defs = (f'<linearGradient id="fog" x1="0" x2="0" y1="0" y2="1">'
            f'<stop offset="0" stop-color="{t["bg"]}" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="{t["dust"]}" stop-opacity=".13"/></linearGradient>'
            f'<radialGradient id="lit"><stop offset="0" stop-color="{t["gold"]}" stop-opacity=".7"/>'
            f'<stop offset="1" stop-color="{t["gold"]}" stop-opacity="0"/></radialGradient>')
    body = [
        lamp, "".join(clouds), "".join(wind),
        base_grid(grid, theme, opacity=0.97, rx=3.2, sweep=0.011, mode="climate"),
        f'<rect x="0" y="{n(ground + 4)}" width="{n(w)}" height="{n(strip_y - ground - 2)}" '
        f'fill="url(#fog)"/>',
        "".join(snow), "".join(rain), "".join(splash), "".join(sun),
        "".join(storm_cells), "".join(bolts), "".join(flashes), "".join(gauge),
        f'<text x="{n(LEFT)}" y="{n(base + 22)}" font-size="8" fill="{t["dim"]}">'
        f'rain gauge · commits per month · {stats["total"]} in this window</text>',
        header(w, theme, "contribution climate",
               f'{user} · uv {stats["best_count"]} · wind {stats["rate"]}/wk · '
               f'{stats["streak"]}d high pressure', chips),
        month_labels(grid, w, theme),
        footer(w, h, theme, f'rain on {len(dry)} dry weeks · snow on a quiet winter · '
                            f'flares on {len(big)} hot days · storms where {len(bolts)} streaks died',
               legend=False),
    ]
    return (open_svg(w, h, theme, style, defs, "contribution climate forecast",
                     f'{stats["total"]} contributions'), "".join(body), w, h)


# --------------------------------------------------------------------------- #
# mode: pulse  -  the year on a cardiac monitor
# --------------------------------------------------------------------------- #

def render_pulse(grid, theme, stats, user, weeks, anim=True):
    t = THEMES[theme]
    w = LEFT + weeks * PITCH + PAD_R
    h = TOP + ROWS * PITCH + PAD_B
    days = past_days(grid)
    mid = TOP + (ROWS * PITCH) / 2
    amp = 56.0
    maxc = max(1, stats["best_count"])
    step_x = (w - LEFT - PAD_R - GAP) / max(1, len(days))

    seg, ybase = [], mid
    for i, (c, r, d) in enumerate(days):
        x = LEFT + (i + 0.5) * step_x
        if d["count"] <= 0:
            seg.append(f"L{n(x)} {n(ybase)}")
            continue
        a = 0.3 + 0.7 * min(1.0, d["count"] / maxc)
        wd = max(1.1, min(2.6, step_x * 0.4))
        seg += [f"L{n(x - wd * 1.6)} {n(ybase)}",
                f"L{n(x - wd * 0.5)} {n(ybase + a * 6)}",
                f"L{n(x)} {n(ybase - a * amp)}",
                f"L{n(x + wd * 0.7)} {n(ybase + a * 20)}",
                f"L{n(x + wd * 1.7)} {n(ybase)}"]
    trace = f"M{n(LEFT)} {n(ybase)} " + " ".join(seg)

    # flatlines: 7+ empty days -> a bracket under the grid instead of a wall of grey
    bands, run_start, run = [], None, 0
    for i, (c, r, d) in enumerate(days):
        if d["count"] == 0:
            if not run:
                run_start = i
            run += 1
        else:
            if run >= 7:
                bands.append((run_start, run))
            run = 0
    if run >= 7:
        bands.append((run_start, run))
    by, band_svg = TOP + ROWS * PITCH + 7, []
    longest_quiet = 0
    for s0, ln in bands[:18]:
        longest_quiet = max(longest_quiet, ln)
        x0 = LEFT + s0 * step_x
        x1 = LEFT + min(len(days), s0 + ln) * step_x
        wide = min(x1, w - PAD_R) - x0
        if wide < 4:
            continue
        band_svg.append(f'<path d="M{n(x0)} {n(by)} v4 h{n(wide)} v-4" fill="none" stroke="{t["dim"]}" '
                        f'stroke-width=".8" opacity=".5"/>')

    day_ix = {d["date"]: i for i, (c, r, d) in enumerate(days)}
    peaks, used_x = [], []
    for c, r, d in sorted(days, key=lambda z: -z[2]["count"])[:6]:
        if not d["count"] or d["date"] not in day_ix:
            continue
        x = LEFT + (day_ix[d["date"]] + 0.5) * step_x
        if any(abs(x - u) < 64 for u in used_x):
            continue
        used_x.append(x)
        a = 0.3 + 0.7 * min(1.0, d["count"] / maxc)
        y = mid - a * amp
        peaks.append(f'<g class="ping"><circle cx="{n(x)}" cy="{n(y)}" r="4.5" fill="none" '
                     f'stroke="{t["accent"]}" stroke-width="1"/>'
                     f'<text x="{n(max(LEFT + 14, min(w - PAD_R - 14, x)))}" y="{n(y + 13)}" '
                     f'font-size="8.5" fill="{t["accent"]}" text-anchor="middle" '
                     f'font-family="ui-monospace,Consolas,monospace">{d["count"]}</text></g>')
        if len(peaks) >= 3:
            break

    anim_css = ""
    if anim:
        anim_css = (
            "@keyframes trace{0%{stroke-dashoffset:1000;opacity:.95}60%{stroke-dashoffset:0;opacity:.95}"
            "100%{stroke-dashoffset:0;opacity:.6}}"
            "@keyframes sweep{from{stroke-dashoffset:1000}to{stroke-dashoffset:0}}"
            "@keyframes ping{0%,58%{opacity:0}66%{opacity:1}100%{opacity:0}}"
            "@keyframes blink{0%,42%{opacity:1}50%,100%{opacity:.12}}"
            "@keyframes gridin{from{opacity:0}to{opacity:1}}"
            ".trace{stroke-dasharray:1000;animation:trace 6.8s ease-in-out infinite alternate}"
            ".lead{stroke-dasharray:26 1000;animation:sweep 6.8s linear infinite}"
            ".ping{animation:ping 6.8s ease-out infinite;opacity:0}"
            ".heart{animation:blink 1.15s steps(1,end) infinite}"
            ".cell{animation:gridin 1s ease-out both}"
            + REDUCED
        )
    defs = (f'<pattern id="mm" width="16" height="16" patternUnits="userSpaceOnUse">'
            f'<path d="M16 0H0v16" fill="none" stroke="{t["grid"]}" stroke-width=".35" opacity=".38"/>'
            f'</pattern>'
            f'<pattern id="mm5" width="80" height="80" patternUnits="userSpaceOnUse">'
            f'<path d="M80 0H0v80" fill="none" stroke="{t["accent"]}" stroke-width=".5" opacity=".1"/>'
            f'</pattern>')
    chips = [("beats", str(stats["total"])), ("per week", str(stats["rate"])),
             ("flatlines", str(stats["flatlines"])), ("longest quiet", f"{longest_quiet}d")]
    body = [
        f'<rect width="{n(w)}" height="{n(h)}" fill="url(#mm)"/>',
        f'<rect width="{n(w)}" height="{n(h)}" fill="url(#mm5)"/>',
        "".join(band_svg),
        base_grid(grid, theme, opacity=0.42 if theme == "light" else 0.26, rx=3.2, sweep=0.011,
                  mode="pulse"),
        f'<path d="{trace}" pathLength="1000" class="trace" fill="none" stroke="{t["accent"]}" '
        f'stroke-width="1.6" stroke-linejoin="round" opacity=".55"/>',
        f'<path d="{trace}" pathLength="1000" class="lead" fill="none" stroke="{t["title"]}" '
        f'stroke-width="2.6" stroke-linecap="round" opacity=".95"/>',
        "".join(peaks),
        f'<path class="heart" d="M{n(LEFT - 24)} {n(TOP - 22)} c-2.6 -3.4 1.8 -7 4.4 -3.4 '
        f'c2.6 -3.6 7 0 4.4 3.4 l-4.4 5.2 z" fill="{t["accent"]}" opacity=".9"/>',
        header(w, theme, "contribution ecg",
               f"{user} · one beat per day · {stats['days']}d window", chips),
        month_labels(grid, w, theme),
        footer(w, h, theme, "amplitude = commits that day · the bright head sweeps the whole year"),
    ]
    return (open_svg(w, h, theme, anim_css, defs, "contribution ecg",
                     f'{stats["total"]} contributions'), "".join(body), w, h)


# --------------------------------------------------------------------------- #
# mode: life  -  Conway's Game of Life, seeded by your real commits
# --------------------------------------------------------------------------- #

def life_step(alive, W=53, H=7):
    """toroidal neighbourhoods, so patterns never die in a corner"""
    from collections import Counter
    cnt = Counter()
    for (c, r) in alive:
        for dc in (-1, 0, 1):
            for dr in (-1, 0, 1):
                if dc == dr == 0:
                    continue
                cnt[((c + dc) % W, (r + dr) % H)] += 1
    return {p for p, k in cnt.items() if k == 3 or (k == 2 and p in alive)}


def render_life(grid, theme, stats, user, weeks, anim=True, gens=16, freeze=0):
    """cellular automaton + a real ECG monitor sweep that eats the year and lets it regrow"""
    import math
    t = THEMES[theme]
    lv = LEVELS[theme]
    W = len(grid)
    w = LEFT + W * PITCH + PAD_R
    h = TOP + ROWS * PITCH + 40
    gh = ROWS * PITCH - GAP
    mid = TOP + gh * 0.56
    seed = {(c, r) for c, col in enumerate(grid) for r, d in enumerate(col)
            if not d["future"] and d["count"] > 0}

    # ---------------- life: the automaton grows faintly around real days
    frames, alive = [], set(seed or {(0, 3)})
    for _ in range(gens):
        frames.append(set(alive))
        alive = life_step(alive, W, ROWS) or set(seed or {(0, 3)})
    step = 0.6
    gdur = step * gens

    # ---------------- ECG: one PQRST complex per week, amplitude = commits
    wk = [sum(d["count"] for d in col if not d["future"]) for col in grid]
    top = max(wk) or 1
    amp = gh * 0.5
    rnd = 7

    def jitter():
        nonlocal rnd
        rnd = (rnd * 1103515245 + 12345) % 2 ** 31
        return rnd / 2 ** 31 - 0.5

    def g(x, mu, s):
        return math.exp(-((x - mu) / s) ** 2)

    pts, col_len, L, prev = [], [], 0.0, None
    S = 30
    for c in range(W):
        a = amp * (wk[c] / top) ** 0.55 if wk[c] else 0.0
        shift = jitter() * 0.08               # heart-rate variability
        col_len.append(L)
        for i in range(S):
            u = i / S
            x = LEFT + c * PITCH - GAP / 2 + u * PITCH
            v = 0.0
            if a:
                m = 0.5 + shift
                v = (0.10 * g(u, m - 0.22, 0.045) - 0.12 * g(u, m - 0.045, 0.02)
                     + 1.0 * g(u, m, 0.026) - 0.24 * g(u, m + 0.045, 0.022)
                     + 0.20 * g(u, m + 0.26, 0.07)) * a
            v += 0.35 * math.sin((c * S + i) / 37.0)   # faint baseline wander
            y = mid - v
            if prev:
                L += math.hypot(x - prev[0], y - prev[1])
            prev = (x, y)
            pts.append(f"{x:.1f} {y:.1f}")
    d_ecg = "M" + "L".join(pts)
    frac = [l / L for l in col_len]

    ink = t["title"]
    glow = t["accent"]
    loop, run = 11.0, 0.78                 # seconds, share of the loop spent sweeping
    PL = 1000

    # ---------------- tiles: quiet empty grid + real days (these get eaten)
    tiles, real, grow = [], [], []
    for c, col in enumerate(grid):
        for r, d in enumerate(col):
            if d["future"]:
                continue
            x, y = LEFT + c * PITCH, TOP + r * PITCH
            tiles.append(f'<rect x="{n(x)}" y="{n(y)}" width="{n(CELL)}" height="{n(CELL)}" rx="3"/>')
            if d["count"] > 0:
                real.append(f'<rect class="k k{c}" x="{n(x)}" y="{n(y)}" width="{n(CELL)}" '
                            f'height="{n(CELL)}" rx="3" fill="{lv[max(1, d["level"])]}"/>')
    shown = range(gens) if anim else [0]
    for gi in shown:
        cells = "".join(f'<rect class="k k{c}" x="{n(LEFT + c * PITCH + 4)}" y="{n(TOP + r * PITCH + 4)}" '
                        f'width="5" height="5" rx="1.5"/>' for c, r in sorted(frames[gi] - seed))
        delay = f' style="animation-delay:{n(-gi * step)}s"' if anim else ""
        grow.append(f'<g class="gen"{delay}>{cells}</g>')

    # ---------------- chrome: minimal text, no chips
    total = stats["total"]
    bpm = round(60 * W / (loop * run))
    mono = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
    last_month = None
    months = []
    for c, col in enumerate(grid):
        d0 = col[0]["date"]
        if d0.month != last_month and d0.day <= 7:
            last_month = d0.month
            if c < W - 2:
                months.append(f'<text x="{n(LEFT + c * PITCH)}" y="{n(TOP - 9)}">{d0.strftime("%b").lower()}</text>')
    chrome = (
        f'<text x="{n(LEFT)}" y="30" font-family="{mono}" font-size="12" fill="{t["title"]}" '
        f'letter-spacing=".4">{esc(user)}<tspan fill="{t["dim"]}"> / pulse</tspan></text>'
        f'<g font-family="{mono}" font-size="11" text-anchor="end">'
        f'<circle class="beat" cx="{n(w - PAD_R - 118)}" cy="26" r="3" fill="{glow}"/>'
        f'<text x="{n(w - PAD_R)}" y="30" fill="{t["title"]}">{bpm}<tspan fill="{t["dim"]}"> bpm</tspan>'
        f'<tspan fill="{t["dim"]}">  ·  </tspan>{total}<tspan fill="{t["dim"]}"> commits</tspan></text></g>'
        f'<g font-family="{mono}" font-size="9" fill="{t["dim"]}">{"".join(months)}</g>'
        f'<text x="{n(LEFT)}" y="{n(h - 14)}" font-family="{mono}" font-size="9" fill="{t["dim"]}">'
        f'one beat per week · height is commits · flatline is silence</text>'
        f'<text x="{n(w - PAD_R)}" y="{n(h - 14)}" font-family="{mono}" font-size="9" fill="{t["dim"]}" '
        f'text-anchor="end">b3/s23</text>'
    )

    defs = (f'<filter id="gl" x="-5%" y="-40%" width="110%" height="180%">'
            f'<feGaussianBlur stdDeviation="2.2"/></filter>'
            f'<radialGradient id="hg"><stop offset="0" stop-color="{glow}" stop-opacity=".55"/>'
            f'<stop offset="1" stop-color="{glow}" stop-opacity="0"/></radialGradient>')

    defs += (f'<path id="ecg" d="{d_ecg}" pathLength="{PL}" fill="none" '
             f'stroke-linejoin="round" stroke-linecap="round"/>')
    path_attr = 'href="#ecg"'
    if anim:
        ecg = (f'<use class="tr" {path_attr} stroke="{glow}" stroke-width="3" opacity=".35" filter="url(#gl)"/>'
               f'<use class="tr" {path_attr} stroke="{ink}" stroke-width="1.15"/>'
               f'<use class="cm" {path_attr} stroke="{glow}" stroke-width="2.4" filter="url(#gl)"/>'
               f'<use class="cm" {path_attr} stroke="{ink}" stroke-width="1.6"/>'
               f'<g class="hd"><circle r="9" fill="url(#hg)"/><circle r="2.1" fill="{ink}"/></g>')
    else:
        ecg = (f'<use {path_attr} stroke="{glow}" stroke-width="3" opacity=".3" filter="url(#gl)"/>'
               f'<use {path_attr} stroke="{ink}" stroke-width="1.15"/>')

    css = ""
    if anim:
        R = run * 100
        css = (
            # trace is drawn by the head, persists like phosphor, then fades out
            f"@keyframes draw{{0%{{stroke-dashoffset:{PL};opacity:1}}{n(R)}%{{stroke-dashoffset:0;opacity:1}}"
            f"{n(R + 6)}%{{stroke-dashoffset:0;opacity:1}}100%{{stroke-dashoffset:0;opacity:0}}}}"
            f".tr{{stroke-dasharray:{PL} {PL};animation:draw {n(loop)}s linear infinite}}"
            # bright comet riding the leading edge
            f"@keyframes cm{{0%{{stroke-dashoffset:24;opacity:1}}{n(R)}%{{stroke-dashoffset:{24 - PL};opacity:1}}"
            f"{n(R + .5)}%,100%{{stroke-dashoffset:{24 - PL};opacity:0}}}}"
            f".cm{{stroke-dasharray:24 {PL * 2};animation:cm {n(loop)}s linear infinite}}"
            # head follows the exact waveform
            f"@keyframes hd{{0%{{offset-distance:0%;opacity:1}}{n(R)}%{{offset-distance:100%;opacity:1}}"
            f"{n(R + 1.5)}%,100%{{offset-distance:100%;opacity:0}}}}"
            f".hd{{offset-path:path('{d_ecg}');offset-rotate:0deg;animation:hd {n(loop)}s linear infinite}}"
            # a day gets bitten: flash, pop, collapse, then regrow quietly
            f"@keyframes eat{{0%{{transform:scale(1);opacity:1}}.8%{{transform:scale(1.35);opacity:1}}"
            f"3.5%{{transform:scale(0);opacity:0}}52%{{transform:scale(0);opacity:0}}"
            f"60%{{transform:scale(1.08);opacity:1}}64%,100%{{transform:scale(1);opacity:1}}}}"
            f".k{{transform-box:fill-box;transform-origin:center;"
            f"animation:eat {n(loop)}s cubic-bezier(.3,.7,.3,1) infinite backwards}}"
            f"@keyframes gen{{0%,{n(100 / gens)}%{{opacity:1}}{n(100 / gens + .01)}%,100%{{opacity:0}}}}"
            f".gen{{opacity:0;animation:gen {n(gdur)}s steps(1,end) infinite}}"
            f"@keyframes beat{{0%,100%{{opacity:.25;transform:scale(1)}}8%{{opacity:1;transform:scale(1.5)}}"
            f"30%{{opacity:.25;transform:scale(1)}}}}"
            f".beat{{transform-box:fill-box;transform-origin:center;animation:beat {n(loop * run / W)}s ease-out infinite}}"
        )
        css += "".join(f".k{c}{{animation-delay:{n(loop * run * frac[c] + 0.02)}s}}" for c in range(W))
        css += ("@media (prefers-reduced-motion:reduce){*{animation:none!important}"
                ".cm,.hd{display:none}.gen{opacity:0}.gen:first-child{opacity:1}}")

    body = [
        f'<g fill="{lv[0]}" opacity="{".55" if theme == "dark" else ".7"}">{"".join(tiles)}</g>',
        f'<g fill="{t["accent"]}" opacity="{".28" if theme == "dark" else ".35"}">{"".join(grow)}</g>',
        f'<g>{"".join(real)}</g>',
        ecg,
        chrome,
    ]
    return (open_svg(w, h, theme, css, defs, "contribution pulse",
                     f"{total} contributions as a heartbeat over a cellular automaton"),
            "".join(body), w, h)


MODES = {"night": render_night, "climate": render_climate, "pulse": render_pulse, "life": render_life}


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], prog="contribgraph.py")
    ap.add_argument("--user", default=os.environ.get("GH_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "")
    ap.add_argument("--out", default="assets", help="output directory (default: assets)")
    ap.add_argument("--modes", default="night,climate,pulse,life")
    ap.add_argument("--weeks", type=int, default=53)
    ap.add_argument("--json", dest="json_path", default="", help="cache file (default: <out>/contributions.json)")
    ap.add_argument("--offline", action="store_true", help="never touch the network, only use the cache")
    ap.add_argument("--refresh", action="store_true", help="re-fetch even if the cache is from today")
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--today", default="", help="override 'today' (YYYY-MM-DD) for reproducible output")
    ap.add_argument("--stdout", action="store_true", help="print the SVG instead of writing files")
    ap.add_argument("--no-anim", dest="anim", action="store_false",
                    help="static snapshot: no CSS animation (useful for OG images / previews)")
    ap.add_argument("--gens", type=int, default=16, help="generations for --modes life (default 16)")
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.json_path) if args.json_path else out / "contributions.json"

    user = args.user or "you"
    days, source = load_days(user, today, cache, args.offline, args.refresh)
    grid = build_window(days, today, args.weeks)
    stats = stats_of(grid)
    stats["source"] = source
    stats["user"] = user
    stats["today"] = today.isoformat()

    if not args.stdout and source.startswith(("live", "cache(stale")):
        cache.write_text(json.dumps({"days": days, "stats": stats}, indent=1, sort_keys=True))

    written = []
    for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
        if mode not in MODES:
            raise SystemExit(f"unknown mode '{mode}' (have: {', '.join(MODES)})")
        for theme in [x.strip() for x in args.themes.split(",") if x.strip()]:
            kw = {"anim": args.anim}
            if mode == "life":
                kw["gens"] = args.gens
            head, body, w, h = MODES[mode](grid, theme, stats, user, args.weeks, **kw)
            svg = head + body + close_svg()
            if args.stdout:
                print(svg)
                return 0
            suffix = "" if args.anim else "-static"
            path = out / f"graph-{mode}-{theme}{suffix}.svg"
            path.write_text(svg)
            written.append((path, len(svg)))
    for path, size in written:
        flag = "  <-- keep under ~200KB" if size > 200_000 else ""
        print(f"{path}  {size / 1024:.1f} KB{flag}")
    print(f"data source: {source} · {stats['total']} commits · {stats['active']} active days · "
          f"best streak {stats['streak']}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
