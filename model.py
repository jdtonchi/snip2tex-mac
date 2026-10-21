"""Vision model backends for snip2tex.

"builtin" runs an MLX model in-process with mlx-vlm (no LM Studio needed);
"lmstudio" talks to LM Studio's OpenAI-compatible API; ClaudeCodeModel runs a
Claude model through the Claude Code CLI (the user's Claude login). All expose
chat(prompt, image_png=None, schema=None) -> str.
"""
import base64, json, os, subprocess, tempfile, threading, time, urllib.request
from pathlib import Path

DEFAULT_MODELS_DIR = Path.home() / ".lmstudio" / "models"


def strip_thinking(s):
    # Gemma 4 thinking leaks as "<|channel>thought ... <channel|>answer"
    if "<channel|>" in s:
        s = s.rsplit("<channel|>", 1)[1]
    return s.strip()


def find_models(models_dir=DEFAULT_MODELS_DIR):
    """MLX vision models under an LM Studio models folder (publisher/name/)."""
    found = []
    root = Path(models_dir).expanduser()
    for cfg in sorted(root.glob("*/*/config.json")):
        try:
            c = json.loads(cfg.read_text())
        except (OSError, ValueError):
            continue
        is_mlx = "quantization" in c or "MLX" in cfg.parent.name or "mlx" in cfg.parent.parent.name
        has_vision = "vision_config" in c or (cfg.parent / "processor_config.json").exists() \
            or (cfg.parent / "preprocessor_config.json").exists()
        if is_mlx and has_vision:
            found.append(str(cfg.parent))
    return found


class BuiltinModel:
    """mlx-vlm model kept loaded in this process. MLX isn't thread-safe, so
    every call goes through one lock."""

    def __init__(self):
        self.path = None
        self.model = self.processor = None
        self.state = "not loaded"   # not loaded | loading | ready | error
        self.error = ""
        self.load_seconds = None
        self.thinking = False  # Gemma thinks before answering: slower, a bit more careful
        self._lock = threading.Lock()

    def load(self, path):
        with self._lock:
            self.state, self.error, self.path = "loading", "", path
            t0 = time.time()
            try:
                from mlx_vlm import load
                self.model, self.processor = load(path)
                self.load_seconds = round(time.time() - t0, 1)
                self.state = "ready"
            except Exception as e:
                self.model = self.processor = None
                self.state, self.error = "error", f"{type(e).__name__}: {e}"

    def unload(self):
        """Drop the weights and hand MLX's cached GPU memory back to macOS."""
        with self._lock:
            if self.model is None and self.state != "loading":
                self.state = "not loaded"
                return
            self.model = self.processor = None
            self.state, self.error, self.load_seconds = "not loaded", "", None
        import gc
        gc.collect()
        try:
            import mlx.core as mx
            (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()
        except Exception:
            pass

    def load_async(self, path):
        threading.Thread(target=self.load, args=(path,), daemon=True).start()

    def warm_up(self):
        """First generation compiles kernels (~3s); do it before the first snip."""
        try:
            self.chat("Say ok.", max_tokens=4)
        except Exception:
            pass

    def chat(self, prompt, image_png=None, schema=None, temperature=0.0, max_tokens=800):
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
        if self.state != "ready":
            raise RuntimeError(f"model is {self.state}" + (f": {self.error}" if self.error else ""))
        with self._lock:
            images, tmp = None, None
            if image_png is not None:
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.write(image_png)
                tmp.close()
                images = [tmp.name]
            # the JSON-schema constraint applies from the first token, which
            # leaves no room for a thought block, so schema calls never think
            think = self.thinking and schema is None
            if think:
                max_tokens += 3000
            try:
                formatted = apply_chat_template(self.processor, self.model.config, prompt,
                                                num_images=len(images or []), enable_thinking=think)
                kwargs = {}
                if schema is not None:
                    from mlx_vlm.structured import build_json_schema_logits_processor
                    tok = getattr(self.processor, "tokenizer", self.processor)
                    kwargs["logits_processors"] = [build_json_schema_logits_processor(tok, schema)]
                r = generate(self.model, self.processor, formatted, image=images,
                             max_tokens=max_tokens, temperature=temperature,
                             enable_thinking=think, verbose=False, **kwargs)
                return strip_thinking(r.text)
            finally:
                if tmp:
                    os.unlink(tmp.name)

    def status(self):
        return {"backend": "builtin", "state": self.state, "error": self.error,
                "model": self.path, "load_seconds": self.load_seconds}


class LMStudioModel:
    def __init__(self, url, model=""):
        self.url, self.model = url, model

    def _get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=3) as r:
            return json.load(r)

    def pick(self):
        if self.model:
            return self.model
        ids = [m["id"] for m in self._get("/models")["data"] if "embed" not in m["id"]]
        if not ids:
            raise RuntimeError("LM Studio has no chat model loaded")
        return ids[0]

    def chat(self, prompt, image_png=None, schema=None, temperature=0.0, max_tokens=800):
        content = [{"type": "text", "text": prompt}]
        if image_png is not None:
            content.insert(0, {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(image_png).decode()}})
        body = {"model": self.pick(), "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content}]}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "out", "strict": True, "schema": schema}}
        req = urllib.request.Request(self.url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return strip_thinking(json.load(r)["choices"][0]["message"]["content"])

    def status(self):
        try:
            models = [m["id"] for m in self._get("/models")["data"]]
            return {"backend": "lmstudio", "state": "ready", "error": "", "model": self.model or "(auto)",
                    "lmstudio_models": models}
        except Exception as e:
            return {"backend": "lmstudio", "state": "error", "error": str(e), "model": self.model}


# Claude models offered in the dashboard (id -> label). Any other id saved in
# the config still works; it's added to the list.
CLAUDE_MODELS = {
    "claude-haiku-5-5": "Haiku 5.5 (fastest)",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-opus-5-5": "Opus 5.5 (always thinks)",
    "claude-fable-5-1": "Fable 5.1 (needs usage credits)",
}


def draws_freely(model, thinking=False):
    """Sonnet and up are good enough to design their own diagrams (TikZ)
    instead of filling in the templates in diagrams.py; Haiku is too when
    thinking is on."""
    m = (model or "").lower()
    return any(k in m for k in ("sonnet", "opus", "fable")) or (thinking and "haiku" in m)


def always_thinks(model):
    """Opus can't have thinking switched off."""
    return "opus" in (model or "").lower()


class ClaudeCodeModel:
    """A Claude model through `claude -p` (Claude Code, signed in with the user's
    Claude account, or an API key if that's how Claude Code is set up). The image
    goes in as a stream-json content block, so no tools are enabled. Thinking is
    off unless asked for (MAX_THINKING_TOKENS=0: ~2 s instead of ~6 s per call
    on Haiku); Opus always thinks. temperature and max_tokens can't be set
    through the CLI and are ignored.

    After each call, take_usage() returns that call's tokens, cost and the
    plan's rate-limit windows (per thread, so concurrent presses don't mix)."""

    SYSTEM = ("You are the model inside snip2tex, a tool for a student's handwritten notes on "
              "an e-ink tablet. Follow the instructions in the user message exactly and reply "
              "with only what they ask for.")

    def __init__(self, claude_path, model="claude-haiku-5-5", workdir=None, thinking=False):
        self.claude, self.model = claude_path, model
        self.thinking = thinking or always_thinks(model)
        self.workdir = Path(workdir or tempfile.gettempdir())
        self.local = threading.local()
        self.error = ""

    def chat(self, prompt, image_png=None, schema=None, temperature=0.0, max_tokens=800):
        content = [{"type": "text", "text": prompt}]
        if image_png is not None:
            content.insert(0, {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": base64.b64encode(image_png).decode()}})
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        cmd = [self.claude, "-p", "--model", self.model, "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--tools", "", "--system-prompt", self.SYSTEM,
               "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        self.workdir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        env = {k: v for k, v in os.environ.items() if k != "MAX_THINKING_TOKENS"}
        if not self.thinking:
            env["MAX_THINKING_TOKENS"] = "0"
        r = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True,
                           timeout=300 if self.thinking else 180, cwd=self.workdir, env=env)
        result, limits = None, {}
        for line in r.stdout.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") == "result":
                result = m
            elif m.get("type") == "rate_limit_event":
                limits = m.get("rate_limit_info") or {}
        usage = (result or {}).get("usage") or {}
        self.local.usage = {
            # the alias ("haiku") resolved to the model that actually answered
            "model": next(iter((result or {}).get("modelUsage") or {}), self.model),
            "seconds": round(time.time() - t0, 2),
            "input": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                     + usage.get("cache_creation_input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cost": (result or {}).get("total_cost_usd", 0.0), "limits": limits,
        }
        if result is None or result.get("is_error"):
            why = (result or {}).get("result") or r.stderr.strip()[-300:] or f"claude exited {r.returncode}"
            self.error = why
            raise RuntimeError(f"Claude Code: {why}")
        self.error = ""
        if schema is not None and result.get("structured_output") is not None:
            return json.dumps(result["structured_output"])
        return (result.get("result") or "").strip()

    def take_usage(self):
        u = getattr(self.local, "usage", None)
        self.local.usage = None
        return u

    def status(self):
        return {"backend": "claude", "state": "error" if self.error else "ready",
                "error": self.error, "model": self.model, "thinking": self.thinking}


class EngineModel:
    """The running Snip2TeX engine's model, over its localhost-only /api/chat.
    For scripts and tests: loading a second copy can run the Mac out of memory."""

    def __init__(self, url="http://127.0.0.1:8799"):
        self.url = url

    def chat(self, prompt, image_png=None, schema=None, temperature=0.0, max_tokens=800):
        body = {"prompt": prompt, "schema": schema, "temperature": temperature, "max_tokens": max_tokens,
                "image_b64": base64.b64encode(image_png).decode() if image_png is not None else None}
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)["reply"]
