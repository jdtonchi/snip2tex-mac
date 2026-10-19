#!/usr/bin/env python3
"""snip2tex: receives lasso-selection PNGs from the reMarkable, turns them into
LaTeX with a local vision model, and sends back a rendered PNG, an evaluated
result or a graph. Dashboard at http://localhost:8799.

The model runs in-process with mlx-vlm (model.py), or via LM Studio. Rendering
uses tectonic + macOS `sips`. Run with `uv run snip2tex.py`, or the menu-bar
app (app.py / Snip2TeX.app).
"""
import base64, json, os, shutil, subprocess, sys, tempfile, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import deps
import diagrams
import model as backends
import power
import remote
import tablet
import tutor

STARTED = time.time()
CODE_DIR = Path(__file__).resolve().parent
HOME = Path.home() / ".snip2tex"
SNIPS = HOME / "snips"
CONFIG_PATH = HOME / "config.json"
SNIPS.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "port": 8799,
    "backend": "builtin",  # builtin (mlx-vlm in this process) | lmstudio
    "model_path": str(backends.DEFAULT_MODELS_DIR / "lmstudio-community" / "gemma-4-E2B-it-MLX-4bit"),
    "lmstudio_url": "http://localhost:1234/v1",
    "model": "",  # LM Studio model id; empty = first non-embedding model it reports
    "tablet_host": "10.11.99.1",  # the reMarkable over USB
    "remote_enabled": False,  # Wi-Fi mode: Tailscale Funnel -> remote_port (remote.py)
    "remote_port": 8798,      # tablet-only listener with a token; never the dashboard
    "remote_token": "",       # generated on first enable, pushed to the tablet over USB
    "remote_url": "",         # https://<mac>.<tailnet>.ts.net, filled in when enabled
    "auto_start_xovi": True,      # restart xovi over SSH after the tablet reboots
    # tablet button behaviour, pushed to the tablet (tablet.TABLET_CONFIG)
    "tablet_replace_snip": True,      # ∑ replaces the handwriting with the LaTeX
    "tablet_replace_graph": False,    # graph replaces the handwriting with the plot
    "tablet_selection_padding": 5,    # px grabbed around the selection
    "tablet_popup_seconds": 30,       # = popup hides itself after this (0: only on tap)
    "tablet_grab_delay_ms": 700,      # wait for the e-ink overlay to clear before grabbing
    "prompt": (
        "Transcribe the handwriting in this image into LaTeX exactly as written. Reproduc"
        "e the structure faithfully: a horizontal line with expressions above and below i"
        "t is a fraction, so use \\frac{top}{bottom}, even when the line is short or faint"
        "; keep superscripts, subscripts, parentheses, and signs exactly where they appea"
        "r. Do not simplify, solve, reorder, or fix anything. Output ONLY the LaTeX body "
        "(no \\documentclass, no ``` fences, no commentary). Use align* for multi-line mat"
        "h, \\[ ... \\] for a single equation, and plain text for prose."
    ),
    "ask_prompt": (
        "This is a snippet from a student's handwritten notes on an e-ink tablet: writing, "
        "a drawing, or both. They selected it and tapped \"ask\". Reply to it. A question: "
        "answer it. A drawing: say what it shows, or answer what they ask about it. A greeting: "
        "reply in kind. A formula or statement: say the single most useful thing about it.\n"
        "Be concise: at most 2 short sentences, under 40 words, answer first, no preamble. "
        "Plain text only, no LaTeX or markdown; write math with Unicode (x², √, π, ≤, →)."
    ),
    "temperature": 0.0,
    "render_dpi": 226,  # RMPP screen density
    "append_tex_file": str(Path.home() / "Documents" / "lecture-notes.tex"),
    "copy_to_clipboard": True,
}

lock = threading.Lock()
recent = []          # newest first, metadata only
clients = {}         # ip -> last seen ts


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


config = load_config()


def save_config():
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


# ---------- model ----------

builtin = backends.BuiltinModel()
show_hook = None  # set by app.py: brings the app window to the front


def llm():
    if config["backend"] == "lmstudio":
        return backends.LMStudioModel(config["lmstudio_url"], config["model"])
    return builtin


def load_model():
    """(Re)load the in-process model in the background, then warm it up."""
    if config["backend"] != "builtin":
        return

    def run():
        default = DEFAULTS["model_path"]
        if config["model_path"] == default and not deps.model_present(default):
            builtin.state = "downloading"
            try:
                deps.ensure_model(default)
            except Exception as e:
                builtin.state, builtin.error = "error", f"model download failed: {e}"
                return
        builtin.load(config["model_path"])
        if builtin.state == "ready":
            builtin.warm_up()
            warm_up_tools()
    threading.Thread(target=run, daemon=True).start()


def warm_up_tools():
    """Pay the one-time costs (tectonic, SymPy import, matplotlib font cache,
    the graph JSON grammar) at startup instead of on the first button press."""
    try:
        render_png(r"\[ x^2 \]")
        evaluate_latex(r"\frac{1}{2}")
        render_graph(repair_spec(graph_spec("y = x^2"), "y = x^2"))
    except Exception as e:
        print("warm-up:", e, flush=True)


def tool(name):
    """Absolute path to a command: apps launched from Finder don't get Homebrew's PATH."""
    if name == "tectonic":
        return deps.ensure_tectonic()
    found = shutil.which(name)
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/sbin", "/usr/sbin"):
        if os.path.exists(f"{d}/{name}"):
            return f"{d}/{name}"
    raise RuntimeError(f"{name} not found (install it, e.g. brew install {name})")


def strip_fences(s):
    s = backends.strip_thinking(s)
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return s.strip()


def prep_image(png):
    """Flatten transparency onto white and upscale tiny crops so the model can read them."""
    try:
        from PIL import Image
    except ImportError:
        return png
    import io
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    bg = Image.new("RGBA", im.size, "white")
    im = Image.alpha_composite(bg, im).convert("RGB")
    if min(im.size) < 256:
        k = 256 / min(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def framebuffer_to_png(raw, params):
    from PIL import Image
    import io
    stride, rows = int(params["stride"]), int(params["rows"])
    x, w = int(params.get("x", 0)), int(params.get("w", stride // 4))
    im = Image.frombuffer("RGBA", (stride // 4, rows), raw, "raw", "BGRA", 0, 1)
    im = im.crop((max(0, x), 0, min(stride // 4, x + w), rows))
    # the framebuffer is physical portrait; in landscape the writing is
    # sideways in it, so turn it upright (PIL rotates counter-clockwise)
    rot = int(float(params.get("rot", 0))) % 360
    if rot:
        im = im.rotate(rot, expand=True)
    out = io.BytesIO()
    clean_screen_grab(im, sel_pad=int(params.get("pad", 0))).save(out, "PNG")
    return out.getvalue()


def clean_screen_grab(im, sel_pad=0, margin=32):
    """Drop the selection overlay if it was still on screen. The grab is the
    selection plus sel_pad px on each side, so the overlay sits sel_pad px in:
    the gray tint goes to white (threshold), then the 2px border, the corner
    resize handles (30px) and the rotation handle (40px circle on the top edge)
    are painted out. A white margin is added so a fraction bar that spans the
    selection doesn't look like a frame."""
    from PIL import ImageDraw, ImageOps
    im = im.convert("L").point(lambda v: 0 if v < 110 else 255)
    d, W, H, p = ImageDraw.Draw(im), im.width, im.height, sel_pad
    d.rectangle((p - 3, p - 3, W - 1 - p + 3, H - 1 - p + 3), outline=255, width=7)
    for cx, cy in ((p, p), (W - 1 - p, p), (p, H - 1 - p), (W - 1 - p, H - 1 - p)):
        d.rectangle((cx - 17, cy - 17, cx + 17, cy + 17), fill=255)
    d.ellipse((W // 2 - 24, p - 24, W // 2 + 24, p + 24), fill=255)
    return ImageOps.expand(im, border=margin, fill=255).convert("RGB")


def image_to_latex(png):
    return strip_fences(llm().chat(config["prompt"], image_png=prep_image(png),
                                   temperature=config["temperature"], max_tokens=600))


# ---------- rendering ----------

# sips rasterizes PDFs at 72dpi, so scale the type up by dpi/72 instead.
DOC = r"""\documentclass[border=%(border)dpt,varwidth=%(width)dpt]{standalone}
\usepackage{amsmath,amssymb,anyfontsize}
\begin{document}
\fontsize{%(size).1f}{%(lead).1f}\selectfont
%(body)s
\end{document}
"""


def render_png(latex):
    try:
        return _render(latex)
    except RuntimeError:
        # models often return bare math with no $...$; retry in display math
        if not any(t in latex for t in ("$", "\\[", "\\begin")):
            return _render("\\[\n" + latex + "\n\\]")
        raise


def _render(latex):
    with tempfile.TemporaryDirectory() as d:
        tex = Path(d) / "s.tex"
        k = config["render_dpi"] / 72
        tex.write_text(DOC % {"border": 4 * k, "width": 400 * k, "size": 11 * k,
                              "lead": 13.6 * k, "body": latex})
        r = subprocess.run([tool("tectonic"), "--chatter", "minimal", str(tex)],
                           cwd=d, capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError("LaTeX failed:\n" + (r.stderr or r.stdout)[-800:])
        out = Path(d) / "s.png"
        subprocess.run([tool("sips"), "-s", "format", "png", "-s", "dpiWidth", str(config["render_dpi"]),
                        "-s", "dpiHeight", str(config["render_dpi"]),
                        str(Path(d) / "s.pdf"), "--out", str(out)],
                       capture_output=True, check=True)
        return out.read_bytes()


# ---------- evaluation ----------

SUPERSCRIPT = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")


def fmt_number(v):
    """1.848e-11 -> '1.848 × 10⁻¹¹'; plain notation for ordinary sizes."""
    v = float(v)
    if v == 0 or 1e-4 <= abs(v) < 1e6:
        return f"{v:.6g}"
    mant, exp = f"{v:.4e}".split("e")
    return f"{mant.rstrip('0').rstrip('.')} × 10{str(int(exp)).translate(SUPERSCRIPT)}"


def fmt_expr(e):
    return str(e).replace("**", "^").replace("*", "·")


def strip_math_delims(latex):
    import re
    src = re.sub(r"\\[,;!:]|\\quad|\\qquad", " ", latex).strip()
    for a, b in (("\\[", "\\]"), ("$$", "$$"), ("$", "$")):
        if src.startswith(a) and src.endswith(b):
            src = src[len(a):-len(b)].strip()
    return src


def looks_like_words(latex):
    """'Hello', '\\text{why is the sky blue?}': prose, not something to compute."""
    import re
    src = strip_math_delims(latex)
    if "\\text" in src:
        return True
    src = re.sub(r"\\[A-Za-z]+", " ", src)  # \times, \frac, ... aren't words
    letters_only = re.sub(r"[\s.,!?'\"]", "", src)
    return bool(re.fullmatch(r"[A-Za-z]{3,}", letters_only)) or len(re.findall(r"[A-Za-z]{4,}", src)) >= 2


def evaluate_latex(latex):
    """Evaluate recognized LaTeX with SymPy and return one line of text."""
    import sympy
    from sympy.parsing.latex import parse_latex
    src = strip_math_delims(latex)
    src = src.replace("\\begin{align*}", "").replace("\\end{align*}", "").replace("&", "")
    try:
        expr = parse_latex(src, backend="lark")
    except Exception:
        return "can't evaluate this"
    if not isinstance(expr, sympy.Basic):
        return "ambiguous, can't evaluate"
    if isinstance(expr, sympy.Equality):
        free = sorted(expr.free_symbols, key=str)
        if not free:
            return "true" if sympy.simplify(expr.lhs - expr.rhs) == 0 else "false"
        sols = sympy.solve(expr, free[0])
        if not sols:
            return f"no solution for {free[0]}"
        shown = [fmt_number(x) if x.is_number and x.is_real and not x.is_Rational else fmt_expr(x) for x in sols]
        return f"{free[0]} = " + ", ".join(shown)
    val = expr.doit()
    if val.free_symbols:
        return "= " + fmt_expr(sympy.simplify(val))
    exact = sympy.nsimplify(val) if val.is_Float else sympy.simplify(val)
    approx = val.evalf()
    if exact.is_Integer or (exact == approx and not exact.is_Rational):
        return "= " + fmt_number(approx) if not exact.is_Integer else f"= {exact}"
    if exact.is_Rational and len(str(exact)) <= 12:
        return f"= {exact} ≈ {fmt_number(approx)}"
    return "= " + fmt_number(approx)


SYMPY_PROMPT = """Translate this handwritten math (transcribed to LaTeX) into ONE SymPy
expression that computes what it asks for.

{latex}

Use SymPy syntax. Symbols are plain letters (x, y, t, n, ...). Available:
sin cos tan asin acos atan exp log sqrt Abs pi E I oo,
diff(f, x), integrate(f, x), integrate(f, (x, a, b)), limit(f, x, a),
Sum(f, (n, a, b)), Product(...), solve(Eq(lhs, rhs), x), solve([eq1, eq2], [x, y]),
simplify, factor, expand, apart, series(f, x, a, n), Matrix([[1, 2], [3, 4]]).det(),
Matrix(...).inv(), gcd, lcm, factorint, isprime, binomial, factorial, N(expr).
Units: convert_to(5*kilometer, mile); names like meter, kilometer, second, hour,
kilogram, gram, newton, joule, electronvolt, watt, mile, inch, foot, celsius is not available.
Constants: planck, hbar, speed_of_light, gravitational_constant, boltzmann,
elementary_charge, electron_rest_mass, avogadro_number.
Evaluate exactly what is written; do not simplify the problem yourself.
If the text names an operation (factor, expand, simplify, derivative, solve, ...),
wrap the expression in it. Examples:
  "factor x^2 - 9"            -> factor(x**2 - 9)
  "\\int_0^\\pi \\sin x dx"     -> integrate(sin(x), (x, 0, pi))
  "\\frac{d}{dx} x^3"           -> diff(x**3, x)
  "3 hours in seconds"        -> convert_to(3*hour, second)
If it is not math that can be computed (words, a greeting, a conceptual
question), set is_math to false and sympy to ""."""

SYMPY_SCHEMA = {
    "type": "object",
    "properties": {"is_math": {"type": "boolean"}, "sympy": {"type": "string"}},
    "required": ["is_math", "sympy"],
    "additionalProperties": False,
}

_sympy_ns = None


def sympy_namespace():
    """SymPy plus long-named units/constants (short ones like m, s, g would
    shadow ordinary variables), with no Python builtins."""
    global _sympy_ns
    if _sympy_ns is None:
        ns = {}
        exec("from sympy import *", ns)
        import sympy.physics.units as u
        for name in dir(u):
            if len(name) >= 4 and not name.startswith("_"):
                ns.setdefault(name, getattr(u, name))
        ns["convert_to"] = u.convert_to
        ns["__builtins__"] = {}
        _sympy_ns = ns
    return _sympy_ns


def repair_sympy(code):
    """integrate(f, (a, b)) -> integrate(f, (x, a, b)): the model drops the variable."""
    import re
    m = re.match(r"^\s*(integrate|Sum|summation|Product)\((.+),\s*\(([^(),]+),\s*([^(),]+)\)\)\s*$", code)
    if not m:
        return None
    var = "n" if m.group(1) in ("Sum", "summation", "Product") and "n" in m.group(2) else "x"
    return f"{m.group(1)}({m.group(2)}, ({var}, {m.group(3)}, {m.group(4)}))"


def run_sympy(code, timeout=8):
    """Evaluate a model-written SymPy expression in the restricted namespace."""
    import concurrent.futures, re
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, convert_xor
    if re.search(r"__|\bimport\b|\blambda\b|\bopen\b|\beval\b|\bexec\b", code):
        raise ValueError("refusing that expression")
    ns = sympy_namespace()

    def go():
        val = parse_expr(code, global_dict=ns, transformations=standard_transformations + (convert_xor,))
        return val.doit() if hasattr(val, "doit") else val

    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(go).result(timeout=timeout)


def fmt_value(v):
    """One line of text for whatever SymPy returned."""
    import sympy
    if isinstance(v, bool) or v in (sympy.true, sympy.false):
        return "true" if bool(v) else "false"
    if isinstance(v, dict):
        return ", ".join(f"{k} = {fmt_value(x)}" for k, x in v.items())
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(x, dict) for x in v):
            return "; ".join(fmt_value(x) for x in v)
        return ", ".join(fmt_value(x) for x in v) if v else "no solution"
    if isinstance(v, sympy.Basic) and v.is_number:
        if v.is_Integer:
            return str(v)
        if v.is_Rational and len(str(v)) <= 12:
            return f"{v} ≈ {fmt_number(v.evalf())}"
        try:
            approx = fmt_number(v.evalf())
        except TypeError:  # complex
            return fmt_expr(sympy.N(v, 6))
        exact = fmt_expr(v)
        # keep short exact forms like pi^2/6 or sqrt(2)
        if not v.has(sympy.Float) and len(exact) <= 16:
            return f"{exact} ≈ {approx}"
        return approx
    if isinstance(v, sympy.Basic) and not v.free_symbols:
        # quantities with units (5 km -> 3.10686 mile)
        return fmt_expr(sympy.N(v, 6))
    return fmt_expr(v)


def compute(latex):
    """(text, is_math) for the = and ? buttons.

    Plain arithmetic and equations go straight to SymPy's LaTeX parser (exact,
    no model). Everything else (derivatives, limits, matrices, units, ...) is
    translated to SymPy by Gemma and computed by SymPy. Prose is not math."""
    import sympy
    from sympy.parsing.latex import parse_latex
    if not looks_like_words(latex):
        try:
            expr = parse_latex(strip_math_delims(latex), backend="lark")
            if isinstance(expr, sympy.Equality):
                return evaluate_latex(latex), True
            if isinstance(expr, sympy.Basic) and not expr.doit().free_symbols:
                return "= " + fmt_value(expr.doit()), True
        except Exception:
            pass
    plan = json.loads(strip_fences(llm().chat(SYMPY_PROMPT.replace("{latex}", latex), schema=SYMPY_SCHEMA,
                                              temperature=0.0, max_tokens=200)))
    if not plan.get("is_math") or not plan.get("sympy", "").strip():
        return "", False
    try:
        try:
            val = run_sympy(plan["sympy"])
        except Exception:
            fixed = repair_sympy(plan["sympy"])
            if not fixed:
                raise
            val = run_sympy(fixed)
    except Exception as e:
        print("sympy failed:", plan["sympy"], "->", e, flush=True)
        # the plain LaTeX parser may still manage it
        direct = evaluate_latex(latex)
        return direct, True
    # an expression with nothing to do (e.g. (x+1)^2): show it expanded
    import re
    bare = not re.match(r"\s*[A-Za-z_]\w*\(", plan["sympy"])
    if bare and isinstance(val, sympy.Basic) and val.free_symbols and not isinstance(val, sympy.Equality):
        expanded = sympy.expand(val)
        if expanded != val:
            val = expanded
    prefix = "" if isinstance(val, (list, tuple, dict)) or isinstance(val, sympy.Equality) else "= "
    return prefix + fmt_value(val), True


# ---------- graphing ----------

GRAPH_PROMPT = """You plan a simple graph of handwritten math for a student.
The math, transcribed to LaTeX:

{latex}

Return the graph spec. Rules:
- Expressions use Python/SymPy syntax: ** for powers, * for multiplication,
  sin cos tan exp log sqrt abs pi E. Use only the variable x in curves.
- y = f(x), or a bare expression f(x): put f(x) in "curves".
- An equation in x and y that is not y = f(x) (circle, ellipse, x = y**2, ...):
  put it in "implicit" as "lhs = rhs".
- Several functions or equations: graph all of them.
- Coordinate pairs: put them in "points".
- x = c (vertical line): put c in "vlines".
- Unknown constants (a, k, ...): substitute a simple value like 1 and say so in "title".
- Pick xmin/xmax/ymin/ymax so the interesting features show: intercepts, extrema,
  intersections, asymptotes, at least one full period for trig. Use null for
  ymin/ymax to let them be chosen from the data.
- label: a short human-readable label for each curve, like "y = x^2"."""

GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "curves": {"type": "array", "items": {"type": "object", "properties": {
            "expr": {"type": "string"}, "label": {"type": "string"}}, "required": ["expr", "label"],
            "additionalProperties": False}},
        "implicit": {"type": "array", "items": {"type": "object", "properties": {
            "expr": {"type": "string"}, "label": {"type": "string"}}, "required": ["expr", "label"],
            "additionalProperties": False}},
        "points": {"type": "array", "items": {"type": "object", "properties": {
            "x": {"type": "number"}, "y": {"type": "number"}, "label": {"type": "string"}},
            "required": ["x", "y", "label"], "additionalProperties": False}},
        "vlines": {"type": "array", "items": {"type": "number"}},
        "xmin": {"type": "number"}, "xmax": {"type": "number"},
        "ymin": {"type": ["number", "null"]}, "ymax": {"type": ["number", "null"]},
    },
    "required": ["title", "curves", "implicit", "points", "vlines", "xmin", "xmax", "ymin", "ymax"],
    "additionalProperties": False,
}


def graph_spec(latex):
    return json.loads(strip_fences(llm().chat(GRAPH_PROMPT.format(latex=latex), schema=GRAPH_SCHEMA,
                                              temperature=0.0, max_tokens=800)))


def parse_math(text, allowed):
    """Parse a SymPy-syntax string; only the given symbols may appear free."""
    import sympy
    from sympy.parsing.sympy_parser import (parse_expr, standard_transformations,
                                            implicit_multiplication_application, convert_xor)
    syms = {n: sympy.Symbol(n) for n in allowed}
    expr = parse_expr(text.replace("−", "-"), local_dict=dict(syms, e=sympy.E, pi=sympy.pi),
                      transformations=standard_transformations + (implicit_multiplication_application, convert_xor))
    extra = {str(f) for f in expr.free_symbols} - set(allowed)
    if extra:
        raise ValueError(f"unknown symbol(s) {', '.join(sorted(extra))} in {text}")
    return expr, syms


def repair_spec(spec, latex):
    """Small models get the spec roughly right; fix the usual slips."""
    import re
    for c in spec.get("curves", []) + spec.get("implicit", []):
        c["expr"] = c["expr"].strip()
    for c in spec.get("curves", []):
        c["expr"] = re.sub(r"^(y|f\(x\))\s*=\s*", "", c["expr"])
    flat = re.sub(r"\s", "", latex)
    explicit = bool(re.search(r"(^|[^a-zA-Z\\])y=", flat)) or "y" not in flat
    if spec.get("implicit") and not explicit:
        # an equation in x and y, drawn implicitly: drop the y = ±sqrt(...) halves
        spec["curves"] = []
    # "\frac" in JSON arrives as a form feed + "rac"; drop LaTeX-ish titles
    if re.search(r"[\\\x00-\x1f]", spec.get("title", "")):
        spec["title"] = ""
    return spec


def render_graph(spec):
    import numpy as np
    import sympy
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import io

    xmin, xmax = float(spec["xmin"]), float(spec["xmax"])
    if not xmax > xmin:
        xmin, xmax = -10.0, 10.0
    xs = np.linspace(xmin, xmax, 4000)
    # ink colours of the Paper Pro, distinct line styles for when they print grey
    styles = [("black", "-"), ("#1f4fd1", "--"), ("#c62828", "-."), ("#2e7d32", ":")]

    plt.rcParams.update({"font.size": 15, "axes.linewidth": 1.5, "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(5.2, 3.8), dpi=config["render_dpi"])
    finite_ys, n = [], 0

    for c in spec.get("curves", []):
        expr, syms = parse_math(c["expr"], ["x"])
        f = sympy.lambdify(syms["x"], expr, "numpy")
        with np.errstate(all="ignore"):
            ys = np.asarray(f(xs), dtype=complex) * np.ones_like(xs)
        ys = np.where(np.abs(ys.imag) < 1e-9, ys.real, np.nan)
        ys[~np.isfinite(ys)] = np.nan
        # break the line across asymptotes instead of drawing a vertical spike
        jump = np.abs(np.diff(ys)) > 50 * (np.nanstd(ys) or 1)
        ys[1:][jump] = np.nan
        color, ls = styles[n % len(styles)]
        ax.plot(xs, ys, color=color, ls=ls, lw=2.8, label=c.get("label") or c["expr"])
        finite_ys.append(ys[np.isfinite(ys)])
        n += 1

    ymin, ymax = spec.get("ymin"), spec.get("ymax")
    if ymin is None or ymax is None or not ymax > ymin:
        allys = np.concatenate(finite_ys) if finite_ys else np.array([])
        if allys.size:
            lo, hi = np.percentile(allys, [3, 97])
            lo, hi = min(lo, 0.0), max(hi, 0.0)
            pad = (hi - lo) * 0.12 or 1.0
            ymin, ymax = lo - pad, hi + pad
        else:
            ymin, ymax = xmin, xmax
    else:
        # model-picked limits often sit exactly on a peak; leave a margin
        pad = (ymax - ymin) * 0.05
        ymin, ymax = ymin - pad, ymax + pad
    ymin, ymax = float(ymin), float(ymax)

    for c in spec.get("implicit", []):
        text = c["expr"]
        lhs, _, rhs = text.partition("=")
        expr, syms = parse_math(f"({lhs}) - ({rhs or 0})", ["x", "y"])
        f = sympy.lambdify((syms["x"], syms["y"]), expr, "numpy")
        gx, gy = np.meshgrid(np.linspace(xmin, xmax, 700), np.linspace(ymin, ymax, 700))
        with np.errstate(all="ignore"):
            gz = np.asarray(f(gx, gy), dtype=float) * np.ones_like(gx)
        color, ls = styles[n % len(styles)]
        ax.contour(gx, gy, gz, levels=[0], colors=[color], linestyles=[ls], linewidths=2.8)
        ax.plot([], [], color=color, ls=ls, lw=2.8, label=c.get("label") or text)
        n += 1

    for v in spec.get("vlines", []):
        ax.axvline(float(v), color="black", ls="--", lw=1.8)
    for pnt in spec.get("points", []):
        ax.plot(float(pnt["x"]), float(pnt["y"]), "o", color="black", ms=8)
        if pnt.get("label"):
            ax.annotate(pnt["label"], (float(pnt["x"]), float(pnt["y"])), textcoords="offset points",
                        xytext=(6, 6), fontsize=13)

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    if spec.get("implicit") and not spec.get("curves"):
        ax.set_aspect("equal", adjustable="box")
    ax.axhline(0, color="black", lw=1.2)
    ax.axvline(0, color="black", lw=1.2)
    ax.grid(True, color="#bbbbbb", lw=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if spec.get("title"):
        ax.set_title(spec["title"], fontsize=15)
    if n > 1 or spec.get("points"):
        ax.legend(fontsize=12, frameon=True, framealpha=1, edgecolor="black")
    fig.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor="white")
    plt.close(fig)
    return out.getvalue()


# ---------- pipeline ----------

def ask_image(png):
    """? button: the snippet itself goes to Gemma (no transcription first), so
    drawings work too. Short plain-text answer for the tablet popup."""
    answer = strip_fences(llm().chat(config["ask_prompt"], image_png=prep_image(png),
                                     temperature=0.2, max_tokens=160))
    return " ".join(answer.split()) or "no answer"


def function_graph(png):
    """The graph button's pipeline, for the ? button's "function" diagrams."""
    latex = image_to_latex(png)
    spec = repair_spec(graph_spec(latex), latex)
    return render_graph(spec), spec.get("title") or latex, spec


def ask_smart(png):
    """? button: Gemma decides between a text answer and a diagram (function
    graph, geometry, flowchart/concept map, chart). Returns (text, png or None,
    kind). Drawing failures fall back to a text answer."""
    img = prep_image(png)
    chat = llm().chat
    kind, written = diagrams.route(chat, img, allow_answer=True)
    # a diagram that fails falls back to a text answer
    for k in ([kind] if kind != "answer" else []):
        try:
            out, caption, spec = diagrams.make(k, chat, img, config["render_dpi"], function_graph, written)
            label = {"function": "graph", "geometry": "figure", "flowchart": "diagram", "chart": "chart"}[k]
            return (caption if caption else f"added a {label}"), out, k
        except Exception as e:
            print(f"ask: {k} failed:", e, flush=True)
    return ask_image(png), None, "answer"


def ask_about(latex):
    """Second, text-only pass: a short plain-text answer for the tablet popup."""
    prompt = config["ask_prompt"].replace("{latex}", latex)
    # models are bad at arithmetic: hand over SymPy's result for real math
    try:
        computed, is_math = compute(latex)
    except Exception:
        computed, is_math = "", False
    if not is_math:
        # prose or small talk: the student's words are the message itself;
        # quoting them inside instructions makes the small model answer the instructions
        text = strip_math_delims(latex)
        text = __import__("re").sub(r"\\text\{([^}]*)\}", r"\1", text).strip()
        prompt = (f"{text}\n\n(You're a friendly study helper. Reply in at most 2 short "
                  "sentences, under 40 words, plain text, no markdown.)")
    elif computed and not computed.startswith(("can't", "ambiguous")):
        prompt += (f"\n\nA computer algebra system already evaluated it exactly: {computed}\n"
                   "Use that result; do not recompute it.")
    answer = strip_fences(llm().chat(prompt, temperature=0.2, max_tokens=160))
    return " ".join(answer.split()) or "no answer"


def process(png, source_ip, evaluate=False, graph=False, ask=False, action=None):
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    (SNIPS / f"{sid}-in.png").write_bytes(png)
    entry = {"id": sid, "time": time.time(), "from": source_ip, "status": "working"}
    with lock:
        recent.insert(0, entry)
        del recent[50:]
    t0 = time.time()
    try:
        if action == "solve":
            text, out = tutor.solve(llm().chat, prep_image(png), render_png)
            entry["result"], entry["kind"] = text, "solve"
            (SNIPS / f"{sid}-out.png").write_bytes(out)
            entry["rendered"] = True
            entry["status"] = "ok"
            return entry, out
        if action == "check":
            entry["result"], entry["kind"] = tutor.check(llm().chat, prep_image(png)), "check"
            entry["status"] = "ok"
            return entry, None
        if ask:
            text, out, kind = ask_smart(png)
            entry["result"], entry["kind"] = text, kind
            if out:
                (SNIPS / f"{sid}-out.png").write_bytes(out)
                entry["rendered"] = True
            entry["status"] = "ok"
            return entry, out
        latex = image_to_latex(png)
        entry["latex"] = latex
        if evaluate:
            text, is_math = compute(latex)
            entry["result"] = text if is_math else "not math (try the ? button)"
            entry["status"] = "ok"
            return entry, None
        if graph:
            spec = repair_spec(graph_spec(latex), latex)
            entry["graph"] = spec
            out = render_graph(spec)
            (SNIPS / f"{sid}-out.png").write_bytes(out)
            entry["rendered"] = True
            entry["status"] = "ok"
            return entry, out
        try:
            out = render_png(latex)
            (SNIPS / f"{sid}-out.png").write_bytes(out)
            entry["rendered"] = True
        except Exception as e:
            out = None
            entry["render_error"] = str(e)
        if config["copy_to_clipboard"]:
            subprocess.run([tool("pbcopy")], input=latex.encode())
        if config["append_tex_file"]:
            p = Path(config["append_tex_file"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(f"\n% snip {sid}\n{latex}\n")
        entry["status"] = "ok"
        return entry, out
    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)
        return entry, None
    finally:
        entry["seconds"] = round(time.time() - t0, 1)


# ---------- connection info ----------

def local_ips():
    out = subprocess.run([tool("ifconfig")], capture_output=True, text=True).stdout
    ips, iface = [], None
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":")[0]
        elif "inet " in line:
            ip = line.split()[1]
            if not ip.startswith("127."):
                kind = ("USB (reMarkable)" if ip.startswith("10.11.99.") else
                        "Tailscale" if ip.startswith("100.") else iface)
                ips.append({"ip": ip, "kind": kind})
    return ips


def mac_usb_ip():
    return next((a["ip"] for a in local_ips() if a["ip"].startswith("10.11.99.")), None)


watcher = tablet.TabletWatcher(config, mac_usb_ip)


def status():
    now = time.time()
    return {
        "model": llm().status(),
        "tablet": watcher.status(),
        "power": power.status(),
        "progress": dict(deps.progress),
        "local_models": backends.find_models(),
        "addresses": local_ips(),
        "tablet_seen": {ip: round(now - t) for ip, t in clients.items()},
        "port": config["port"],
        "config": config,
        "recent": recent,
    }


# ---------- http ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        for k, v in getattr(self, "extra_headers", {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        chunks = []
        while n > 0:
            c = self.rfile.read1(min(n, 1 << 20))
            if not c:
                break
            chunks.append(c)
            n -= len(c)
        return b"".join(chunks)

    def read_upload(self, qs):
        """Body is a PNG, or with ?raw=bgra a strip of framebuffer rows
        (stride/rows/x/w say how to crop it), optionally ?enc=gz64."""
        body = self.read_body()
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if params.get("enc") == "gz64":
            import gzip
            body = gzip.decompress(base64.b64decode(body))
        if params.get("raw") == "bgra":
            body = framebuffer_to_png(body, params)
        return body

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ping":
            clients[self.client_address[0]] = time.time()
            return self.send(200, {"ok": True})
        if not self.is_local():
            return self.send(403, {"error": "dashboard is localhost-only"})
        if path == "/":
            return self.send(200, DASHBOARD, "text/html; charset=utf-8")
        if path == "/api/stale":
            # "yes" when the code changed since this process started; the
            # launcher then restarts the app instead of just showing it
            files = list(CODE_DIR.glob("*.py")) + [HOME / "Snip2TeX Engine.app" / "Contents" / "MacOS" / "Snip2TeX"]
            newest = max(f.stat().st_mtime for f in files if f.exists())
            return self.send(200, "yes" if newest > STARTED else "no", "text/plain")
        if path == "/api/status":
            return self.send(200, status())
        if path == "/api/remote":
            return self.send(200, remote_status())
        if path.startswith("/snips/"):
            f = SNIPS / Path(path).name
            if f.exists():
                return self.send(200, f.read_bytes(), "image/png")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        if path == "/graph":
            # Same upload as /snip; replies with a graph PNG, or one line of
            # text if it failed (the tablet shows that in a popup)
            clients[self.client_address[0]] = time.time()
            entry, out = process(self.read_upload(qs), self.client_address[0], graph=True)
            if out:
                return self.send(200, out, "image/png")
            return self.send(200, "can't graph: " + entry.get("error", "?"), "text/plain; charset=utf-8")
        if path == "/ask":
            # Same upload as /snip; transcribe, ask Gemma about it, reply with
            # its short answer as plain text for the popup
            clients[self.client_address[0]] = time.time()
            entry, out = process(self.read_upload(qs), self.client_address[0], ask=True)
            text = entry.get("result") or ("error: " + entry.get("error", "?"))
            if out:
                # a diagram: the PNG to insert, the popup text in a header
                self.extra_headers = {"X-Caption": base64.b64encode(text.encode()).decode()}
                return self.send(200, out, "image/png")
            return self.send(200, text, "text/plain; charset=utf-8")
        if path in ("/solve", "/check"):
            # ✦ menu. Same upload as /snip. solve: PNG of the worked steps with
            # the answer in X-Caption (like ?'s diagrams); check: one line of text
            clients[self.client_address[0]] = time.time()
            entry, out = process(self.read_upload(qs), self.client_address[0], action=path[1:])
            text = entry.get("result") or ("error: " + entry.get("error", "?"))
            if out:
                self.extra_headers = {"X-Caption": base64.b64encode(text.encode()).decode()}
                return self.send(200, out, "image/png")
            return self.send(200, text, "text/plain; charset=utf-8")
        if path == "/eval":
            # Same upload as /snip; replies with one line of plain text to show
            clients[self.client_address[0]] = time.time()
            entry, _ = process(self.read_upload(qs), self.client_address[0], evaluate=True)
            text = entry.get("result") or ("error: " + entry.get("error", "?"))
            return self.send(200, text, "text/plain; charset=utf-8")
        if path == "/snip":
            # ?format=png -> rendered PNG back (what the tablet inserts);
            # default -> JSON with latex + png.
            clients[self.client_address[0]] = time.time()
            entry, out = process(self.read_upload(qs), self.client_address[0])
            if "format=png" in qs:
                if out:
                    return self.send(200, out, "image/png")
                return self.send(500, {"error": entry.get("error") or entry.get("render_error")})
            res = dict(entry)
            if out:
                res["png_b64"] = base64.b64encode(out).decode()
            return self.send(200 if entry["status"] == "ok" else 500, res)
        if not self.is_local():
            return self.send(403, {"error": "localhost-only"})
        if path == "/api/config":
            new = json.loads(self.read_body())
            before = (config["backend"], config["model_path"])
            for k, v in new.items():
                if k in DEFAULTS:
                    config[k] = type(DEFAULTS[k])(v)
            save_config()
            if (config["backend"], config["model_path"]) != before:
                load_model()
            return self.send(200, config)
        if path == "/api/show":
            if show_hook:
                show_hook()
            else:
                webbrowser.open(f"http://localhost:{config['port']}")
            return self.send(200, {"ok": True})
        if path == "/api/lid":
            # {"awake": true} keeps the Mac awake with the lid closed
            want = bool(json.loads(self.read_body() or b"{}").get("awake"))
            ok = power.set_sleep_disabled(want)
            return self.send(200 if ok else 500, power.status())
        if path == "/api/remote":
            # {"on": true} turns Wi-Fi mode (Tailscale Funnel) on
            on = bool(json.loads(self.read_body() or b"{}").get("on"))
            return self.send(200, set_remote(on))
        if path == "/api/quit":
            power.restore()
            self.send(200, {"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        if path == "/api/chat":
            # the loaded model for local scripts and tests, so they never load a
            # second copy (two 26B copies don't fit in 24 GB): model.EngineModel
            req = json.loads(self.read_body())
            img = base64.b64decode(req["image_b64"]) if req.get("image_b64") else None
            reply = llm().chat(req["prompt"], image_png=img, schema=req.get("schema"),
                               temperature=req.get("temperature", 0.0), max_tokens=req.get("max_tokens", 800))
            return self.send(200, {"reply": reply})
        if path == "/api/test":  # upload an image from the dashboard
            entry, _ = process(self.read_body(), "dashboard")
            return self.send(200, entry)
        self.send(404, {"error": "not found"})


TABLET_PATHS = ("/snip", "/eval", "/graph", "/ask", "/solve", "/check")


class RemoteH(H):
    """Behind Tailscale Funnel (remote.py): only the tablet's button endpoints,
    only with the token. Funnel requests come from localhost, so is_local must
    never be true here."""

    def is_local(self):
        return False

    def token_ok(self):
        import hmac, urllib.parse
        token = urllib.parse.parse_qs(self.path.partition("?")[2]).get("token", [""])[0]
        want = config.get("remote_token") or ""
        return bool(want) and config.get("remote_enabled") and hmac.compare_digest(token, want)

    def do_GET(self):
        if self.path.split("?")[0] == "/ping" and self.token_ok():
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] not in TABLET_PATHS or not self.token_ok():
            return self.send(404, {"error": "not found"})
        super().do_POST()


def set_remote(on):
    """Dashboard toggle for Wi-Fi mode. Returns the status dict."""
    if on and not config["remote_token"]:
        config["remote_token"] = remote.new_token()
    ok, msg = remote.set_funnel(config["remote_port"], on)
    if on and ok:
        config["remote_url"] = remote.status(config["remote_port"])["url"]
    config["remote_enabled"] = bool(on and ok)
    save_config()
    return remote_status()


def remote_status():
    st = remote.status(config["remote_port"])
    st.update(enabled=config["remote_enabled"], url=config["remote_url"] or st["url"],
              pushed=watcher.remote_pushed)
    return st


DASHBOARD = r"""<!doctype html><html><head><meta charset="utf-8"><title>snip2tex</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fafaf9;--fg:#1c1c1c;--mut:#777;--card:#fff;--line:#e5e5e5;--ok:#2a8a4a;--bad:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--card:#202020;--line:#333}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px -apple-system,system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px;display:grid;grid-template-columns:300px 1fr;gap:16px}
@media(max-width:760px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px}
h1{font-size:18px;margin:0 0 12px}h2{font-size:13px;text-transform:uppercase;color:var(--mut);margin:0 0 10px;letter-spacing:.05em}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
code,textarea,input{font:12px ui-monospace,Menlo,monospace}
label{display:block;color:var(--mut);font-size:12px;margin:8px 0 3px}
input,textarea,select{width:100%;box-sizing:border-box;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
button{margin-top:10px;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--fg);color:var(--bg);cursor:pointer}
.snip{display:grid;grid-template-columns:1fr 1fr;gap:10px;border-top:1px solid var(--line);padding:12px 0}
.snip img{max-width:100%;background:#fff;border-radius:4px}
.snip pre{grid-column:1/-1;white-space:pre-wrap;margin:0;background:var(--bg);padding:8px;border-radius:6px;cursor:pointer}
.meta{grid-column:1/-1;color:var(--mut);font-size:12px}
</style></head><body><main>
<div>
 <h1>snip2tex</h1>
 <div class="card"><h2>Connection</h2><div id="conn">…</div></div>
 <div class="card"><h2>Config</h2>
  <label>Model runs</label><select id="backend" onchange="showBackend()">
   <option value="builtin">built in (MLX, no LM Studio)</option><option value="lmstudio">in LM Studio</option></select>
  <div id="builtinOpts"><label>Model</label><select id="model_path"></select></div>
  <div id="lmOpts"><label>LM Studio URL</label><input id="lmstudio_url">
  <label>LM Studio model</label><select id="model"></select></div>
  <label>Transcription prompt</label><textarea id="prompt" rows="6"></textarea>
  <label>? button prompt (sent with the picture of the selection)</label><textarea id="ask_prompt" rows="5"></textarea>
  <label>Append to .tex file (blank = off)</label><input id="append_tex_file">
  <label><input type="checkbox" id="copy_to_clipboard" style="width:auto"> Copy LaTeX to clipboard</label>
  <label><input type="checkbox" id="auto_start_xovi" style="width:auto"> Restart xovi after the tablet reboots (USB)</label>
  <button onclick="save()">Save</button>
 </div>
 <div class="card"><h2>Tablet buttons</h2>
  <label><input type="checkbox" id="tablet_replace_snip" style="width:auto"> ∑ replaces the handwriting with the LaTeX</label>
  <label><input type="checkbox" id="tablet_replace_graph" style="width:auto"> Graph replaces the handwriting with the plot</label>
  <label>Padding around the selection (px)</label><input id="tablet_selection_padding" type="number" min="0" max="60">
  <label>Hide the = result after (seconds, 0 = only when tapped)</label><input id="tablet_popup_seconds" type="number" min="0" max="600">
  <label>Wait before grabbing the screen (ms)</label><input id="tablet_grab_delay_ms" type="number" min="200" max="3000" step="50">
  <div class="meta">Sent to the tablet within a few seconds; no restart needed.</div>
  <button onclick="save()">Save</button>
 </div>
 <div class="card"><h2>Wi-Fi mode</h2>
  <div id="remote">…</div>
  <button id="remoteBtn" onclick="toggleRemote()">…</button>
  <div class="meta">Lets the tablet reach this Mac without the cable, over the internet through Tailscale Funnel (works on campus Wi-Fi that blocks devices from seeing each other). The cable is still used whenever it's plugged in. Only the tablet buttons are reachable this way, and only with a secret token; this dashboard never is. Needs the Tailscale app, signed in. After turning it on, plug the tablet in once so it gets the address and token.</div>
 </div>
 <div class="card"><h2>Lid closed</h2>
  <div id="lid">…</div>
  <button id="lidBtn" onclick="toggleLid()">…</button>
  <div class="meta">Keeps the Mac running with the lid shut so the tablet buttons work from your bag. Uses more battery and can get warm in a bag. Turns itself off at <span id="lowBatt">15</span>% battery and when Snip2TeX quits. The first time, macOS asks for your password once.</div>
 </div>
 <div class="card"><h2>App</h2><button onclick="quitApp()">Quit snip2tex</button></div>
 <div class="card"><h2>Test</h2><input type="file" id="f" accept="image/*" onchange="test()"></div>
</div>
<div class="card"><h2>Recent</h2><div id="recent">Nothing yet.</div></div>
</main><script>
let loaded=false;
const TB_BOOL=['tablet_replace_snip','tablet_replace_graph'], TB_NUM=['tablet_selection_padding','tablet_popup_seconds','tablet_grab_delay_ms'];
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const dot=ok=>`<span class="dot" style="background:var(${ok?'--ok':'--bad'})"></span>`;
async function tick(){
 const s=await (await fetch('/api/status')).json();
 const seen=Object.entries(s.tablet_seen);
 document.getElementById('conn').innerHTML=
  `<div>${dot(s.model.state==='ready')}Model ${esc(s.model.state)}${s.model.backend==='builtin'&&s.model.model?' · '+esc(s.model.model.split('/').pop()):''}${s.model.load_seconds?' · loaded in '+s.model.load_seconds+'s':''}</div>
   ${s.model.error?`<div class="meta" style="color:var(--bad)">${esc(s.model.error)}</div>`:''}
   ${Object.values(s.progress).map(p=>`<div class="meta">${esc(p)}</div>`).join('')}
   <div>${dot(s.tablet.state==='ready')}Tablet ${esc(s.tablet.state)}${s.tablet.firmware?' · '+esc(s.tablet.firmware):''}</div>
   ${s.tablet.detail?`<div class="meta">${esc(s.tablet.detail)}</div>`:''}
   <div class="meta">last request: ${seen.length?seen.map(([ip,a])=>`${ip} ${a}s ago`).join(', '):'none yet'}</div>
   <label>Tablet should POST to</label>`+
  s.addresses.map(a=>`<div><code>http://${a.ip}:${s.port}/snip</code> <span style="color:var(--mut)">${esc(a.kind)}</span></div>`).join('');
 if(!loaded){loaded=true;const c=s.config;
  for(const k of ['lmstudio_url','prompt','ask_prompt','append_tex_file','backend'])document.getElementById(k).value=c[k];
  document.getElementById('copy_to_clipboard').checked=c.copy_to_clipboard;
  document.getElementById('auto_start_xovi').checked=c.auto_start_xovi;
  for(const k of TB_BOOL)document.getElementById(k).checked=c[k];
  for(const k of TB_NUM)document.getElementById(k).value=c[k];
  const mp=document.getElementById('model_path'), paths=[...new Set([c.model_path,...s.local_models])];
  mp.innerHTML=paths.map(x=>`<option value="${esc(x)}">${esc(x.split('/').slice(-2).join('/'))}</option>`).join('');
  mp.value=c.model_path;
  const m=document.getElementById('model');
  m.innerHTML='<option value="">(auto: first loaded)</option>'+(s.model.lmstudio_models||[]).map(x=>`<option>${esc(x)}</option>`).join('');
  m.value=c.model; showBackend();}
 const pw=s.power;
 document.getElementById('lid').innerHTML=`${dot(!pw.sleep_disabled)}${pw.sleep_disabled?'Stays awake when closed':'Sleeps when closed (normal)'}${pw.battery!=null?` · battery ${pw.battery}%${pw.on_battery?'':' (charging)'}`:''}${pw.note?`<div class="meta" style="color:var(--bad)">${esc(pw.note)}</div>`:''}`;
 document.getElementById('lidBtn').textContent=pw.sleep_disabled?'Let it sleep when closed':'Keep awake when closed';
 document.getElementById('lidBtn').dataset.awake=pw.sleep_disabled?'1':'';
 document.getElementById('lowBatt').textContent=pw.low_battery;
 document.getElementById('recent').innerHTML=s.recent.length?s.recent.map(r=>`<div class="snip">
  <div class="meta">${new Date(r.time*1000).toLocaleTimeString()} · ${esc(r.from)} · ${r.status}${r.seconds!=null?' · '+r.seconds+'s':''}</div>
  <img src="/snips/${r.id}-in.png">${r.rendered?`<img src="/snips/${r.id}-out.png">`:`<div class="meta">${esc(r.result?'answered':(r.render_error||r.error||''))}</div>`}
  ${r.result?`<div class="meta" style="font-size:15px;color:var(--fg)">${esc(r.result)}</div>`:''}
  ${r.latex?`<pre title="click to copy" onclick="navigator.clipboard.writeText(this.innerText)">${esc(r.latex)}</pre>`:''}
 </div>`).join(''):'Nothing yet.';
}
async function remoteTick(){try{const r=await (await fetch('/api/remote')).json();
 const el=document.getElementById('remote'), b=document.getElementById('remoteBtn');
 let t;
 if(!r.installed)t=`${dot(false)}Tailscale isn't installed: get it from <a href="https://tailscale.com/download/mac" target="_blank">tailscale.com</a> and sign in`;
 else if(!r.running)t=`${dot(false)}Tailscale is installed but not connected: open it and sign in`;
 else if(r.enabled&&r.funnel)t=`${dot(true)}On · ${esc(r.url)}<div class="meta">${r.pushed?'tablet has the address and token':'plug the tablet in once to give it the address and token'}</div>`;
 else if(r.enabled)t=`${dot(false)}On here, but Funnel isn't serving: try turning it off and on`;
 else t=`${dot(false)}Off (USB cable only)`;
 if(r.note)t+=`<div class="meta" style="color:var(--bad)">${esc(r.note)}</div>`;
 el.innerHTML=t; b.style.display=r.installed&&r.running?'':'none';
 b.textContent=r.enabled?'Turn off Wi-Fi mode':'Turn on Wi-Fi mode'; b.dataset.on=r.enabled?'1':'';}catch(e){}}
async function toggleRemote(){const b=document.getElementById('remoteBtn');b.disabled=true;b.textContent='…';
 try{await fetch('/api/remote',{method:'POST',body:JSON.stringify({on:!b.dataset.on})});}finally{b.disabled=false;remoteTick();}}
remoteTick();setInterval(remoteTick,10000);
async function toggleLid(){const b=document.getElementById('lidBtn');b.disabled=true;b.textContent='…';
 try{await fetch('/api/lid',{method:'POST',body:JSON.stringify({awake:!b.dataset.awake})});}finally{b.disabled=false;tick();}}
function showBackend(){const b=document.getElementById('backend').value;
 document.getElementById('builtinOpts').style.display=b==='builtin'?'':'none';
 document.getElementById('lmOpts').style.display=b==='lmstudio'?'':'none';}
async function quitApp(){if(confirm('Quit snip2tex? The tablet buttons stop working until you start it again.')){await fetch('/api/quit',{method:'POST'});document.body.innerHTML='<p style="padding:24px">snip2tex stopped.</p>';}}
async function save(){
 const body={};for(const k of ['lmstudio_url','model','prompt','ask_prompt','append_tex_file','backend','model_path'])body[k]=document.getElementById(k).value;
 body.copy_to_clipboard=document.getElementById('copy_to_clipboard').checked;
 body.auto_start_xovi=document.getElementById('auto_start_xovi').checked;
 for(const k of TB_BOOL)body[k]=document.getElementById(k).checked;
 for(const k of TB_NUM)body[k]=parseInt(document.getElementById(k).value||'0',10);
 await fetch('/api/config',{method:'POST',body:JSON.stringify(body)});tick();
}
async function test(){const f=document.getElementById('f').files[0];if(f){fetch('/api/test',{method:'POST',body:f});setTimeout(tick,300);}}
tick();setInterval(tick,2000);
</script></body></html>"""


def start(open_browser=True):
    """Load the model and serve in a background thread; returns the server."""
    port = config["port"]
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    # Wi-Fi mode's listener: localhost only, Tailscale Funnel forwards to it
    remote_srv = ThreadingHTTPServer(("127.0.0.1", config["remote_port"]), RemoteH)
    threading.Thread(target=remote_srv.serve_forever, daemon=True).start()
    load_model()
    watcher.start()
    power.guard()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"snip2tex on :{port}  dashboard http://localhost:{port}", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [f"http://localhost:{port}"]).start()
    return srv


if __name__ == "__main__":
    start(open_browser="--no-browser" not in sys.argv)
    threading.Event().wait()
