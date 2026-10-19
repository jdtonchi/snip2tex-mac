"""The ✦ menu's actions.

Solve steps: Gemma reads the problem and writes a worked solution as a spec
(steps of LaTeX + a few words on why); it's typeset with the ∑ button's LaTeX
renderer and inserted below the selection, the final answer goes in the popup.

Check work: Gemma reads the student's own working and names the first wrong
line (or says it's right); one line of plain text for the popup.
"""
import json, re

from diagrams import plain

SOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "problem": {"type": "string"},
        "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
            "type": "object",
            "properties": {"why": {"type": "string", "maxLength": 80}, "math": {"type": "string"}},
            "required": ["why", "math"], "additionalProperties": False}},
        "answer": {"type": "string"},
    },
    "required": ["problem", "steps", "answer"],
    "additionalProperties": False,
}

SOLVE_PROMPT = """This is a problem from a student's handwritten notes. Solve it step by step,
the way a good tutor would write it on the board.
- problem: the problem in LaTeX (no $ signs).
- steps: each step is one line of maths in LaTeX (no $ signs, no \\[ \\]; use
  \\Rightarrow or = to chain, \\frac, ^, \\sqrt, \\int etc.) and "why": a few plain
  words saying what was done ("subtract 3 from both sides", "chain rule").
  Small, clear steps; no skipped algebra. At most 12 steps.
- answer: the final answer in LaTeX, short (x = 2, or 12\\,\\text{m/s}).
Be correct: double-check the arithmetic before you answer."""

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "verdict": {"type": "string", "enum": ["correct", "mistake", "unsure"]},
        "line": {"type": ["integer", "null"]},
        "issue": {"type": "string", "maxLength": 120},
        "fix": {"type": "string", "maxLength": 120},
    },
    "required": ["lines", "verdict", "line", "issue", "fix"],
    "additionalProperties": False,
}

CHECK_PROMPT = """This is a student's own handwritten working for a problem. Check it like a strict tutor.
- lines: copy each line of their working, in order, as written (plain text or LaTeX).
- Go through the lines one at a time and check that each follows from the one before.
- verdict: "correct" if every line is right; "mistake" at the FIRST line that's wrong;
  "unsure" if you can't read it or it isn't working you can check.
- line: the number (1 = first line) of the first wrong line, else null.
- issue: what is wrong there, in under 15 plain words ("dropped the minus sign").
- fix: what that line should be (plain text, short). Empty when correct.
Don't solve the whole thing for them; only point at the first mistake."""

LATEX_SPECIALS = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                  "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex_text(s):
    return "".join(LATEX_SPECIALS.get(c, c) for c in plain(s))


def bare_math(s):
    """Model math sometimes comes wrapped in $...$ or \\[...\\] anyway."""
    s = (s or "").strip()
    for a, b in (("\\[", "\\]"), ("$$", "$$"), ("$", "$"), ("\\(", "\\)")):
        if s.startswith(a) and s.endswith(b):
            s = s[len(a):-len(b)].strip()
    return s


def _json(raw):
    return json.loads(re.sub(r"^```(json)?|```$", "", raw.strip()))


def solution_latex(spec):
    """The LaTeX body for the ∑ renderer: each step's reason in small italics,
    its maths displayed under it, the answer boxed at the end."""
    parts = []
    for i, st in enumerate(spec["steps"][:12], 1):
        why = tex_text(st.get("why", ""))
        if why:
            parts.append(r"\noindent{\small\textit{%d. %s}}" % (i, why))
        parts.append(r"\[ %s \]" % bare_math(st["math"]))
    parts.append(r"\[ \boxed{%s} \]" % bare_math(spec["answer"]))
    return "\n".join(parts)


def solve(chat, png, render_png):
    """Returns (popup text, PNG of the worked steps)."""
    spec = _json(chat(SOLVE_PROMPT, image_png=png, schema=SOLVE_SCHEMA, temperature=0.0, max_tokens=1600))
    if not spec.get("steps"):
        raise ValueError("no steps")
    try:
        out = render_png(solution_latex(spec))
    except RuntimeError:
        # one bad step shouldn't sink the rest: drop steps until it typesets
        good = []
        for st in spec["steps"]:
            try:
                render_png(r"\[ %s \]" % bare_math(st["math"]))
                good.append(st)
            except RuntimeError:
                pass
        out = render_png(solution_latex(dict(spec, steps=good or spec["steps"][:0])))
    return "answer: " + plain(bare_math(spec["answer"])), out


def check(chat, png):
    """Returns one line of plain text for the popup."""
    r = _json(chat(CHECK_PROMPT, image_png=png, schema=CHECK_SCHEMA, temperature=0.0, max_tokens=900))
    if r["verdict"] == "correct":
        return "✓ looks right"
    if r["verdict"] == "unsure" or not r.get("issue"):
        return "can't check this one (is it worked steps?)"
    n = r.get("line")
    lines = r.get("lines") or []
    where = f"line {n}" if n else "a line"
    if n and 1 <= n <= len(lines):
        where += f" ({plain(lines[n - 1])[:40]})"
    text = f"✗ {where}: {plain(r['issue'])}"
    if r.get("fix"):
        text += f" → {plain(r['fix'])}"
    return text
