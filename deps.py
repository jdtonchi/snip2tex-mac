"""Fetch what snip2tex needs on first run, so a fresh Mac only needs this folder:
the tectonic LaTeX engine (standalone binary) and the MLX vision model."""
import os, shutil, tarfile, tempfile, threading, urllib.request
from pathlib import Path

BIN = Path.home() / ".snip2tex" / "bin"
TECTONIC_URL = ("https://github.com/tectonic-typesetting/tectonic/releases/download/"
                "tectonic%400.17.0/tectonic-0.17.0-aarch64-apple-darwin.tar.gz")
MODEL_REPO = "lmstudio-community/gemma-4-E2B-it-MLX-4bit"

# what the dashboard and menu show while something is being fetched
progress = {}
_lock = threading.Lock()


def _set(key, text):
    with _lock:
        if text is None:
            progress.pop(key, None)
        else:
            progress[key] = text


def find_tectonic():
    for p in (shutil.which("tectonic"), "/opt/homebrew/bin/tectonic", "/usr/local/bin/tectonic", BIN / "tectonic"):
        if p and os.access(p, os.X_OK):
            return str(p)
    return None


def ensure_tectonic():
    """Return a tectonic path, downloading the official binary if there is none."""
    found = find_tectonic()
    if found:
        return found
    _set("tectonic", "downloading LaTeX engine…")
    try:
        BIN.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as d:
            tgz = Path(d) / "t.tar.gz"
            urllib.request.urlretrieve(TECTONIC_URL, tgz)
            with tarfile.open(tgz) as t:
                member = next(m for m in t.getmembers() if Path(m.name).name == "tectonic")
                member.name = "tectonic"
                t.extract(member, d, filter="data")
            shutil.move(str(Path(d) / "tectonic"), BIN / "tectonic")
        os.chmod(BIN / "tectonic", 0o755)
        return str(BIN / "tectonic")
    finally:
        _set("tectonic", None)


def model_present(path):
    p = Path(path)
    return (p / "config.json").exists() and any(p.glob("*.safetensors"))


def ensure_model(path, repo=MODEL_REPO):
    """Download the model into `path` (LM Studio's layout) if it isn't there."""
    if model_present(path):
        return path
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import tqdm as hf_tqdm

    class Progress(hf_tqdm):
        def update(self, n=1):
            super().update(n)
            # byte bars report size; the per-file bar doesn't
            if self.total and getattr(self, "unit", "") == "B" and self.total > 1e6:
                _set("model", f"downloading model… {self.n / self.total:.0%} of {self.total / 1e9:.1f} GB")
            elif self.total:
                _set("model", f"downloading model… {self.n}/{self.total} files (4 GB total)")

    _set("model", "downloading model…")
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        snapshot_download(repo, local_dir=path, tqdm_class=Progress)
        return path
    finally:
        _set("model", None)
