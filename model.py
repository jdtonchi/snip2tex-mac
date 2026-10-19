"""Vision model backends for snip2tex.

"builtin" runs an MLX model in-process with mlx-vlm (no LM Studio needed);
"lmstudio" talks to LM Studio's OpenAI-compatible API. Both expose
chat(prompt, image_png=None, schema=None) -> str.
"""
import base64, json, os, tempfile, threading, time, urllib.request
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
            try:
                formatted = apply_chat_template(self.processor, self.model.config, prompt,
                                                num_images=len(images or []), enable_thinking=False)
                kwargs = {}
                if schema is not None:
                    from mlx_vlm.structured import build_json_schema_logits_processor
                    tok = getattr(self.processor, "tokenizer", self.processor)
                    kwargs["logits_processors"] = [build_json_schema_logits_processor(tok, schema)]
                r = generate(self.model, self.processor, formatted, image=images,
                             max_tokens=max_tokens, temperature=temperature,
                             enable_thinking=False, verbose=False, **kwargs)
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
