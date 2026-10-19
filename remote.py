"""Wi-Fi mode: the tablet reaches this Mac through Tailscale Funnel when the
USB cable isn't plugged in (campus Wi-Fi blocks device-to-device traffic, but
the tablet can reach the internet).

Funnel publishes a port to the internet at https://<mac>.<tailnet>.ts.net, so
it points at a separate listener (config `remote_port`) that serves only the
tablet's button endpoints and needs the shared token (config `remote_token`,
pushed to the tablet over USB by tablet.py). The dashboard and settings never
go through it: Funnel traffic arrives from localhost, which the main server
would otherwise trust.

Needs the Tailscale app (tailscale.com), signed in. Turning Funnel on the
first time may print a link to approve it for the tailnet; that's shown in
the dashboard.
"""
import json, os, secrets, shutil, subprocess

CLI_PATHS = ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
             "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"]

note = ""  # last message worth showing (e.g. Funnel's approval link)


def cli():
    for p in CLI_PATHS:
        if os.path.exists(p):
            return p
    return shutil.which("tailscale")


def _run(*args, timeout=20):
    exe = cli()
    if not exe:
        raise RuntimeError("Tailscale isn't installed (get it from tailscale.com)")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def new_token():
    return secrets.token_urlsafe(24)


def status(port):
    """{installed, running, dns, url, funnel} for the dashboard."""
    out = {"installed": bool(cli()), "running": False, "dns": "", "url": "", "funnel": False, "note": note}
    if not out["installed"]:
        return out
    try:
        st = json.loads(_run("status", "--json", timeout=5).stdout or "{}")
        out["running"] = st.get("BackendState") == "Running"
        out["dns"] = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
        if out["dns"]:
            out["url"] = "https://" + out["dns"]
        fs = _run("funnel", "status", "--json", timeout=5).stdout
        out["funnel"] = f"127.0.0.1:{port}" in fs or f"localhost:{port}" in fs
    except Exception as e:
        out["note"] = f"tailscale: {e}"
    return out


def set_funnel(port, on):
    """Turn Funnel to the remote listener on/off. Returns (ok, message)."""
    global note
    try:
        r = _run("funnel", "--bg", str(port)) if on else _run("funnel", "--https=443", "off")
    except subprocess.TimeoutExpired:
        # first use: it waits for the tailnet admin to approve Funnel
        note = "Tailscale is waiting for Funnel to be approved; see the link in the Tailscale app or admin console"
        return False, note
    except Exception as e:
        note = str(e)
        return False, note
    text = (r.stdout + r.stderr).strip()
    note = "" if r.returncode == 0 else text[-400:]
    return r.returncode == 0, text
