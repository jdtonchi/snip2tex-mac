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
import gallery
import model as backends
import power
import install_tablet
import remote
import tablet
import tutor

STARTED = time.time()
CODE_DIR = Path(__file__).resolve().parent
HOME = Path.home() / ".snip2tex"
SNIPS = HOME / "snips"      # old flat layout, moved into HISTORY on start (migrate_snips)
HISTORY = HOME / "history"  # one folder per press: history/YYYY/MM-DD/HHMMSS-button-xxxx/
CONFIG_PATH = HOME / "config.json"
HISTORY.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "port": 8799,
    "backend": "builtin",  # builtin (mlx-vlm in this process) | lmstudio
    "use_claude": False,   # send every model call to Claude through Claude Code instead
    "claude_model": "claude-haiku-5-5",  # model.CLAUDE_MODELS, or any id Claude Code accepts
    "thinking": False,     # let the model think first (Claude; built-in Gemma for non-JSON calls). Opus always does
    "color": True,         # keep ink colours in screen grabs and use colour in what goes back
    "free_diagrams": True, # with Sonnet or better (or Haiku + thinking), ? draws its own TikZ diagrams instead of the templates
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
    "tablet_exact_selection": True,   # grab only the selected strokes (white behind them), not the whole box
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
    "render_dpi": 226,  # RMPP screen density; the tablet shows 1 image px per screen px, so this also sets the size
    "render_font": "classic",  # FONTS key
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
diagrams.COLOR = config["color"]


def save_config():
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


# ---------- model ----------

builtin = backends.BuiltinModel()
show_hook = None  # set by app.py: brings the app window to the front


_claude = None


def local_llm():
    if config["backend"] == "lmstudio":
        return backends.LMStudioModel(config["lmstudio_url"], config["model"])
    builtin.thinking = config["thinking"]
    return builtin


def llm():
    """The model every button uses, metered for the dashboard's Usage card. With
    use_claude, Claude through Claude Code, falling back to the local model when a
    call fails (signed out, offline, rate-limited) and that model is loaded."""
    global _claude
    if config["use_claude"]:
        try:
            path = tool("claude")
        except RuntimeError:
            path = None
        if path:
            want = (path, config["claude_model"],
                    config["thinking"] or backends.always_thinks(config["claude_model"]))
            if _claude is None or (_claude.claude, _claude.model, _claude.thinking) != want:
                _claude = backends.ClaudeCodeModel(path, config["claude_model"], HOME / "claude-cwd",
                                                   thinking=config["thinking"])
            local = local_llm()
            ready = local.status().get("state") == "ready" if local is builtin else True
            return Metered(_claude, Metered(local) if ready else None)
    return Metered(local_llm())


# ---------- usage ----------

USAGE_PATH = HOME / "usage.jsonl"
usage_lock = threading.Lock()
LIMITS_PATH = HOME / "claude-limits.json"
try:  # latest rate-limit windows reported by Claude Code
    claude_limits = json.loads(LIMITS_PATH.read_text())
except (OSError, ValueError):
    claude_limits = {}


def record_usage(entry):
    with usage_lock:
        with USAGE_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")


class Metered:
    """Wraps a backend: logs every call (backend, seconds, tokens, cost) to usage.jsonl."""

    def __init__(self, inner, fallback=None):
        self.inner, self.fallback = inner, fallback

    def chat(self, prompt, image_png=None, schema=None, temperature=0.0, max_tokens=800):
        t0 = time.time()
        backend = {"ClaudeCodeModel": "claude", "LMStudioModel": "lmstudio",
                   "BuiltinModel": "builtin"}.get(type(self.inner).__name__, "?")
        try:
            out = self.inner.chat(prompt, image_png=image_png, schema=schema,
                                  temperature=temperature, max_tokens=max_tokens)
            err = ""
        except Exception as e:
            out, err = None, str(e)[-200:]
        entry = {"t": round(time.time(), 1), "backend": backend, "seconds": round(time.time() - t0, 2),
                 "image": image_png is not None, "ok": not err}
        u = self.inner.take_usage() if hasattr(self.inner, "take_usage") else None
        if u:
            if u.get("limits"):
                claude_limits.clear()
                claude_limits.update(u["limits"], seen=time.time())
                LIMITS_PATH.write_text(json.dumps(claude_limits))
            entry.update({k: u[k] for k in ("model", "input", "output", "cost")})
        if err:
            entry["error"] = err
        record_usage(entry)
        if err:
            if self.fallback is not None:
                print(f"{backend} failed ({err}); using the local model", flush=True)
                return self.fallback.chat(prompt, image_png=image_png, schema=schema,
                                          temperature=temperature, max_tokens=max_tokens)
            raise RuntimeError(err)
        return out

    def status(self):
        return self.inner.status()


def usage_summary():
    """Calls, tokens and cost for today / the last 7 days / all time, per backend."""
    now = time.time()
    lt = time.localtime(now)
    today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    spans = {"today": today, "week": now - 7 * 86400, "all": 0}
    out = {k: {"calls": 0, "failed": 0, "claude_calls": 0, "local_calls": 0, "input": 0, "output": 0,
               "cost": 0.0, "seconds": 0.0} for k in spans}
    last = None
    try:
        lines = USAGE_PATH.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        last = e
        for k, since in spans.items():
            if e["t"] >= since:
                o = out[k]
                o["calls"] += 1
                o["failed"] += 0 if e.get("ok", True) else 1
                o["claude_calls" if e.get("backend") == "claude" else "local_calls"] += 1
                o["input"] += e.get("input", 0)
                o["output"] += e.get("output", 0)
                o["cost"] += e.get("cost", 0.0)
                o["seconds"] += e.get("seconds", 0.0)
    for o in out.values():
        o["cost"] = round(o["cost"], 4)
        o["avg_seconds"] = round(o["seconds"] / o["calls"], 1) if o["calls"] else 0
    return dict(out, last=last, limits=dict(claude_limits))


def claude_active():
    """use_claude is on and the claude command exists (otherwise llm() uses the local model)."""
    if not config["use_claude"]:
        return False
    try:
        return bool(tool("claude"))
    except RuntimeError:
        return False


def load_model():
    """(Re)load the in-process model in the background, then warm it up. With
    Claude on, the local model is unloaded instead (it's ~15 GB on a 24 GB Mac)."""
    if claude_active():
        builtin.unload()
        threading.Thread(target=warm_up_tools, kwargs={"model": False}, daemon=True).start()
        return
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
        if claude_active():  # Claude was switched on while this was loading
            builtin.unload()
            return
        if builtin.state == "ready":
            builtin.warm_up()
            warm_up_tools()
    threading.Thread(target=run, daemon=True).start()


def warm_up_tools(model=True):
    """Pay the one-time costs (tectonic, SymPy import, matplotlib font cache,
    the graph JSON grammar) at startup instead of on the first button press.
    model=False skips the model call (with Claude it would cost a request)."""
    try:
        render_png(r"\[ x^2 \]")
        render_tikz(r"\begin{tikzpicture}\node[draw] (a) at (0,0) {a}; \draw[->] (a) -- (1,0);\end{tikzpicture}")
        evaluate_latex(r"\frac{1}{2}")
        if model:
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
    clean_screen_grab(im, sel_pad=int(params.get("pad", 0)), color=config["color"]).save(out, "PNG")
    return out.getvalue()


def clean_screen_grab(im, sel_pad=0, margin=32, color=False):
    """Drop the selection overlay if it was still on screen. The grab is the
    selection plus sel_pad px on each side, so the overlay sits sel_pad px in:
    the gray tint goes to white (threshold), then the 2px border, the corner
    resize handles (30px) and the rotation handle (40px circle on the top edge)
    are painted out. A white margin is added so a fraction bar that spans the
    selection doesn't look like a frame.

    color=True keeps coloured ink (the Paper Pro's framebuffer is colour):
    dark pixels and clearly coloured ones that differ from the background
    survive in their own colour, dark greys become black, the rest goes white."""
    from PIL import Image, ImageDraw, ImageOps
    if color:
        import numpy as np
        a = np.asarray(im.convert("RGB")).astype(np.int32)  # int16 overflows in the luminance sum
        lum = (a[..., 0] * 299 + a[..., 1] * 587 + a[..., 2] * 114) // 1000
        chroma = a.max(axis=2) - a.min(axis=2)
        bg = np.median(a.reshape(-1, 3), axis=0)  # the page, or the overlay's tint
        off_bg = np.abs(a - bg).sum(axis=2) > 90
        dark, inked = lum < 110, (chroma > 60) & off_bg
        out = np.full_like(a, 255)
        out[inked] = a[inked]
        out[dark & (chroma <= 40)] = 0
        out[dark & (chroma > 40)] = a[dark & (chroma > 40)]
        im = Image.fromarray(out.astype(np.uint8), "RGB")
        white = (255, 255, 255)
    else:
        im = im.convert("L").point(lambda v: 0 if v < 110 else 255)
        white = 255
    d, W, H, p = ImageDraw.Draw(im), im.width, im.height, sel_pad
    d.rectangle((p - 3, p - 3, W - 1 - p + 3, H - 1 - p + 3), outline=white, width=7)
    for cx, cy in ((p, p), (W - 1 - p, p), (p, H - 1 - p), (W - 1 - p, H - 1 - p)):
        d.rectangle((cx - 17, cy - 17, cx + 17, cy + 17), fill=white)
    d.ellipse((W // 2 - 24, p - 24, W // 2 + 24, p + 24), fill=white)
    return ImageOps.expand(im, border=margin, fill=white).convert("RGB")


DIAGRAM_HINT = ("\n\nException: if the image is mainly a drawing rather than writing (shapes or "
                "labels joined by arrows or lines, a flowchart, tree, network, circuit, cycle, "
                "geometric figure, or a labelled sketch), output only the single word DIAGRAM.")


# Paper Pro ink colours, defined for \\textcolor in rendered LaTeX and TikZ
INK_COLORS = {"red": "C62828", "blue": "1F4FD1", "green": "2E7D32", "yellow": "F2B705",
              "orange": "EF6C00", "pink": "D81B60", "purple": "7B1FA2", "cyan": "00838F",
              "magenta": "C2185B", "gray": "757575"}
INK_DEFS = "\\usepackage{xcolor}\n" + "\n".join(
    "\\definecolor{%s}{HTML}{%s}" % kv for kv in INK_COLORS.items())

COLOR_HINT = ("\n\nInk colours: if part of the writing is in a coloured ink (not black or gray), "
              "wrap exactly that part in \\textcolor{NAME}{...} with the closest of: "
              + ", ".join(c for c in INK_COLORS if c != "gray")
              + ". Black and gray writing gets no colour command.")


def image_to_latex(png, diagram=False):
    """LaTeX for the selection. diagram=True (∑ button): a drawing comes back as
    "DIAGRAM" instead, for tikz_diagram, and with color on, coloured ink comes
    back as \\textcolor (only for ∑: SymPy and the graph button want plain LaTeX)."""
    prompt = config["prompt"] + (DIAGRAM_HINT if diagram else "")
    if diagram and config["color"]:
        prompt += COLOR_HINT
    return strip_fences(llm().chat(prompt, image_png=prep_image(png),
                                   temperature=config["temperature"], max_tokens=600))


TIKZ_PROMPT = """Redraw this hand-drawn diagram as a TikZ picture.
Output ONLY \\begin{{tikzpicture}} ... \\end{{tikzpicture}}: no preamble, no \\usetikzlibrary,
no fences, no commentary.
- Keep the layout as drawn: use absolute coordinates in cm, x from 0 to {w}, y from 0 to {h}
  (y grows upward), placing each part where it is in the drawing.
- Labelled shapes are nodes: draw, circle / rectangle / rounded corners / ellipse / diamond
  as drawn, e.g. \\node[draw, rounded corners] (a) at (1,2) {{Start}};
- Connections: \\draw[->] (a) -- (b); (or [<->], [-] as drawn); bend left/right for curved
  ones; put labels written on arrows as node[midway, above] {{...}}.
- Copy every word, number and symbol exactly as written; math in $...$.
- {colors}
- Available libraries: arrows.meta, positioning, shapes.geometric, shapes.misc, calc,
  decorations.pathmorphing, automata, angles, quotes."""

TIKZ_DOC = r"""\documentclass[border=%(border)dpt]{standalone}
\usepackage{amsmath,amssymb,graphicx}
\usepackage{tikz}
\usetikzlibrary{arrows.meta,positioning,shapes.geometric,shapes.misc,calc,decorations.pathmorphing,automata,angles,quotes}
%(inks)s
%(extra)s
\tikzset{every picture/.style={line width=0.8pt, >={Stealth[length=2.2mm]}}}
%(fonts)s
\begin{document}
\scalebox{%(k).3f}{%(body)s}
\end{document}
"""


def tikz_diagram(png):
    """∑ on a drawing: Gemma (or Claude) writes TikZ for it; one retry with the
    LaTeX error. Returns (tikz source, PNG)."""
    import re
    from PIL import Image
    w, h = Image.open(__import__("io").BytesIO(png)).size
    cw = 10.0
    ch = round(cw * h / max(w, 1), 1)
    colors = (("Draw each part in the colour it was drawn in, as draw=NAME / text=NAME with NAME one of "
               + ", ".join(INK_COLORS) + " (black parts stay black); no fill, no shading.")
              if config["color"] else "Black lines only: no colors, no fill, no shading.")
    prompt = TIKZ_PROMPT.format(w=cw, h=ch, colors=colors)
    img = prep_image(png)
    err = ""
    for attempt in range(2):
        ask = prompt if not err else (prompt + f"\n\nYour last picture failed to compile:\n{err}\n"
                                      f"The code was:\n{code}\nFix it.")
        code = strip_fences(llm().chat(ask, image_png=img, temperature=0.0, max_tokens=2000))
        m = re.search(r"\\begin\{tikzpicture\}.*\\end\{tikzpicture\}", code, re.S)
        code = m.group(0) if m else code
        try:
            return code, render_tikz(code)
        except RuntimeError as e:
            err = str(e)[-600:]
            print(f"tikz failed (try {attempt + 1}):", err.splitlines()[-1:] or err, flush=True)
    raise RuntimeError("couldn't draw that diagram: " + err[-200:])


def render_tikz(code, dpi=None, font=None, extra=""):
    dpi = dpi or config["render_dpi"]
    fonts = FONTS.get(font or config["render_font"], FONTS["classic"])[1]
    with tempfile.TemporaryDirectory() as d:
        tex = Path(d) / "s.tex"
        tex.write_text(TIKZ_DOC % {"border": 4 * dpi / 72, "k": dpi / 72, "fonts": fonts, "body": code,
                                   "inks": INK_DEFS, "extra": extra})
        r = subprocess.run([tool("tectonic"), "--chatter", "minimal", str(tex)],
                           cwd=d, capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError("LaTeX failed:\n" + (r.stderr or r.stdout)[-800:])
        out = Path(d) / "s.png"
        subprocess.run([tool("sips"), "-s", "format", "png", "-s", "dpiWidth", str(dpi), "-s", "dpiHeight", str(dpi),
                        str(Path(d) / "s.pdf"), "--out", str(out)], capture_output=True, check=True)
        return out.read_bytes()


# ---------- rendering ----------

# sips rasterizes PDFs at 72dpi, so scale the type up by dpi/72 instead.
DOC = r"""\documentclass[border=%(border)dpt,varwidth=%(width)dpt]{standalone}
\usepackage{amsmath,amssymb,anyfontsize}
%(inks)s
%(fonts)s
\begin{document}
\fontsize{%(size).1f}{%(lead).1f}\selectfont
%(body)s
\end{document}
"""


# Font styles for rendered LaTeX (tectonic is XeTeX, so unicode-math works).
# An unknown or failing font falls back to classic (_render).
FONTS = {
    "classic": ("Classic (Computer Modern)", ""),
    "sans": ("Clean sans (Fira)",
             r"\usepackage{unicode-math}\setmainfont{FiraSans-Regular.otf}\setmathfont{FiraMath-Regular.otf}"),
}


def render_png(latex, dpi=None, font=None):
    try:
        return _render(latex, dpi, font)
    except RuntimeError:
        # models often return bare math with no $...$; retry in display math
        if not any(t in latex for t in ("$", "\\[", "\\begin")):
            return _render("\\[\n" + latex + "\n\\]", dpi, font)
        raise


def _render(latex, dpi=None, font=None):
    font = font or config["render_font"]
    try:
        return _render_once(latex, dpi or config["render_dpi"], FONTS.get(font, FONTS["classic"])[1])
    except RuntimeError:
        if font == "classic":
            raise
        # a font package or font missing: still give a result
        print(f"render with font {font} failed, using classic", flush=True)
        return _render_once(latex, dpi or config["render_dpi"], "")


def _render_once(latex, dpi, fonts):
    with tempfile.TemporaryDirectory() as d:
        tex = Path(d) / "s.tex"
        k = dpi / 72
        tex.write_text(DOC % {"border": 4 * k, "width": 400 * k, "size": 11 * k,
                              "lead": 13.6 * k, "fonts": fonts, "body": latex, "inks": INK_DEFS})
        r = subprocess.run([tool("tectonic"), "--chatter", "minimal", str(tex)],
                           cwd=d, capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError("LaTeX failed:\n" + (r.stderr or r.stdout)[-800:])
        out = Path(d) / "s.png"
        subprocess.run([tool("sips"), "-s", "format", "png", "-s", "dpiWidth", str(dpi),
                        "-s", "dpiHeight", str(dpi),
                        str(Path(d) / "s.pdf"), "--out", str(out)],
                       capture_output=True, check=True)
        return out.read_bytes()


PREVIEW_LATEX = r"\[ \frac{d}{dx}\left(x^3 \sin x\right) = 3x^2 \sin x + x^3 \cos x \]"


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


SYMPY_PROMPT = """You set up a calculation from a student's handwritten math (image attached).
Transcription (it may miss arrows and layout, trust the image):

{latex}

First decide what is given and what is asked, then write it for SymPy.
- givens: names the student assigns a value to, in the order written.
  "n = 4", "let n be 4", "n: 4", and an arrow between n and 4 in either
  direction (4 -> n or n -> 4) all mean n is 4: the name gets the number or
  expression at the other end of the arrow. A value may use earlier givens.
  Never list the thing being asked for as a given.
- sympy: ONE SymPy expression for what is asked, written with the given names
  (do not plug the values in yourself).
- solve_for: the unknown when an equation has to be solved, else "".
How to read what is asked:
- A bare expression, or "expression = ?": evaluate it. solve_for is "".
- An equation with an unknown ("2x + 3 = 11", "F = m a" with F and m given):
  sympy is Eq(lhs, rhs), solve_for is the one name that is not given.
- A ? or blank box inside an equation ("3 + ? = 7"): write it as the symbol q
  and solve for q.
- A named operation (factor, expand, simplify, derivative, ...): wrap the
  expression in it.
Plain SymPy syntax only: ** for powers, * for multiplication, no LaTeX, no backslashes.
Available: sin cos tan asin acos atan exp log sqrt Abs pi E I oo,
diff(f, x), integrate(f, x), integrate(f, (x, a, b)), limit(f, x, a),
Sum(f, (n, a, b)), Product(...), Eq(lhs, rhs), simplify, factor, expand, apart,
series(f, x, a, n), Matrix([[1, 2], [3, 4]]).det(), Matrix(...).inv(), gcd, lcm,
factorint, isprime, binomial, factorial, N(expr).
Units: convert_to(5*kilometer, mile); names like meter, kilometer, second, hour,
kilogram, gram, newton, joule, electronvolt, watt, mile, inch, foot, celsius is not available.
Constants: planck, hbar, speed_of_light, gravitational_constant, boltzmann,
elementary_charge, electron_rest_mass, avogadro_number.
Examples (givens as name: value):
  "factor x^2 - 9"                 -> givens none, sympy factor(x**2 - 9)
  "\\int_0^\\pi \\sin x dx"          -> givens none, sympy integrate(sin(x), (x, 0, pi))
  "a = 3, b = 4, \\sqrt{a^2+b^2} = ?" -> givens a: 3, b: 4, sympy sqrt(a**2 + b**2)
  "4 -> n, n!"                     -> givens n: 4, sympy factorial(n)
  "x^2 - 5x + 6 = 0"               -> sympy Eq(x**2 - 5*x + 6, 0), solve_for x
  "m = 2, F = 10, F = ma"          -> givens m: 2, F: 10, sympy Eq(F, m*a), solve_for a
  "x = 2, d/dx x^3"                -> givens x: 2, sympy diff(x**3, x)
  "3 hours in seconds"             -> sympy convert_to(3*hour, second)
If it is not math that can be computed (words, a greeting, a conceptual
question), set is_math to false and sympy to ""."""

SYMPY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_math": {"type": "boolean"},
        "givens": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "value": {"type": "string"}},
            "required": ["name", "value"], "additionalProperties": False}},
        "sympy": {"type": "string"},
        "solve_for": {"type": "string"},
    },
    "required": ["is_math", "givens", "sympy", "solve_for"],
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


def fix_code(code):
    """Undo JSON escapes the model triggers by writing LaTeX (\\frac -> form feed + 'rac')
    and turn leftover LaTeX into SymPy."""
    import re
    code = code.translate({0x0c: "\\f", 0x09: "\\t", 0x0d: "\\r", 0x08: "\\b"}).strip()
    if "\\" not in code:
        return code
    from sympy.parsing.latex import parse_latex
    src = re.sub(r"\\(?:text|mathrm|operatorname)\{(\w+)\}", r"\\\1", code).replace("**", "^")
    try:
        return str(parse_latex(src, backend="lark"))
    except Exception:
        return code


def run_sympy(code, timeout=8, local=None):
    """Evaluate a model-written SymPy expression in the restricted namespace."""
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, convert_xor

    def go():
        val = parse_expr(checked(code), local_dict=dict(local or {}), global_dict=sympy_namespace(),
                         transformations=standard_transformations + (convert_xor,))
        return val.doit() if hasattr(val, "doit") else val

    return with_timeout(go, timeout)


def checked(code):
    import re
    if re.search(r"__|\bimport\b|\blambda\b|\bopen\b|\beval\b|\bexec\b", code):
        raise ValueError("refusing that expression")
    return code


def with_timeout(fn, timeout=8):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(fn).result(timeout=timeout)


def run_plan(plan, timeout=8):
    """Givens -> values, then evaluate (or solve) what is asked with them plugged in.
    Values are substituted after evaluating, so diff(x**3, x) with x = 2 gives 12, not diff(8, 2).
    Returns (value, unknown symbol or None, givens shown as text)."""
    import re, sympy
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, convert_xor
    tr = standard_transformations + (convert_xor,)
    ns = sympy_namespace()
    unknown = (plan.get("solve_for") or "").strip()
    givens = [(g["name"].strip(), fix_code(g["value"])) for g in plan.get("givens") or []
              if re.fullmatch(r"[A-Za-z]\w*", g.get("name", "").strip()) and g["name"].strip() != unknown]
    # given names are plain symbols even when SymPy has a constant by that name (E, I, S, N)
    local = {n: sympy.Symbol(n) for n, _ in givens}
    if unknown and re.fullmatch(r"[A-Za-z]\w*", unknown):
        local[unknown] = sympy.Symbol(unknown)
    else:
        unknown = ""

    def go():
        values = {}
        for name, code in givens:
            v = parse_expr(checked(code), local_dict=dict(local), global_dict=ns, transformations=tr)
            v = v.doit() if hasattr(v, "doit") else v
            values[local[name]] = v.subs(values) if hasattr(v, "subs") else v
        val = parse_expr(checked(fix_code(plan["sympy"])), local_dict=dict(local), global_dict=ns,
                         transformations=tr)
        val = val.doit() if hasattr(val, "doit") else val
        if values and hasattr(val, "subs"):
            val = val.subs(values)
            val = val.doit() if hasattr(val, "doit") else val
        if unknown:
            x = local[unknown]
            eq = val if isinstance(val, sympy.Equality) else sympy.Eq(val, 0)
            return sympy.solve(eq, x), x
        if isinstance(val, sympy.Equality) and len(val.free_symbols) == 1:
            x = next(iter(val.free_symbols))
            return sympy.solve(val, x), x
        return val, None

    val, x = with_timeout(go, timeout)
    shown = ", ".join(f"{n} = {fmt_expr(c)}" for n, c in givens)
    return val, x, shown


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


def compute(latex, png=None):
    """(text, is_math) for the = and ? buttons.

    A single plain expression or equation goes straight to SymPy's LaTeX parser (exact,
    no model). Everything else (givens like "n = 4" or an arrow from 4 to n, "expr = ?",
    derivatives, units, ...) is planned by Gemma from the image: givens, the expression
    asked for, and what to solve for. SymPy does the math. Prose is not math."""
    import re, sympy
    from sympy.parsing.latex import parse_latex
    src = strip_math_delims(latex)
    single = not re.search(r"\?|,|;|\\\\|&|\\begin|arrow|\\to\b|\\mapsto|\\gets", src) and src.count("=") <= 1
    if single and not looks_like_words(latex):
        try:
            expr = parse_latex(src, backend="lark")
            if isinstance(expr, sympy.Equality):
                return evaluate_latex(latex), True
            if isinstance(expr, sympy.Basic) and not expr.doit().free_symbols:
                return "= " + fmt_value(expr.doit()), True
        except Exception:
            pass
    prompt = SYMPY_PROMPT.replace("{latex}", latex)
    image = prep_image(png) if png is not None else None
    plan, err = None, None
    for attempt in range(2):
        ask = prompt if not err else (prompt + f"\n\nYour last answer was {json.dumps(plan)}\n"
                                      f"SymPy rejected it: {err}\nFix it. Plain SymPy, no backslashes.")
        plan = json.loads(strip_fences(llm().chat(ask, image_png=image, schema=SYMPY_SCHEMA,
                                                  temperature=0.0, max_tokens=300)))
        if not plan.get("is_math") or not plan.get("sympy", "").strip():
            return "", False
        try:
            try:
                val, x, givens = run_plan(plan)
            except Exception:
                fixed = repair_sympy(fix_code(plan["sympy"]))
                if not fixed:
                    raise
                val, x, givens = run_plan(dict(plan, sympy=fixed))
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"sympy failed (try {attempt + 1}):", json.dumps(plan), "->", err, flush=True)
    else:
        # the plain LaTeX parser may still manage it
        return evaluate_latex(latex), True
    if x is not None:
        if not val:
            text = f"no solution for {x}"
        elif all(isinstance(v, dict) for v in val):
            text = fmt_value(val)
        else:
            text = f"{x} = " + ", ".join(fmt_value(v) for v in val)
    else:
        # an expression with nothing to do (e.g. (x+1)^2): show it expanded
        bare = not re.match(r"\s*[A-Za-z_]\w*\(", plan["sympy"])
        if bare and isinstance(val, sympy.Basic) and val.free_symbols and not isinstance(val, sympy.Equality):
            expanded = sympy.expand(val)
            if expanded != val:
                val = expanded
        prefix = "" if isinstance(val, (list, tuple, dict)) or isinstance(val, sympy.Equality) else "= "
        text = prefix + fmt_value(val)
    if givens:
        text = f"{givens} → " + (text[2:] if text.startswith("= ") else text)
    return text, True


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
    if config["color"]:
        styles = [(c, "-") for c in diagrams.INKS]

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


# ---------- free diagrams (Sonnet and up) ----------

FREE_PREAMBLE = r"""\usepackage{pgfplots}\pgfplotsset{compat=1.18}
\usetikzlibrary{shapes.symbols,shapes.arrows,decorations.markings,decorations.pathreplacing,patterns,fit,backgrounds,trees,mindmap,circuits.ee.IEC,3d}"""

FREE_PROMPT = """This is a snippet from a student's handwritten notes on an e-ink tablet. They selected it and asked for a visual.{written}
Design the diagram that best answers or illustrates it. Any kind that fits: a graph, a labelled figure, a free-body diagram, a cycle, a timeline, a table, a circuit, a labelled sketch of an object or organism, geometry with the unknowns solved, a comparison. It is inserted into their notes below the selection.

Reply with JSON:
- "tikz": one complete \\begin{{tikzpicture}} ... \\end{{tikzpicture}}. Nothing else: no preamble, \\usetikzlibrary, \\documentclass or \\input.
- "caption": one short plain-text sentence for a popup (the answer, if they asked a question). No LaTeX or markdown; write math with Unicode (x², π, →).

Drawing rules:
- At most 14 cm wide and 12 cm tall. Normal text size (never \\tiny or \\scriptsize). Labels must not overlap lines, shapes or each other: leave room and place them deliberately.
- Work out every number yourself and get it right. Plot functions with pgfplots (\\begin{{axis}}) or \\draw plot, with labelled axes.
- {colors}
- Available: TikZ libraries arrows.meta, positioning, calc, shapes.geometric, shapes.misc, shapes.symbols, shapes.arrows, decorations.pathmorphing, decorations.markings, decorations.pathreplacing, patterns, angles, quotes, fit, backgrounds, trees, mindmap, circuits.ee.IEC, 3d, automata; packages amsmath, amssymb, pgfplots (compat 1.18). Nothing else is loaded."""

FREE_SCHEMA = {"type": "object", "properties": {"tikz": {"type": "string"}, "caption": {"type": "string"}},
               "required": ["tikz", "caption"], "additionalProperties": False}

# TeX that could read or write files on the Mac
UNSAFE_TEX = __import__("re").compile(r"\\(input|include|openin|openout|read|write|immediate|newwrite|"
                                       r"newread|catcode|directlua|special|usepackage|documentclass)(?![a-zA-Z])")


def free_diagrams_on():
    return bool(config["free_diagrams"] and claude_active() and backends.draws_freely(config["claude_model"], config["thinking"]))


def free_diagram(png, written=""):
    """? button with Sonnet or better: the model designs the diagram itself as
    TikZ (no template); one retry with the LaTeX error. Returns (caption, PNG, TikZ)."""
    import re
    colors = ("Use any colours, fills and shading you like." if config["color"]
              else "Black and gray only; no colours.")
    prompt = FREE_PROMPT.format(written=f'\nIt says: "{written}"' if written else "", colors=colors)
    err, code = "", ""
    for attempt in range(2):
        ask = prompt if not err else (prompt + f"\n\nYour last picture failed to compile:\n{err}\n"
                                      f"The code was:\n{code}\nFix it.")
        out = json.loads(llm().chat(ask, image_png=png, schema=FREE_SCHEMA, temperature=0.2, max_tokens=4000))
        code = strip_fences(out.get("tikz", ""))
        m = re.search(r"\\begin\{tikzpicture\}.*\\end\{tikzpicture\}", code, re.S)
        code = m.group(0) if m else code
        if UNSAFE_TEX.search(code):
            err = "not allowed: " + UNSAFE_TEX.search(code).group(0)
            continue
        try:
            img = render_tikz(code, extra=FREE_PREAMBLE)
            caption = " ".join(str(out.get("caption", "")).split())
            return caption, img, code
        except RuntimeError as e:
            err = str(e)[-600:]
            print(f"free diagram failed (try {attempt + 1}):", err.splitlines()[-1:] or err, flush=True)
    raise RuntimeError("couldn't draw that: " + err[-200:])


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
    if kind != "answer" and free_diagrams_on():
        try:
            caption, out, code = free_diagram(img, written)
            return caption or "added a diagram", out, "free", code
        except Exception as e:
            print("ask: free diagram failed, using a template:", e, flush=True)
    # a diagram that fails falls back to a text answer
    for k in ([kind] if kind != "answer" else []):
        try:
            out, caption, spec = diagrams.make(k, chat, img, config["render_dpi"], function_graph, written)
            label = {"function": "graph", "geometry": "figure", "flowchart": "diagram", "chart": "chart"}[k]
            return (caption if caption else f"added a {label}"), out, k, None
        except Exception as e:
            print(f"ask: {k} failed:", e, flush=True)
    return ask_image(png), None, "answer", None


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


def on_white(png):
    """Flatten onto white: the tablet shows transparent pixels as black
    (LaTeX/TikZ renders from sips have a transparent background)."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    if im.mode in ("RGB", "L"):
        return png
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, "white")
    out = io.BytesIO()
    Image.alpha_composite(bg, im).convert("RGB").save(out, "PNG")
    return out.getvalue()


# ---------- history ----------

INFO_KEYS = ("id", "time", "button", "kind", "status", "model", "from", "seconds", "result", "latex",
             "tikz", "graph", "error", "render_error")


def history_dir(sid, button):
    """history/2026/10-21/125509-ask-e7ca for id 20261021-125509-e7ca."""
    day, hms, tag = sid.split("-")
    return HISTORY / day[:4] / f"{day[4:6]}-{day[6:]}" / f"{hms}-{button}-{tag}"


def current_model_name():
    if claude_active():
        return config["claude_model"]
    if config["backend"] == "lmstudio":
        return config["model"] or "LM Studio"
    return Path(config["model_path"]).name


def save_history(entry, out):
    d = HISTORY / entry["dir"]
    d.mkdir(parents=True, exist_ok=True)
    if out:
        (d / "output.png").write_bytes(out)
    info = {k: entry[k] for k in INFO_KEYS if entry.get(k) not in (None, "")}
    (d / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))


def migrate_snips():
    """Move the old flat snips/<id>-in.png / -out.png into history folders (once)."""
    if not SNIPS.is_dir():
        return
    moved = 0
    for f in sorted(SNIPS.glob("*-in.png")):
        sid = f.name[:-len("-in.png")]
        try:
            t = time.mktime(time.strptime(sid[:15], "%Y%m%d-%H%M%S"))
            d = history_dir(sid, "older")  # the old files don't say which button
        except (ValueError, IndexError):
            continue
        d.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), d / "input.png")
        old_out = SNIPS / (sid + "-out.png")
        if old_out.exists():
            shutil.move(str(old_out), d / "output.png")
        (d / "info.json").write_text(json.dumps({"id": sid, "time": t, "button": "older", "status": "ok",
                                                 "migrated": True}, indent=2))
        moved += 1
    for o in SNIPS.glob("*-out.png"):  # outputs without an input
        o.unlink()
    try:
        SNIPS.rmdir()
    except OSError:
        pass
    if moved:
        print(f"moved {moved} old snips into {HISTORY}", flush=True)


def history_list():
    """Every saved press, newest first (the gallery)."""
    items = []
    for info in HISTORY.glob("*/*/*/info.json"):
        try:
            e = json.loads(info.read_text())
        except (OSError, ValueError):
            continue
        d = info.parent
        e["dir"] = str(d.relative_to(HISTORY))
        e["has_input"], e["has_output"] = (d / "input.png").exists(), (d / "output.png").exists()
        items.append(e)
    items.sort(key=lambda e: e.get("time", 0), reverse=True)
    return items


def history_path(rel):
    """A folder inside HISTORY (exactly YYYY/MM-DD/name), or None."""
    p = (HISTORY / rel).resolve()
    if p.parent.parent.parent != HISTORY.resolve() or not p.is_dir():
        return None
    return p


def process(png, source_ip, evaluate=False, graph=False, ask=False, action=None):
    """Runs a button; returns (entry, PNG or None). Every PNG goes back opaque.
    Input, output and what was said are saved in history/."""
    button = action or ("eval" if evaluate else "graph" if graph else "ask" if ask else "snip")
    entry, out = _process(png, source_ip, evaluate, graph, ask, action, button)
    if out:
        try:
            out = on_white(out)
        except Exception as e:
            print("flattening onto white failed:", e, flush=True)
    try:
        save_history(entry, out)
    except Exception as e:
        print("saving to history failed:", e, flush=True)
    return entry, out


def _process(png, source_ip, evaluate=False, graph=False, ask=False, action=None, button="snip"):
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    entry = {"id": sid, "time": time.time(), "from": source_ip, "status": "working", "button": button,
             "model": current_model_name()}
    entry["dir"] = str(history_dir(sid, button).relative_to(HISTORY))
    (HISTORY / entry["dir"]).mkdir(parents=True, exist_ok=True)
    (HISTORY / entry["dir"] / "input.png").write_bytes(png)
    with lock:
        recent.insert(0, entry)
        del recent[50:]
    t0 = time.time()
    try:
        if action == "solve":
            text, out = tutor.solve(llm().chat, prep_image(png), render_png)
            entry["result"], entry["kind"] = text, "solve"
            entry["rendered"] = True
            entry["status"] = "ok"
            return entry, out
        if action == "check":
            entry["result"], entry["kind"] = tutor.check(llm().chat, prep_image(png)), "check"
            entry["status"] = "ok"
            return entry, None
        if ask:
            text, out, kind, source = ask_smart(png)
            entry["result"], entry["kind"] = text, kind
            if source:
                entry["tikz"] = source
            if out:
                entry["rendered"] = True
            entry["status"] = "ok"
            return entry, out
        latex = image_to_latex(png, diagram=not (evaluate or graph))
        if latex.strip().strip(".").upper() == "DIAGRAM":
            # ∑ on a drawing: redraw it with TikZ
            entry["kind"] = "tikz"
            try:
                latex, out = tikz_diagram(png)
                entry["latex"] = latex
                entry["rendered"] = True
            except Exception as e:
                # still give a result: the plain transcription
                print("tikz gave up, transcribing instead:", e, flush=True)
                entry["render_error"] = str(e)
                latex = image_to_latex(png)
        entry["latex"] = latex
        if entry.get("rendered"):
            if config["copy_to_clipboard"]:
                subprocess.run([tool("pbcopy")], input=latex.encode())
            entry["status"] = "ok"
            return entry, out
        if evaluate:
            text, is_math = compute(latex, png)
            entry["result"] = text if is_math else "not math (try the ? button)"
            entry["status"] = "ok"
            return entry, None
        if graph:
            spec = repair_spec(graph_spec(latex), latex)
            entry["graph"] = spec
            out = render_graph(spec)
            entry["rendered"] = True
            entry["status"] = "ok"
            return entry, out
        try:
            out = render_png(latex)
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
        "fonts": {k: name for k, (name, _) in FONTS.items()},
        "claude_models": backends.CLAUDE_MODELS,
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
        if path == "/api/usage":
            return self.send(200, usage_summary())
        if path == "/api/setup":
            return self.send(200, dict(setup_state, needed=needs_setup(),
                                       reachable=install_tablet.reachable(config["tablet_host"]),
                                       help=install_tablet.PASSWORD_HELP))
        if path == "/api/preview":
            # the dashboard's sample of the render settings, before saving them
            from urllib.parse import parse_qs, urlparse
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                dpi = min(600, max(72, int(q.get("dpi", config["render_dpi"]))))
                out = render_png(PREVIEW_LATEX, dpi, q.get("font"))
            except Exception as e:
                return self.send(500, {"error": str(e)[-300:]})
            return self.send(200, out, "image/png")
        if path == "/gallery":
            return self.send(200, gallery.PAGE, "text/html; charset=utf-8")
        if path == "/api/history":
            return self.send(200, history_list())
        if path.startswith("/history/"):
            # /history/YYYY/MM-DD/name/input.png | output.png
            import urllib.parse
            rel, _, name = urllib.parse.unquote(path[len("/history/"):]).rpartition("/")
            d = history_path(rel)
            if d and name in ("input.png", "output.png") and (d / name).exists():
                return self.send(200, (d / name).read_bytes(), "image/png")
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
        if path == "/model":
            # the tablet's toolbar ✦ menu: ?set=<local|claude id> / ?thinking=0|1,
            # replies with the menu. Only the tablet: USB, or Wi-Fi with the token
            # (RemoteH already checked the token before getting here)
            if not (self.is_local() or self.client_address[0].startswith("10.11.99.")
                    or isinstance(self, RemoteH)):
                return self.send(403, {"error": "tablet-only"})
            import urllib.parse
            q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
            try:
                set_model_from_tablet(q.get("set"), q.get("thinking"))
                if q.get("wifi") == "1" and not config["remote_enabled"]:
                    # ✦ menu "Wi-Fi": switch Wi-Fi mode on; the watcher then gives
                    # the tablet the Funnel URL and token over USB
                    st = set_remote(True)
                    wifi_note["error"] = "" if config["remote_enabled"] else (st.get("note") or "couldn't start Tailscale Funnel")
                    print("tablet asked for Wi-Fi mode:", "on" if config["remote_enabled"] else wifi_note["error"], flush=True)
            except ValueError as e:
                return self.send(400, dict(model_menu(), error=str(e)))
            return self.send(200, model_menu())
        if not self.is_local():
            return self.send(403, {"error": "localhost-only"})
        if path == "/api/config":
            update_config(json.loads(self.read_body()))
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
        if path == "/api/setup":
            # {"password": "..."}: first-launch tablet setup; the password isn't stored
            if not setup_state["running"]:
                run_setup(json.loads(self.read_body() or b"{}").get("password") or None)
            return self.send(200, setup_state)
        if path == "/api/remote":
            # {"on": true} turns Wi-Fi mode (Tailscale Funnel) on
            on = bool(json.loads(self.read_body() or b"{}").get("on"))
            return self.send(200, set_remote(on))
        if path in ("/api/history/delete", "/api/history/reveal"):
            d = history_path(str(json.loads(self.read_body() or b"{}").get("dir", "")))
            if not d:
                return self.send(404, {"error": "not found"})
            if path.endswith("delete"):
                shutil.rmtree(d)
            else:
                subprocess.run(["open", "-R", str(d / "info.json")])
            return self.send(200, {"ok": True})
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


# ---------- first-launch setup ----------

setup_state = {"running": False, "log": [], "ok": False, "error": ""}


def update_config(new):
    """Dashboard (and tablet) config changes; reloads the model when needed."""
    before = (config["backend"], config["model_path"], config["use_claude"])
    for k, v in new.items():
        if k in DEFAULTS:
            config[k] = type(DEFAULTS[k])(v)
    save_config()
    diagrams.COLOR = config["color"]
    if (config["backend"], config["model_path"], config["use_claude"]) != before:
        load_model()


def model_menu():
    """What the tablet's toolbar ✦ menu shows: the local model, the Claude
    models (when Claude Code is installed), which one is in use, thinking."""
    local = (Path(config["model_path"]).name if config["backend"] == "builtin"
             else (config["model"] or "LM Studio"))
    # gemma-4-26B-A4B-it-QAT-MLX-4bit -> gemma-4-26B-A4B
    local = __import__("re").sub(r"-(it|MLX|mlx|QAT|\d+bit)\b.*", "", local) or local
    options = [{"id": "local", "label": "On this Mac: " + local}]
    try:
        has_claude = bool(tool("claude"))
    except RuntimeError:
        has_claude = False
    if has_claude:
        ids = dict(backends.CLAUDE_MODELS)
        ids.setdefault(config["claude_model"], config["claude_model"])
        options += [{"id": k, "label": "Claude " + v} for k, v in ids.items()]
    current = config["claude_model"] if claude_active() else "local"
    locked = current != "local" and backends.always_thinks(current)
    st = llm().status()
    return {"options": options, "current": current, "thinking": bool(config["thinking"] or locked),
            "thinking_locked": locked, "state": st.get("state", ""), "error": st.get("error", "")[:120],
            "wifi_enabled": bool(config["remote_enabled"]), "wifi_error": wifi_note["error"][:160]}


wifi_note = {"error": ""}  # why the tablet's last "turn Wi-Fi mode on" failed


def set_model_from_tablet(choice=None, thinking=None):
    new = {}
    if choice == "local":
        new["use_claude"] = False
    elif choice:
        if choice not in {o["id"] for o in model_menu()["options"]}:
            raise ValueError(f"unknown model {choice}")
        new.update(use_claude=True, claude_model=choice)
    if thinking in ("0", "1"):
        new["thinking"] = thinking == "1"
    if new:
        print("tablet model menu:", new, flush=True)
        update_config(new)


def needs_setup():
    """No key yet, the key isn't accepted, or the patch isn't on the tablet."""
    st = watcher.status()
    return (not install_tablet.KEY.exists() or st["state"] == "no ssh key"
            or "patch missing" in st["detail"]
            or (st["state"] == "error" and "denied" in st["detail"].lower()))


def run_setup(password):
    """Dashboard onboarding: install the key with the password (used once, not
    saved) and copy the tablet patch. Progress in setup_state."""
    setup_state.update(running=True, log=[], ok=False, error="")

    def work():
        try:
            install_tablet.install(config["tablet_host"], config["port"], password=password,
                                   log=setup_state["log"].append)
            setup_state["ok"] = True
        except install_tablet.SetupError as e:
            setup_state["error"] = str(e)
        except Exception as e:
            setup_state["error"] = f"{type(e).__name__}: {e}"
        finally:
            setup_state["running"] = False
    threading.Thread(target=work, daemon=True).start()


TABLET_PATHS = ("/snip", "/eval", "/graph", "/ask", "/solve", "/check", "/model")


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
 <h1 style="display:flex;align-items:center;gap:10px">snip2tex <a href="/gallery" style="margin-left:auto;font-size:13px;font-weight:normal;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;text-decoration:none">Gallery</a></h1>
 <div class="card" id="setupCard" style="display:none"><h2>Set up the tablet</h2>
  <div id="setupSteps"></div>
  <div id="setupForm">
   <label>Tablet root password (used once to install a key; not saved)</label>
   <input id="setupPw" type="password" autocomplete="off">
   <div class="meta" id="setupHelp"></div>
   <label style="display:flex;gap:6px;align-items:center;color:var(--fg)"><input type="checkbox" id="setupBacked" style="width:auto"> I've backed up this password somewhere off the tablet</label>
   <button id="setupBtn" onclick="runSetup()">Set up tablet</button>
  </div>
  <pre id="setupLog" style="white-space:pre-wrap;display:none"></pre>
  <div class="meta"><a href="#" onclick="hideSetup();return false">Skip for now</a></div>
 </div>
 <div class="card"><h2>Connection</h2><div id="conn">…</div><div class="meta"><a href="#" onclick="showSetup();return false">Set up tablet…</a></div></div>
 <div class="card"><h2>Usage</h2><div id="usage">…</div></div>
 <div class="card"><h2>Config</h2>
  <label style="display:flex;gap:6px;align-items:center;color:var(--fg)"><input type="checkbox" id="use_claude" style="width:auto" onchange="showThinking()"> Use Claude (through Claude Code) instead of the local model</label>
  <div id="claudeOpts"><label>Claude model</label><select id="claude_model" onchange="showThinking()"></select>
  <label style="display:flex;gap:6px;align-items:center;color:var(--fg)"><input type="checkbox" id="free_diagrams" style="width:auto"> Sonnet and up (or Haiku with thinking on) draw their own diagrams for ? (instead of the templates)</label></div>
  <div class="meta">Every button then sends the selection image and its prompt to Anthropic, signed in as your Claude Code account (it counts against your plan's limits, shown under Usage). If a Claude call fails, the local model below answers instead when it's loaded.</div>
  <label style="display:flex;gap:6px;align-items:center;color:var(--fg)"><input type="checkbox" id="thinking" style="width:auto"> Let the model think before answering</label>
  <div class="meta" id="thinkNote"></div>
  <label style="display:flex;gap:6px;align-items:center;color:var(--fg)"><input type="checkbox" id="color" style="width:auto"> Color: keep ink colors from the tablet and send color back</label>
  <div class="meta">Colored ink is kept in what the model sees; ∑ output, TikZ drawings, graphs and charts come back in the Paper Pro's ink colors. Off: everything is black and white.</div>
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
  <label><input type="checkbox" id="tablet_exact_selection" style="width:auto"> Send only the selected strokes (other ink inside the selection box is left out; the box flashes white for a moment)</label>
  <label>Padding around the selection (px)</label><input id="tablet_selection_padding" type="number" min="0" max="60">
  <label>Hide the = result after (seconds, 0 = only when tapped)</label><input id="tablet_popup_seconds" type="number" min="0" max="600">
  <label>Wait before grabbing the screen (ms)</label><input id="tablet_grab_delay_ms" type="number" min="200" max="3000" step="50">
  <div class="meta">Sent to the tablet within a few seconds; no restart needed.</div>
  <button onclick="save()">Save</button>
 </div>
 <div class="card"><h2>Rendered LaTeX</h2>
  <label>Resolution: <span id="dpiVal"></span> dpi</label>
  <input id="render_dpi" type="range" min="120" max="480" step="2" oninput="dpiLabel();previewSoon()">
  <label>Font</label><select id="render_font" onchange="previewSoon()"></select>
  <div style="background:#fff;border-radius:6px;padding:8px;margin-top:10px;overflow:auto;max-height:260px"><img id="preview" alt="preview" style="display:block"></div>
  <div class="meta">The tablet shows one image pixel per screen pixel, so a higher resolution also makes the result bigger on the page. 226 dpi matches the screen (11 pt text at its real size). Also used for graphs, diagrams and Solve steps; the font is for LaTeX only.</div>
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
const TB_BOOL=['tablet_replace_snip','tablet_replace_graph','tablet_exact_selection'], TB_NUM=['tablet_selection_padding','tablet_popup_seconds','tablet_grab_delay_ms'];
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const dot=ok=>`<span class="dot" style="background:var(${ok?'--ok':'--bad'})"></span>`;
async function tick(){
 const s=await (await fetch('/api/status')).json();
 const seen=Object.entries(s.tablet_seen);
 document.getElementById('conn').innerHTML=
  `<div>${dot(s.model.state==='ready')}Model ${esc(s.model.state)}${s.model.backend==='claude'?' · Claude '+esc(s.model.model)+(s.model.thinking?' (thinking)':''):''}${s.model.backend==='builtin'&&s.model.model?' · '+esc(s.model.model.split('/').pop()):''}${s.model.load_seconds?' · loaded in '+s.model.load_seconds+'s':''}</div>
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
  document.getElementById('use_claude').checked=c.use_claude;
  document.getElementById('thinking').checked=savedThinking=c.thinking;
  document.getElementById('color').checked=c.color;
  document.getElementById('free_diagrams').checked=c.free_diagrams;
  const cm=document.getElementById('claude_model'), cms=Object.assign({},s.claude_models);
  if(!(c.claude_model in cms))cms[c.claude_model]=c.claude_model;
  cm.innerHTML=Object.entries(cms).map(([k,n])=>`<option value="${esc(k)}">${esc(n)}</option>`).join('');
  cm.value=c.claude_model; showThinking();
  for(const k of TB_BOOL)document.getElementById(k).checked=c[k];
  for(const k of TB_NUM)document.getElementById(k).value=c[k];
  const rf=document.getElementById('render_font');
  rf.innerHTML=Object.entries(s.fonts).map(([k,n])=>`<option value="${k}">${esc(n)}</option>`).join('');
  rf.value=c.render_font;document.getElementById('render_dpi').value=c.render_dpi;dpiLabel();preview();
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
  <img src="/history/${r.dir}/input.png">${r.rendered?`<img src="/history/${r.dir}/output.png">`:`<div class="meta">${esc(r.result?'answered':(r.render_error||r.error||''))}</div>`}
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
const num=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
const until=t=>{const m=Math.max(0,Math.round((t*1000-Date.now())/60000));return m>=1440?Math.round(m/1440)+' d':m>=60?Math.floor(m/60)+' h '+(m%60)+' min':m+' min';};
async function usageTick(){try{
 const u=await (await fetch('/api/usage')).json(), el=document.getElementById('usage');
 const row=(name,o)=>`<tr><td>${name}</td><td>${o.calls}${o.failed?` <span style="color:var(--bad)">(${o.failed} failed)</span>`:''}</td><td>${o.claude_calls} / ${o.local_calls}</td><td>${num(o.input)} / ${num(o.output)}</td><td>$${o.cost.toFixed(3)}</td><td>${o.avg_seconds}s</td></tr>`;
 let t=`<table style="width:100%;font-size:13px;border-collapse:collapse;text-align:left"><tr style="color:var(--mut)"><th>&nbsp;</th><th>calls</th><th>Claude / local</th><th>tokens in / out</th><th>cost</th><th>avg</th></tr>
  ${row('Today',u.today)}${row('7 days',u.week)}${row('All time',u.all)}</table>
  <div class="meta">Cost is at API list prices; on a Claude subscription it counts toward your plan's limits instead of being billed. Local model calls are free.</div>`;
 const w=(u.limits&&u.limits.unifiedWindows)||{};
 const bar=(name,x)=>x?`<div style="margin-top:8px">${name}: ${Math.round(x.utilization*100)}% used · resets in ${until(x.resetsAt)}
   <div style="height:6px;background:var(--line);border-radius:3px;margin-top:3px"><div style="height:6px;width:${Math.min(100,x.utilization*100)}%;background:var(${x.utilization>0.85?'--bad':'--ok'});border-radius:3px"></div></div></div>`:'';
 if(w.five_hour||w.seven_day)t+=`<div style="margin-top:6px"><b>Claude plan</b>${bar('5-hour window',w.five_hour)}${bar('7-day window',w.seven_day)}
   <div class="meta">as of the last Claude call, ${Math.round((Date.now()/1000-u.limits.seen)/60)} min ago${u.limits.status&&u.limits.status!=='allowed'?' · <span style="color:var(--bad)">'+esc(u.limits.status)+'</span>':''}</div></div>`;
 if(u.last)t+=`<div class="meta">last call: ${esc(u.last.backend)}${u.last.model?' '+esc(u.last.model):''}, ${u.last.seconds}s${u.last.error?' · <span style="color:var(--bad)">'+esc(u.last.error)+'</span>':''}</div>`;
 el.innerHTML=t;}catch(e){}}
usageTick();setInterval(usageTick,5000);
async function toggleLid(){const b=document.getElementById('lidBtn');b.disabled=true;b.textContent='…';
 try{await fetch('/api/lid',{method:'POST',body:JSON.stringify({awake:!b.dataset.awake})});}finally{b.disabled=false;tick();}}
let savedThinking=false;
function showThinking(){const on=document.getElementById('use_claude').checked,
  opus=on&&/opus/i.test(document.getElementById('claude_model').value), t=document.getElementById('thinking');
 document.getElementById('claudeOpts').style.display=on?'':'none';
 if(opus&&!t.disabled)savedThinking=t.checked;
 if(!opus&&t.disabled)t.checked=savedThinking;
 t.disabled=opus; if(opus)t.checked=true;
 document.getElementById('thinkNote').textContent=opus?'Opus always thinks; it can\'t be switched off.':
  on?'Slower (Haiku ~2 s → ~6 s per call) but more careful.':
  'Built-in Gemma only: slower, a bit more careful. Not used for the steps that need JSON (graphs, diagrams, =).';}
function showBackend(){const b=document.getElementById('backend').value;
 document.getElementById('builtinOpts').style.display=b==='builtin'?'':'none';
 document.getElementById('lmOpts').style.display=b==='lmstudio'?'':'none';}
async function quitApp(){if(confirm('Quit snip2tex? The tablet buttons stop working until you start it again.')){await fetch('/api/quit',{method:'POST'});document.body.innerHTML='<p style="padding:24px">snip2tex stopped.</p>';}}
async function save(){
 const body={};for(const k of ['lmstudio_url','model','prompt','ask_prompt','append_tex_file','backend','model_path'])body[k]=document.getElementById(k).value;
 body.copy_to_clipboard=document.getElementById('copy_to_clipboard').checked;
 body.auto_start_xovi=document.getElementById('auto_start_xovi').checked;
 body.use_claude=document.getElementById('use_claude').checked;
 body.claude_model=document.getElementById('claude_model').value;
 // Opus forces the box on; keep the saved choice for the other models
 const t=document.getElementById('thinking'); body.thinking=t.disabled?savedThinking:t.checked; savedThinking=body.thinking;
 body.color=document.getElementById('color').checked;
 body.free_diagrams=document.getElementById('free_diagrams').checked;
 for(const k of TB_BOOL)body[k]=document.getElementById(k).checked;
 for(const k of TB_NUM)body[k]=parseInt(document.getElementById(k).value||'0',10);
 body.render_dpi=parseInt(document.getElementById('render_dpi').value,10);
 body.render_font=document.getElementById('render_font').value;
 await fetch('/api/config',{method:'POST',body:JSON.stringify(body)});tick();
}
function dpiLabel(){const v=+document.getElementById('render_dpi').value;
 document.getElementById('dpiVal').textContent=`${v} (${(v/226).toFixed(2)}× screen)`;}
let previewTimer=null;
function previewSoon(){clearTimeout(previewTimer);previewTimer=setTimeout(preview,350);}
function preview(){const dpi=document.getElementById('render_dpi').value, font=document.getElementById('render_font').value;
 const img=document.getElementById('preview');img.src=`/api/preview?dpi=${dpi}&font=${encodeURIComponent(font)}&t=${Date.now()}`;}
async function test(){const f=document.getElementById('f').files[0];if(f){fetch('/api/test',{method:'POST',body:f});setTimeout(tick,300);}}
tick();setInterval(tick,2000);
let setupHidden=false, setupForced=false;
try{setupHidden=localStorage.getItem('setupSkipped')==='1';}catch(e){}
function hideSetup(){setupHidden=true;setupForced=false;try{localStorage.setItem('setupSkipped','1');}catch(e){}document.getElementById('setupCard').style.display='none';}
function showSetup(){setupForced=true;setupTick();}
async function setupTick(){try{
 const st=await (await fetch('/api/setup')).json(), card=document.getElementById('setupCard');
 const show=setupForced||st.running||(st.needed&&!setupHidden&&!st.ok);
 card.style.display=show?'':'none'; if(!show)return;
 document.getElementById('setupHelp').textContent=st.help;
 document.getElementById('setupSteps').innerHTML=`<div>${dot(st.reachable)}${st.reachable?'Tablet connected over USB':'Plug the tablet in over USB (developer mode on, xovi installed)'}</div>`;
 const log=document.getElementById('setupLog');
 const lines=st.log.slice(); if(st.running)lines.push('working…'); if(st.error)lines.push('failed: '+st.error); if(st.ok)lines.push('Tablet set up.');
 log.style.display=lines.length?'':'none'; log.textContent=lines.join('\n');
 document.getElementById('setupBtn').disabled=st.running||!st.reachable;
 document.getElementById('setupForm').style.display=st.ok?'none':'';
 if(st.ok){setupForced=false;}
}catch(e){}}
async function runSetup(){
 if(!document.getElementById('setupBacked').checked){alert('Back up the password first: you need it to get back into the tablet, and it changes after a factory reset.');return;}
 const pw=document.getElementById('setupPw'); await fetch('/api/setup',{method:'POST',body:JSON.stringify({password:pw.value})});
 pw.value=''; setupTick();}
setupTick();setInterval(setupTick,2000);
</script></body></html>"""


def start(open_browser=True):
    """Load the model and serve in a background thread; returns the server."""
    port = config["port"]
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    # Wi-Fi mode's listener: localhost only, Tailscale Funnel forwards to it
    remote_srv = ThreadingHTTPServer(("127.0.0.1", config["remote_port"]), RemoteH)
    threading.Thread(target=remote_srv.serve_forever, daemon=True).start()
    try:
        migrate_snips()
    except Exception as e:
        print("moving old snips failed:", e, flush=True)
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
