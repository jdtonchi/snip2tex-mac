"""Diagrams for the graph and ? buttons.

Gemma looks at the selection and picks a kind (route), then fills in a small
JSON spec for it; Python validates the spec, does the maths and layout, and
draws it with matplotlib. The model never draws or calculates.

Kinds: function graphs (snip2tex.render_graph), geometry, flowcharts/concept
maps, charts. The ? button can also pick "answer" (plain text instead).
"""
import io, json, math, re, textwrap

KINDS = {
    "function": "a function, equation or inequality to graph on axes (y = x^2, sin x and cos x, a circle x^2+y^2=9)",
    "geometry": "a geometric figure: triangle, right triangle, circle, rectangle, square, regular polygon "
                "(possibly with side lengths, angles, and a ? to find)",
    "flowchart": "a process, steps, cycle, hierarchy, decision tree, or concept map: things connected by arrows or lines; "
                 "also a labelled diagram of a thing and its parts (a cell, the heart, an atom) or of how something works",
    "chart": "data to plot: a table or list of numbers, measurements over time, categories with values, percentages",
}

ROUTE_PROMPT = """This is a snippet from a student's handwritten notes on an e-ink tablet.
{task}
Kinds:
{kinds}
Examples:
- "graph y = x^2 - 4" -> function
- "sin x and cos x, plot" -> function
- a triangle sketch with sides 3, 4 and a "?" -> geometry
- "circle r = 5, area?" -> geometry
- "flowchart of mitosis" -> flowchart
- "concept map of the causes of WW1" -> flowchart
- boxes joined by arrows, or ideas joined by lines -> flowchart
- "make a diagram of the water cycle" -> flowchart
- "diagram of a plant cell" -> flowchart
- "how do vaccines work? draw it" -> flowchart
- "Mon 72, Tue 85, Wed 90 bar chart" -> chart
- a table of numbers -> chart
{answer_examples}First copy what is written (short), then give the kind."""

ASK_TASK = """They tapped "ask". Decide whether to reply in words ("answer") or to draw a diagram.
Draw ONLY if they ask for a drawing (graph, plot, draw, sketch, chart, diagram, flowchart,
graphic, picture, illustrate, map out, visualize) or they sketched a figure/table that should be redrawn neatly or solved
(e.g. a triangle with a "?"). Questions, words, greetings and formulas get "answer"."""

DIAGRAM_TASK = """They want a clean diagram of it. Pick the kind of diagram that fits best."""

ANSWER_KIND = {"answer": "reply in words: a question, explanation, greeting, or anything not asking for a drawing"}


# asking for a drawing in so many words: never answered in words
DRAW_WORDS = re.compile(r"\b(draw|drawn|sketch|diagram|diagrams|chart|flow ?chart|graph|graphic|picture|plot|"
                        r"concept map|mind map|map out|visuali[sz]e|illustrate|illustration)\b", re.I)


def route(chat, png, allow_answer):
    kinds = dict(ANSWER_KIND, **KINDS) if allow_answer else KINDS
    answer_examples = ('- "what is entropy?" -> answer\n- "Hello" -> answer\n'
                       '- "d/dx sin(x^2)" -> answer\n') if allow_answer else ""
    prompt = ROUTE_PROMPT.format(task=ASK_TASK if allow_answer else DIAGRAM_TASK,
                                 kinds="\n".join(f"- {k}: {v}" for k, v in kinds.items()),
                                 answer_examples=answer_examples)
    # "written" comes first so the model reads the note before deciding, and
    # so an explicit "draw ..." can overrule an "answer"
    schema = {"type": "object", "properties": {"written": {"type": "string", "maxLength": 200},
                                               "kind": {"type": "string", "enum": list(kinds)}},
              "required": ["written", "kind"], "additionalProperties": False}
    out = json.loads(chat(prompt, image_png=png, schema=schema, temperature=0.0, max_tokens=120))
    written = plain(out.get("written", ""))
    if out["kind"] == "answer" and DRAW_WORDS.search(written):
        return route(chat, png, allow_answer=False)[0], written
    return out["kind"], written


LATEX_WORDS = {r"\\rightarrow": "→", r"\\to": "→", r"\\leftarrow": "←", r"\\Rightarrow": "⇒",
               r"\\times": "×", r"\\cdot": "·", r"\\pi": "π", r"\\theta": "θ", r"\\alpha": "α",
               r"\\beta": "β", r"\\Delta": "Δ", r"\\delta": "δ", r"\\lambda": "λ", r"\\mu": "μ",
               r"\\leq": "≤", r"\\geq": "≥", r"\\neq": "≠", r"\\approx": "≈", r"\\degree": "°"}


def plain(text):
    """Model-written labels sometimes contain LaTeX; matplotlib would try to
    render $...$ as math (and crash on bad input). Make it plain text."""
    text = str(text or "")
    # JSON already turned \rightarrow, \times, \frac, \beta into control chars
    for ctrl, esc in (("\r", "\\r"), ("\t", "\\t"), ("\x0c", "\\f"), ("\x08", "\\b")):
        text = text.replace(ctrl, esc)
    for k, v in LATEX_WORDS.items():
        text = re.sub(k + r"(?![A-Za-z])", v, text)
    text = re.sub(r"\\frac\{([^}]*)\}\{([^}]*)\}", r"\1/\2", text)
    text = re.sub(r"\\(?:text|mathrm|mathbf)\{([^}]*)\}", r"\1", text)
    text = text.replace("$", "").replace("\\", "").replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_strings(obj):
    if isinstance(obj, str):
        return plain(obj)
    if isinstance(obj, list):
        return [clean_strings(x) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in ("id", "from", "to", "chart", "figure", "shape", "direction", "fit",
                               "right_angle_at", "find") else clean_strings(v)) for k, v in obj.items()}
    return obj


def fmt(v):
    """3 significant figures, no trailing zeros; integers stay integers."""
    if v is None:
        return "?"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.3g}" if abs(v) < 1e5 else f"{v:.3e}"


def _fig(dpi, size=(5.2, 3.9)):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 14, "axes.linewidth": 1.5, "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=size, dpi=dpi)
    return plt, fig, ax


def _png(plt, fig):
    fig.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor="white")
    plt.close(fig)
    return out.getvalue()


# ---------- geometry ----------

NUM = {"type": ["number", "null"]}
GEOMETRY_SCHEMA = {
    "type": "object",
    "properties": {
        "figure": {"type": "string", "enum": ["triangle", "circle", "rectangle", "regular_polygon"]},
        "a": NUM, "b": NUM, "c": NUM, "A": NUM, "B": NUM, "C": NUM,
        "right_angle_at": {"type": "string", "enum": ["A", "B", "C", "none"]},
        "radius": NUM, "width": NUM, "height": NUM, "sides": {"type": ["integer", "null"]}, "side": NUM,
        "find": {"type": "array", "items": {"type": "string", "enum": [
            "a", "b", "c", "A", "B", "C", "area", "perimeter", "circumference", "diagonal", "diameter"]}},
    },
    "required": ["figure", "a", "b", "c", "A", "B", "C", "right_angle_at", "radius", "width", "height",
                 "sides", "side", "find"],
    "additionalProperties": False,
}

GEOMETRY_PROMPT = """Describe the geometry figure in this snippet of handwritten notes as a spec.
- Triangle: vertices A, B, C; side a is opposite A (BC), b opposite B (CA), c opposite C (AB).
  Fill in the side lengths and angles (in degrees) that are given; null for unknown.
  A right triangle: set right_angle_at to that vertex (usually C, with c the hypotenuse).
  Two legs 3 and 4 of a right triangle: a=3, b=4, right_angle_at=C.
  A triangle drawn with two sides labelled and a "?" on the longest side is a
  right triangle asking for the hypotenuse: a, b = the two sides, right_angle_at=C,
  find=["c"].
  The angle BETWEEN two sides is the one at the vertex they share, opposite the third
  side: between a and b is C. "Sides 5 and 7 with 40 degrees between them" (or an angle
  drawn where those two sides meet): a=5, b=7, C=40, find=["c", "A", "B"].
- Circle: radius (or width = diameter). Rectangle/square: width, height.
- Regular polygon: sides = number of sides, side = side length.
- find: what is asked for (anything marked "?" or asked in words); if nothing is
  asked, list the most useful unknowns.
Only use numbers that are written; never invent values."""


def repair_geometry(spec, written):
    """Two sides and an angle "between them": the model often puts the angle at
    the wrong vertex (b, c and C instead of A). Move it to where the sides meet."""
    if spec.get("figure") != "triangle" or not re.search(r"\bbetween\b|\bincluded\b", written or "", re.I):
        return spec
    sides = [k for k in "abc" if spec.get(k) not in (None, "")]
    angles = [k for k in "ABC" if spec.get(k) not in (None, "")]
    if len(sides) == 2 and len(angles) == 1:
        included = next(k for k in "abc" if k not in sides).upper()
        if angles[0] != included:
            spec[included], spec[angles[0]] = spec[angles[0]], None
            spec["find"] = [included.lower()] + [k for k in "ABC" if k != included]
    return spec


def _solve_triangle(s):
    """Fill in a, b, c, A, B, C (degrees) from whatever is known. Raises ValueError."""
    t = {k: (float(s[k]) if s.get(k) not in (None, "") else None) for k in "abcABC"}
    if s.get("right_angle_at") in ("A", "B", "C"):
        t[s["right_angle_at"]] = 90.0
    rad, deg = math.radians, math.degrees
    pairs = [("a", "A"), ("b", "B"), ("c", "C")]
    for _ in range(6):
        angles = [t[x] for x in "ABC"]
        if sum(v is not None for v in angles) == 2:
            missing = "ABC"[angles.index(None)]
            t[missing] = 180.0 - sum(v for v in angles if v is not None)
        sides = [t[x] for x in "abc"]
        if all(v is not None for v in sides):
            a, b, c = sides
            for side, ang, o1, o2 in (("a", "A", b, c), ("b", "B", c, a), ("c", "C", a, b)):
                if t[ang] is None:
                    x = (o1 ** 2 + o2 ** 2 - t[side] ** 2) / (2 * o1 * o2)
                    if not -1 <= x <= 1:
                        raise ValueError("those sides can't make a triangle")
                    t[ang] = deg(math.acos(x))
        # two sides and the angle between them -> third side
        for side, ang in pairs:
            o = [x for x in "abc" if x != side]
            if t[side] is None and t[ang] is not None and t[o[0]] is not None and t[o[1]] is not None:
                t[side] = math.sqrt(t[o[0]] ** 2 + t[o[1]] ** 2 - 2 * t[o[0]] * t[o[1]] * math.cos(rad(t[ang])))
        # law of sines with any complete side/angle pair
        known = next(((t[s_], t[a_]) for s_, a_ in pairs if t[s_] is not None and t[a_] is not None), None)
        if known:
            ratio = known[0] / math.sin(rad(known[1]))
            for side, ang in pairs:
                if t[side] is None and t[ang] is not None:
                    t[side] = ratio * math.sin(rad(t[ang]))
                elif t[ang] is None and t[side] is not None:
                    x = t[side] / ratio
                    if x > 1 + 1e-9:
                        raise ValueError("no triangle has those measurements")
                    t[ang] = deg(math.asin(min(1.0, x)))  # acute solution of the ambiguous case
        if all(t[k] is not None for k in "abcABC"):
            break
    if not all(t[k] is not None for k in "abcABC"):
        raise ValueError("not enough measurements to pin down the triangle")
    if any(t[k] <= 0 for k in "abcABC") or abs(t["A"] + t["B"] + t["C"] - 180) > 0.5:
        raise ValueError("those measurements don't make a triangle")
    return t


def render_geometry(spec, dpi):
    from matplotlib.patches import Arc, Circle, Polygon
    plt, fig, ax = _fig(dpi, (5.2, 4.2))
    find = list(dict.fromkeys(spec.get("find") or []))
    results, fig_kind = {}, spec["figure"]
    asked = lambda k: k in find  # noqa: E731

    def side_label(p, q, text, bold, inside):
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        dx, dy = q[0] - p[0], q[1] - p[1]
        n = math.hypot(dx, dy) or 1
        nx, ny = dy / n, -dx / n  # normal; flip to point away from the inside point
        if (mx + nx - inside[0]) ** 2 + (my + ny - inside[1]) ** 2 < (mx - inside[0]) ** 2 + (my - inside[1]) ** 2:
            nx, ny = -nx, -ny
        ax.annotate(text, (mx, my), xytext=(nx * 16, ny * 16), textcoords="offset points",
                    ha="center", va="center", fontsize=14, fontweight="bold" if bold else "normal")

    if fig_kind == "triangle":
        t = _solve_triangle(spec)
        A = (0.0, 0.0)
        B = (t["c"], 0.0)
        C = (t["b"] * math.cos(math.radians(t["A"])), t["b"] * math.sin(math.radians(t["A"])))
        P = {"A": A, "B": B, "C": C}
        ax.add_patch(Polygon([A, B, C], closed=True, fill=False, lw=2.8, ec="black"))
        cx, cy = (A[0] + B[0] + C[0]) / 3, (A[1] + B[1] + C[1]) / 3
        for name, (x, y) in P.items():
            dx, dy = x - cx, y - cy
            n = math.hypot(dx, dy) or 1
            ax.annotate(name, (x, y), xytext=(dx / n * 14, dy / n * 14), textcoords="offset points",
                        ha="center", va="center", fontsize=15)
        for side, (p, q) in {"a": (B, C), "b": (C, A), "c": (A, B)}.items():
            given = spec.get(side) is not None
            if given or asked(side):
                side_label(p, q, fmt(t[side]) if given else f"{side} = {fmt(t[side])}", not given, (cx, cy))
            if asked(side):
                results[side] = fmt(t[side])
        size = max(t["a"], t["b"], t["c"])
        for ang, at in (("A", A), ("B", B), ("C", C)):
            others = [P[k] for k in "ABC" if k != ang]
            dirs = [math.degrees(math.atan2(o[1] - at[1], o[0] - at[0])) for o in others]
            lo, hi = sorted(dirs)
            if hi - lo > 180:
                lo, hi = hi, lo + 360
            given = spec.get(ang) is not None or spec.get("right_angle_at") == ang
            if abs(t[ang] - 90) < 0.5 and (given or asked(ang)):
                r = size * 0.07
                u = [((o[0] - at[0]) / math.dist(o, at), (o[1] - at[1]) / math.dist(o, at)) for o in others]
                sq = [(at[0] + u[0][0] * r, at[1] + u[0][1] * r),
                      (at[0] + (u[0][0] + u[1][0]) * r, at[1] + (u[0][1] + u[1][1]) * r),
                      (at[0] + u[1][0] * r, at[1] + u[1][1] * r)]
                ax.plot(*zip(*sq), color="black", lw=1.5)
            elif given or asked(ang):
                r = size * 0.18
                ax.add_patch(Arc(at, 2 * r, 2 * r, theta1=lo, theta2=hi, lw=1.5))
                mid = math.radians((lo + hi) / 2)
                label = f"{fmt(t[ang])}°" if given else f"{ang} = {fmt(t[ang])}°"
                ax.text(at[0] + math.cos(mid) * r * 1.6, at[1] + math.sin(mid) * r * 1.6, label,
                        ha="center", va="center", fontsize=12, fontweight="normal" if given else "bold")
            if asked(ang):
                results[ang] = fmt(t[ang]) + "°"
        s_ = (t["a"] + t["b"] + t["c"]) / 2
        extra = {"area": math.sqrt(max(0.0, s_ * (s_ - t["a"]) * (s_ - t["b"]) * (s_ - t["c"]))),
                 "perimeter": t["a"] + t["b"] + t["c"]}
        pts = [A, B, C]
    elif fig_kind == "circle":
        r = spec.get("radius") or ((spec.get("width") or 0) / 2) or None
        if not r:
            raise ValueError("no radius given")
        ax.add_patch(Circle((0, 0), r, fill=False, lw=2.8))
        ax.plot([0, r], [0, 0], color="black", lw=1.8)
        ax.plot([0], [0], "o", color="black", ms=4)
        ax.annotate(f"r = {fmt(r)}", (r / 2, 0), xytext=(0, 10), textcoords="offset points", ha="center")
        extra = {"area": math.pi * r * r, "circumference": 2 * math.pi * r, "diameter": 2 * r}
        pts = [(-r, -r), (r, r)]
    elif fig_kind == "rectangle":
        w, h = spec.get("width"), spec.get("height")
        if not w or not h:
            raise ValueError("need a width and a height")
        ax.add_patch(Polygon([(0, 0), (w, 0), (w, h), (0, h)], closed=True, fill=False, lw=2.8))
        side_label((0, 0), (w, 0), fmt(w), False, (w / 2, h / 2))
        side_label((w, 0), (w, h), fmt(h), False, (w / 2, h / 2))
        if asked("diagonal"):
            ax.plot([0, w], [0, h], color="black", lw=1.5, ls="--")
        extra = {"area": w * h, "perimeter": 2 * (w + h), "diagonal": math.hypot(w, h)}
        pts = [(0, 0), (w, h)]
    else:  # regular polygon
        n, s1 = spec.get("sides"), spec.get("side")
        if not n or n < 3 or not s1:
            raise ValueError("need the number of sides and the side length")
        R = s1 / (2 * math.sin(math.pi / n))
        verts = [(R * math.cos(2 * math.pi * k / n + math.pi / 2), R * math.sin(2 * math.pi * k / n + math.pi / 2))
                 for k in range(n)]
        ax.add_patch(Polygon(verts, closed=True, fill=False, lw=2.8))
        side_label(verts[0], verts[1], fmt(s1), False, (0, 0))
        apothem = s1 / (2 * math.tan(math.pi / n))
        extra = {"area": n * s1 * apothem / 2, "perimeter": n * s1}
        pts = verts

    for k in find:
        if k in extra and k not in results:
            results[k] = fmt(extra[k])
    if not find and fig_kind != "triangle":
        results["area"] = fmt(extra["area"])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    ax.set_xlim(min(xs) - span * 0.25, max(xs) + span * 0.25)
    ax.set_ylim(min(ys) - span * 0.25, max(ys) + span * 0.25)
    ax.set_aspect("equal")
    ax.axis("off")
    caption = ", ".join(f"{k} = {v}" for k, v in results.items())
    if caption:
        ax.set_title(caption, fontsize=15, fontweight="bold")
    return _png(plt, fig), caption


# ---------- flowcharts / concept maps ----------

FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "direction": {"type": "string", "enum": ["down", "right"]},
        "nodes": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "label": {"type": "string"},
            "shape": {"type": "string", "enum": ["box", "rounded", "diamond", "ellipse"]}},
            "required": ["id", "label", "shape"], "additionalProperties": False}},
        "edges": {"type": "array", "items": {"type": "object", "properties": {
            "from": {"type": "string"}, "to": {"type": "string"}, "label": {"type": "string"}},
            "required": ["from", "to", "label"], "additionalProperties": False}},
    },
    "required": ["title", "direction", "nodes", "edges"],
    "additionalProperties": False,
}

FLOW_PROMPT = """Turn this snippet of handwritten notes into a clean flowchart or concept map spec.
- One node per box, bubble or idea; keep the student's wording, short.
- Edges follow the arrows/lines; use "label" for words written on an arrow (else "").
- Shapes: diamond for yes/no decisions, ellipse for concepts in a concept map or
  start/end, rounded for steps, box otherwise.
- direction: "down" for processes and hierarchies, "right" for timelines.
- A cycle (water cycle, cell cycle, Krebs cycle): the steps in order, with an edge
  from the last step back to the first.
- If they only wrote a topic and asked for a diagram of it, make a short, correct one
  (at most 8 nodes) from what you know:
  - a process or "how X works": the steps in order;
  - a thing with parts (a plant cell, the heart, an atom): the thing in the middle
    (ellipse) with an edge to each part; the part's label is its name and a few words
    on its role, like "Chloroplast: photosynthesis"; edge labels empty."""


def _layers(ids, edges):
    """Longest-path layering; edges that close a cycle are ignored for layering."""
    out = {i: [] for i in ids}
    for f, t in edges:
        out[f].append(t)
    state, order, back = {}, [], set()

    def dfs(n):
        state[n] = 1
        for m in out[n]:
            if state.get(m) == 1:
                back.add((n, m))
            elif m not in state:
                dfs(m)
        state[n] = 2
        order.append(n)
    for i in ids:
        if i not in state:
            dfs(i)
    layer = {i: 0 for i in ids}
    for n in reversed(order):
        for m in out[n]:
            if (n, m) not in back:
                layer[m] = max(layer[m], layer[n] + 1)
    return layer


TOPIC_FLOW_PROMPT = """A student asked: "{request}"
Make the diagram they want as a flowchart or concept map spec, from what you know.
It must be correct and useful for studying: 4 to 8 nodes, short labels.
- A process or "how X works": the steps in order, edges between consecutive steps.
- A cycle: the steps in order, plus an edge from the last step back to the first.
- A thing with parts (a plant cell, the heart, an atom): the thing in the middle
  (ellipse) with an edge to each part; each part's label is its name and a few words
  on its role, like "Chloroplast: photosynthesis"; edge labels empty.
- Causes or effects: the event in the middle, an edge from each cause to it."""


def _longest_cycle(ids, edges, limit=20000):
    """Longest simple directed cycle (small graphs; the search is capped)."""
    out = {i: [] for i in ids}
    for f, t in edges:
        out[f].append(t)
    best, steps = [], [0]

    def dfs(start, path, seen):
        nonlocal best
        steps[0] += 1
        if steps[0] > limit:
            return
        for m in out[path[-1]]:
            if m == start and len(path) > len(best):
                best = list(path)
            elif m not in seen and ids.index(m) > ids.index(start):
                seen.add(m)
                path.append(m)
                dfs(start, path, seen)
                path.pop()
                seen.discard(m)
    for i in ids:
        dfs(i, [i], {i})
    return best


def _ring(n, box_w, box_h):
    """Radius that fits n boxes around a circle without touching: neighbours
    at the top and bottom sit side by side, so the chord must clear a box width."""
    return max(1.4, (box_w + 1.0) / (2 * math.sin(math.pi / n)), (box_h + 0.5) / math.sin(math.pi / n))


def _round_layout(ids, edges, box_w, box_h):
    """Cycles go around a circle, a hub with its parts goes in the middle of
    them. None when neither fits (then it's layered)."""
    n = len(ids)
    if n < 3:
        return None
    nbrs = {i: set() for i in ids}
    for f, t in edges:
        nbrs[f].add(t)
        nbrs[t].add(f)
    hub = max(ids, key=lambda i: len(nbrs[i]))
    if len(nbrs[hub]) >= max(3, n - 1) and len({frozenset(e) for e in edges}) <= n + 1:
        others = [i for i in ids if i != hub]
        r = _ring(len(others), box_w, box_h) + 0.6
        xy = {hub: (0.0, 0.0)}
        for k, i in enumerate(others):
            a = math.pi / 2 - 2 * math.pi * k / len(others)
            xy[i] = (r * math.cos(a), r * math.sin(a))
        return xy, "hub"
    cyc = _longest_cycle(ids, edges)
    if len(cyc) >= 3 and len(cyc) >= 0.6 * n:
        r = _ring(len(cyc), box_w, box_h)
        xy = {}
        for k, i in enumerate(cyc):  # clockwise from the top
            a = math.pi / 2 - 2 * math.pi * k / len(cyc)
            xy[i] = (r * math.cos(a), r * math.sin(a))
        # anything else sits outside, next to what it's connected to
        for i in ids:
            if i not in xy:
                anchor = next((j for j in nbrs[i] if j in cyc), cyc[0])
                ax_, ay_ = xy[anchor]
                d = math.hypot(ax_, ay_) or 1
                out_r = d + max(box_w, box_h) + 0.6
                while any(math.hypot(out_r * ax_ / d - x, out_r * ay_ / d - y) < box_w for x, y in xy.values()):
                    out_r += box_h + 0.4
                xy[i] = (out_r * ax_ / d, out_r * ay_ / d)
        return xy, "cycle"
    return None


def _is_arrow(label):
    return label.strip() in ("", "→", "->", "-->", "=>", "⇒", "↓", "→→")


def render_flowchart(spec, dpi):
    from matplotlib.patches import Ellipse, FancyArrowPatch, FancyBboxPatch, Polygon
    nodes = [n for n in spec.get("nodes", []) if n.get("id")][:20]
    if not nodes:
        raise ValueError("no boxes found")
    ids = list(dict.fromkeys(n["id"] for n in nodes))
    byid = {n["id"]: n for n in nodes}
    edges = [(e["from"], e["to"], "" if _is_arrow(e.get("label", "")) else e["label"]) for e in spec.get("edges", [])
             if e.get("from") in byid and e.get("to") in byid and e["from"] != e["to"]]
    layer = _layers(ids, [(f, t) for f, t, _ in edges])
    rows = {}
    for i in ids:
        rows.setdefault(layer[i], []).append(i)
    # a few barycenter passes to reduce crossings
    pos_in = {i: k for r in rows.values() for k, i in enumerate(r)}
    for _ in range(4):
        for L in sorted(rows):
            def bary(i):
                ps = [pos_in[f] for f, t, _ in edges if t == i and layer[f] < L]
                return sum(ps) / len(ps) if ps else pos_in[i]
            rows[L].sort(key=bary)
            pos_in.update({i: k for k, i in enumerate(rows[L])})

    down = spec.get("direction", "down") != "right"
    wrap = lambda s: "\n".join(textwrap.wrap(s, 16, break_long_words=False)) or " "  # noqa: E731
    labels = {i: wrap(byid[i]["label"]) for i in ids}
    widest = max(len(l) for i in ids for l in labels[i].split("\n"))
    box_w = max(1.6, min(3.2, 0.13 * widest + 0.5))
    box_h = 0.42 + 0.3 * max(labels[i].count("\n") + 1 for i in ids)
    round_ = _round_layout(ids, [(f, t) for f, t, _ in edges], box_w, box_h)
    if round_:
        xy, mode = round_
    else:
        mode = "layered"
        gap_along, gap_across = (box_h + 0.6, box_w + 0.5) if down else (box_w + 1.0, box_h + 0.6)
        xy = {}
        for L, r in rows.items():
            for k, i in enumerate(r):
                across = (k - (len(r) - 1) / 2) * gap_across
                xy[i] = (across, -L * gap_along) if down else (L * gap_along, -across)

    xs, ys = [p[0] for p in xy.values()], [p[1] for p in xy.values()]
    xlim = (min(xs) - box_w * 0.75, max(xs) + box_w * 0.75)
    ylim = (min(ys) - box_h * 1.1, max(ys) + box_h * 1.1 + (0.5 if spec.get("title") else 0))
    # one data unit = one inch at full size (box widths were sized for 12pt
    # text), scaled down together with the font if it won't fit a page
    span_w, span_h = xlim[1] - xlim[0], ylim[1] - ylim[0]
    k = min(1.0, 6.5 / span_w, 8.0 / span_h)
    plt, fig, ax = _fig(dpi, (span_w * k, span_h * k))
    fig.subplots_adjust(0, 0, 1, 1)
    for i in ids:
        x, y = xy[i]
        shape = byid[i].get("shape", "box")
        if shape == "diamond":
            ax.add_patch(Polygon([(x, y + box_h * 0.75), (x + box_w * 0.6, y), (x, y - box_h * 0.75),
                                  (x - box_w * 0.6, y)], closed=True, fc="white", ec="black", lw=2))
        elif shape == "ellipse":
            ax.add_patch(Ellipse((x, y), box_w * 1.1, box_h * 1.15, fc="white", ec="black", lw=2))
        else:
            style = "round,pad=0.02,rounding_size=0.18" if shape == "rounded" else "square,pad=0.02"
            ax.add_patch(FancyBboxPatch((x - box_w / 2, y - box_h / 2), box_w, box_h, boxstyle=style,
                                        fc="white", ec="black", lw=2))
        ax.text(x, y, labels[i], ha="center", va="center", fontsize=12 * k)

    def border(p, q):
        # where the line from p's centre towards q leaves p's box
        dx, dy = q[0] - p[0], q[1] - p[1]
        sx = (box_w / 2) / abs(dx) if dx else float("inf")
        sy = (box_h / 2) / abs(dy) if dy else float("inf")
        s = min(sx, sy) * 1.08
        return p[0] + dx * s, p[1] + dy * s
    for f, t, lab in edges:
        p, q = xy[f], xy[t]
        if mode == "cycle":
            bend = -0.25  # follow the circle (clockwise)
        elif mode == "hub":
            bend = 0.0
        else:
            bend = 0.25 if layer[t] <= layer[f] else 0.0  # back edges curve around
        a, b = border(p, q), border(q, p)
        ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=16, lw=1.8, color="black",
                                     connectionstyle=f"arc3,rad={bend}"))
        if lab:
            ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, lab, fontsize=10 * k, ha="center",
                    va="center", bbox=dict(fc="white", ec="none", pad=1))
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_aspect("equal")
    ax.axis("off")
    if spec.get("title"):
        ax.text((xlim[0] + xlim[1]) / 2, ylim[1] - 0.35, spec["title"], ha="center", va="center",
                fontsize=14 * k, fontweight="bold")
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor="white")
    plt.close(fig)
    return out.getvalue(), spec.get("title", "")


# ---------- charts ----------

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "chart": {"type": "string", "enum": ["bar", "line", "scatter", "pie"]},
        "title": {"type": "string"}, "x_label": {"type": "string"}, "y_label": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string"}},
        "series": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "x": {"type": "array", "items": {"type": "number"}},
            "y": {"type": "array", "items": {"type": "number"}}},
            "required": ["name", "x", "y"], "additionalProperties": False}},
        "fit": {"type": "string", "enum": ["none", "linear"]},
    },
    "required": ["chart", "title", "x_label", "y_label", "categories", "series", "fit"],
    "additionalProperties": False,
}

CHART_PROMPT = """Turn the data in this snippet of handwritten notes into a chart spec.
- Copy the numbers exactly as written; never invent data.
- Named categories (months, groups, items): chart "bar" (or "pie" for parts of a
  whole / percentages), category names in "categories", values in each series' y,
  x left empty.
- Numeric x and y (measurements, time series): "scatter" for measured points,
  "line" for things over time; numbers in x and y.
- One series per column/row of values; name it from the header.
- fit "linear" if they ask for a trend/best-fit line or the data looks like an
  experiment to fit, else "none".
Example: a table with columns "mass (kg)" 0.1 0.2 0.3 and "stretch (cm)" 2.1 3.9 6.2,
"plot this + best fit" -> chart "scatter", x_label "mass (kg)", y_label "stretch (cm)",
one series with x [0.1, 0.2, 0.3] and y [2.1, 3.9, 6.2], fit "linear", categories [].
Example: "Mon 72, Tue 85, Wed 90" -> chart "bar", categories ["Mon", "Tue", "Wed"],
one series with x [] and y [72, 85, 90], fit "none"."""


def repair_chart(spec):
    """The usual slips: a two-column table split into two y-only series, or
    numeric x drawn as bars."""
    series = [s for s in spec.get("series", []) if s.get("y")]
    cats = spec.get("categories") or []
    if (len(series) == 2 and not cats and not any(s.get("x") for s in series)
            and len(series[0]["y"]) == len(series[1]["y"])):
        # columns x | y written as two series
        xs, ys = series
        series = [{"name": ys["name"], "x": xs["y"], "y": ys["y"]}]
        spec["x_label"] = spec.get("x_label") or xs["name"]
        spec["y_label"] = spec.get("y_label") or ys["name"]
    series = [s for s in series if not (s.get("x") and list(s["x"]) == list(s["y"]))] or series
    if spec.get("chart") == "bar" and not cats and all(s.get("x") for s in series):
        spec["chart"] = "scatter" if spec.get("fit") == "linear" else "line"
    if spec.get("fit") == "linear" and spec.get("chart") == "bar":
        spec["chart"] = "scatter"
    spec["series"] = series
    return spec


def render_chart(spec, dpi):
    import numpy as np
    spec = repair_chart(spec)
    series = spec["series"]
    if not series:
        raise ValueError("no numbers found")
    plt, fig, ax = _fig(dpi)
    colors = ["black", "#1f4fd1", "#c62828", "#2e7d32"]
    kind, caption = spec.get("chart", "bar"), ""
    cats = spec.get("categories") or []
    if kind == "pie":
        ys = series[0]["y"]
        names = cats[:len(ys)] + [str(i + 1) for i in range(len(cats), len(ys))]
        ax.pie(ys, labels=names, autopct="%1.0f%%", colors=["white"] * len(ys),
               wedgeprops=dict(edgecolor="black", linewidth=1.8), textprops=dict(fontsize=12))
        ax.set_aspect("equal")
    elif kind == "bar" or (cats and not any(s.get("x") for s in series)):
        n = max(len(s["y"]) for s in series)
        names = cats[:n] + [str(i + 1) for i in range(len(cats), n)]
        width = 0.8 / len(series)
        hatches = ["", "//", "..", "xx"]
        for k, s in enumerate(series):
            xs = np.arange(len(s["y"])) + (k - (len(series) - 1) / 2) * width
            ax.bar(xs, s["y"], width, color="white", edgecolor=colors[k % 4], hatch=hatches[k % 4],
                   lw=1.8, label=s["name"] or None)
        ax.set_xticks(range(n), names, rotation=30 if max(map(len, names)) > 4 else 0, ha="right"
                      if max(map(len, names)) > 4 else "center")
    else:
        for k, s in enumerate(series):
            ys = np.asarray(s["y"], dtype=float)
            xs = np.asarray(s["x"] if len(s.get("x") or []) == len(ys) else range(1, len(ys) + 1), dtype=float)
            if kind == "line":
                ax.plot(xs, ys, color=colors[k % 4], lw=2.5, marker="o", label=s["name"] or None)
            else:
                ax.plot(xs, ys, "o", color=colors[k % 4], ms=7, label=s["name"] or None)
            if spec.get("fit") == "linear" and len(xs) >= 2:
                m, b = np.polyfit(xs, ys, 1)
                pred = m * xs + b
                ss = ((ys - ys.mean()) ** 2).sum()
                r2 = 1 - ((ys - pred) ** 2).sum() / ss if ss else 1.0
                line = np.linspace(xs.min(), xs.max(), 50)
                ax.plot(line, m * line + b, color=colors[k % 4], ls="--", lw=1.8)
                caption = f"y = {fmt(m)}x {'+' if b >= 0 else '−'} {fmt(abs(b))}, R² = {r2:.3f}"
        if caption:
            ax.text(0.02, 0.97, caption, transform=ax.transAxes, va="top", fontsize=12,
                    bbox=dict(fc="white", ec="black", pad=3))
    if kind != "pie":
        ax.set_xlabel(spec.get("x_label", ""))
        ax.set_ylabel(spec.get("y_label", ""))
        ax.grid(True, axis="y", color="#bbbbbb", lw=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        if len(series) > 1 and any(s["name"] for s in series):
            ax.legend(fontsize=11, frameon=True, edgecolor="black")
    if spec.get("title"):
        ax.set_title(spec["title"], fontsize=15)
    return _png(plt, fig), caption


# ---------- entry point ----------

SPECS = {
    "geometry": (GEOMETRY_PROMPT, GEOMETRY_SCHEMA, render_geometry),
    "flowchart": (FLOW_PROMPT, FLOW_SCHEMA, render_flowchart),
    "chart": (CHART_PROMPT, CHART_SCHEMA, render_chart),
}


def make(kind, chat, png, dpi, function_graph, written=""):
    """Draw `kind` from the snippet. Returns (png bytes, caption, spec).
    function_graph(png) handles "function" (transcribe, graph spec, render).
    `written` is the router's reading of the note: when it's only a request
    for a diagram of some topic, the flowchart is made from the words alone,
    since from the image the model tends to just copy the title into a box."""
    if kind == "function":
        return function_graph(png)
    prompt, schema, render = SPECS[kind]
    spec = None
    if kind == "flowchart" and written and DRAW_WORDS.search(written):
        raw = chat(TOPIC_FLOW_PROMPT.format(request=written), schema=schema, temperature=0.2, max_tokens=900)
        spec = clean_strings(json.loads(re.sub(r"^```(json)?|```$", "", raw.strip())))
        if len(spec.get("nodes", [])) < 3:
            spec = None
    if spec is None:
        if written:
            prompt += f'\nThe words and numbers in the note read: "{written}" (check against the image).'
        raw = chat(prompt, image_png=png, schema=schema, temperature=0.0, max_tokens=900)
        spec = clean_strings(json.loads(re.sub(r"^```(json)?|```$", "", raw.strip())))
    if kind == "geometry":
        spec = repair_geometry(spec, written)
    out, caption = render(spec, dpi)
    return out, caption, spec
