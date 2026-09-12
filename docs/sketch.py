"""The README's how-it-works diagram, as a hand-sketched SVG: rough strokes and a
handwriting font stack, deterministic (seeded). Regenerate with

    python docs/sketch.py

after changing the flow; it writes docs/how-it-works-dark.svg: light ink on a dark card,
the ground baked in so it looks the same on a light page and a dark one."""
import random, os

FONT = "'Segoe Print','Bradley Hand','Comic Sans MS','Chalkboard SE',cursive"
W, H = 980, 476


def jitter(r, a=1.6):
    return r.uniform(-a, a)


def rough_line(r, x1, y1, x2, y2, n=3):
    """A line drawn as n wobbly segments, twice, slightly offset."""
    out = []
    for pass_ in range(2):
        pts = [(x1 + jitter(r, 1.0), y1 + jitter(r, 1.0))]
        for i in range(1, n):
            t = i / n
            pts.append((x1 + (x2 - x1) * t + jitter(r), y1 + (y2 - y1) * t + jitter(r)))
        pts.append((x2 + jitter(r, 1.0), y2 + jitter(r, 1.0)))
        d = "M" + " L".join("%.1f %.1f" % p for p in pts)
        out.append(d)
    return out


def rough_rect(r, x, y, w, h):
    ds = []
    ds += rough_line(r, x, y, x + w, y)
    ds += rough_line(r, x + w, y, x + w, y + h)
    ds += rough_line(r, x + w, y + h, x, y + h)
    ds += rough_line(r, x, y + h, x, y)
    return ds


def dashed_rect(r, x, y, w, h):
    """One wobbly pass around a rectangle - a boundary, not a box."""
    ds = []
    for a, b, c, d in ((x, y, x + w, y), (x + w, y, x + w, y + h),
                       (x + w, y + h, x, y + h), (x, y + h, x, y)):
        ds.append(rough_line(r, a, b, c, d, 4)[0])
    return ds


def arrow(r, x1, y1, x2, y2, bend=0.0):
    """A slightly curved arrow with a hand-drawn head."""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    L = (dx * dx + dy * dy) ** 0.5 or 1
    nx, ny = -dy / L, dx / L
    cx, cy = mx + nx * bend + jitter(r, 3), my + ny * bend + jitter(r, 3)
    ds = []
    for _ in range(2):
        ds.append("M%.1f %.1f Q%.1f %.1f %.1f %.1f" % (x1 + jitter(r), y1 + jitter(r),
                                                   cx, cy, x2 + jitter(r), y2 + jitter(r)))
    # head: direction from control point to tip
    hx, hy = x2 - cx, y2 - cy
    hl = (hx * hx + hy * hy) ** 0.5 or 1
    ux, uy = hx / hl, hy / hl
    px, py = -uy, ux
    for s in (1, -1):
        bx = x2 - ux * 12 + px * 6 * s
        by = y2 - uy * 12 + py * 6 * s
        ds += rough_line(r, x2, y2, bx, by, 1)
    return ds


def text(x, y, s, size=15, anchor="start", cls="ink", italic=False, rot=0.0):
    style = " font-style='italic'" if italic else ""
    tr = " transform='rotate(%.1f %.1f %.1f)'" % (rot, x, y) if rot else ""
    return ("<text x='%.1f' y='%.1f' font-size='%d' text-anchor='%s' class='%s'%s%s>%s</text>"
            % (x, y, size, anchor, cls, style, tr, s))


def build(dark):
    r = random.Random(7)
    ink = "#e5e7eb" if dark else "#1f2937"
    soft = "#9ca3af" if dark else "#6b7280"
    accent = "#fbbf24" if dark else "#b45309"
    fill = "rgba(251,191,36,0.08)" if dark else "rgba(180,83,9,0.06)"
    out = ["<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' width='%d' height='%d'>" % (W, H, W, H),
           "<style>",
           "text{font-family:%s}" % FONT,
           ".ink{fill:%s}.soft{fill:%s}.acc{fill:%s}" % (ink, soft, accent),
           ".s{fill:none;stroke:%s;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}" % ink,
           ".sa{fill:none;stroke:%s;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}" % accent,
           ".ss{fill:none;stroke:%s;stroke-width:1.3;stroke-linecap:round;stroke-linejoin:round}" % soft,
           ".sb{fill:none;stroke:%s;stroke-width:1.3;stroke-linecap:round;stroke-dasharray:11 9}" % soft,
           "</style>",
           # the dark ground is baked in, so the sketch is the same dark card on any
           # page - GitHub light, GitHub dark, a VS Code preview.
           "<rect x='0' y='0' width='%d' height='%d' rx='14' fill='#1f1f1f'/>" % (W, H)]

    def paths(ds, cls="s"):
        for d in ds:
            out.append("<path class='%s' d='%s'/>" % (cls, d))

    # column titles
    out.append(text(60, 34, "harvest side", 17, rot=-0.6))
    out.append(text(178, 34, "- runs on its own, nightly", 14, cls="soft"))
    out.append(text(560, 34, "query side", 17, rot=0.5))
    out.append(text(662, 34, "- anywhere, when asked", 14, cls="soft"))

    # the platform boundary: everything the harvest touches is within it
    paths(dashed_rect(r, 44, 50, 380, 408), "sb")
    out.append(text(414, 272, "inside the platform", 13, anchor="middle", cls="soft",
                    italic=True, rot=-90))

    # A: Fabric workspace
    paths(rough_rect(r, 60, 62, 340, 92))
    out.append(text(78, 90, "Fabric workspace", 17))
    out.append(text(78, 114, "models, reports, notebooks, pipelines,", 14, cls="soft"))
    out.append(text(78, 134, "usage, query log", 14, cls="soft"))

    # A -> B
    paths(arrow(r, 230, 156, 230, 206, bend=4))
    out.append(text(244, 186, "python src/run.py all", 13, cls="soft"))

    # B: raw -> graph -> rank
    paths(rough_rect(r, 110, 208, 240, 44))
    out.append(text(230, 237, "raw  →  graph  →  rank", 16, anchor="middle"))

    # B -> C
    paths(arrow(r, 230, 254, 230, 330, bend=-4))

    # C: the context (accent)
    ds = rough_rect(r, 60, 332, 340, 118)
    out.append("<path d='M62 334 h336 v114 h-336 z' fill='%s' stroke='none'/>" % fill)
    paths(ds, "sa")
    out.append(text(78, 362, "the context", 18, cls="acc", rot=-0.8))
    out.append(text(190, 362, "(one lakehouse, for now)", 13, cls="soft", italic=True))
    out.append(text(78, 392, "Tables/", 15))
    out.append(text(150, 392, "the ranked graph", 14, cls="soft"))
    out.append(text(78, 416, "Files/", 15))
    out.append(text(150, 416, "raw, wiki, context.md, graph.html", 14, cls="soft"))
    out.append(text(78, 440, "one per tenant, hidden - ideally", 12, cls="soft", italic=True))

    # D: agent
    paths(rough_rect(r, 560, 62, 360, 92))
    out.append(text(578, 90, "any agent, stateless", 17))
    out.append(text(578, 118, "“what was avg price in NSW?”", 15, cls="soft", italic=True))
    out.append(text(578, 140, "a laptop, a notebook, CI, a chat - outside", 12, cls="soft"))

    # D -> E
    paths(arrow(r, 740, 156, 740, 206, bend=-4))
    out.append(text(754, 186, "python -m ask", 13, cls="soft"))

    # E: search -> define -> rank 1
    paths(rough_rect(r, 580, 208, 320, 44))
    out.append(text(740, 237, "read context.md  →  rank 1", 16, anchor="middle"))

    # E -> C  (reads)
    paths(arrow(r, 578, 236, 404, 372, bend=28), "ss")
    out.append(text(474, 288, "reads in, from outside", 13, cls="soft", rot=-8))

    # E -> F
    paths(arrow(r, 740, 254, 740, 318, bend=5))
    out.append(text(754, 292, "DAX, calling rank 1 by name", 13, cls="soft"))

    # F: the model
    paths(rough_rect(r, 580, 320, 320, 44))
    out.append(text(740, 349, "the semantic model that owns it", 15, anchor="middle"))

    # F -> G
    paths(arrow(r, 740, 366, 740, 420, bend=-4))

    # G: answer (underlined, no box)
    out.append(text(740, 448, "number  +  source  +  confidence", 17, anchor="middle", rot=0.6))
    paths(rough_line(r, 612, 458, 868, 460, 4), "ss")

    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "how-it-works-dark.svg")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(build(dark=True))
    print("wrote", dest)
