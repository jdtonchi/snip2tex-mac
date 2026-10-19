#!/usr/bin/env python3
"""Install the snip2tex tablet side over USB (macOS, Windows, Linux).

Needs: a reMarkable Paper Pro in developer mode, plugged in over USB, with
xovi installed in /home/root/xovi (https://github.com/asivery/rm-xovi-extensions)
including qt-resource-rebuilder, qt-command-executor, framebuffer-spy and
xovi-message-broker. Uses the system ssh/scp; asks for the tablet's root
password once (Settings > Help > Copyrights and licenses) to install a key.

Usage: python install_tablet.py [--host 10.11.99.1] [--port 8799]
"""
import argparse, json, socket, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = Path.home() / ".snip2tex" / "ssh" / "id_ed25519"
XOVI = "/home/root/xovi"
EXTENSIONS = ["qt-resource-rebuilder", "qt-command-executor", "framebuffer-spy", "xovi-message-broker"]


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def ssh_opts():
    return ["-i", str(KEY), "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new"]


def usb_ip(host):
    """This computer's address on the tablet's USB network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return s.getsockname()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.11.99.1")
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    target = f"root@{a.host}"

    if not KEY.exists():
        KEY.parent.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "snip2tex", "-f", str(KEY)])
    pub = KEY.with_suffix(".pub").read_text().strip()
    print("The tablet's root password is under Settings > General > Help > Copyrights and licenses.\n"
          "Back it up somewhere off the tablet before continuing: you need it to get back in over SSH,\n"
          "and it changes after a factory reset.", flush=True)
    # password prompt (once): install the key
    run(["ssh", "-o", "StrictHostKeyChecking=accept-new", target,
         f"mkdir -p ~/.ssh && grep -qxF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> ~/.ssh/authorized_keys"])

    def sh(cmd):
        return run(["ssh", *ssh_opts(), target, cmd], capture_output=True, text=True).stdout

    if "yes" not in sh(f"[ -f {XOVI}/start ] && echo yes || echo no"):
        sys.exit(f"xovi isn't installed in {XOVI}; install it first (see README)")
    # enable the extensions snip2tex uses
    for ext in EXTENSIONS:
        sh(f"[ -f {XOVI}/extensions.d/{ext}.so ] || mv {XOVI}/inactive-extensions/{ext}.so {XOVI}/extensions.d/ 2>/dev/null; true")
    missing = sh(" ".join(f"[ -f {XOVI}/extensions.d/{e}.so ] || echo {e};" for e in EXTENSIONS)).split()
    if missing:
        sys.exit(f"missing xovi extensions: {', '.join(missing)}")

    sh(f"mkdir -p {XOVI}/exthome/qt-resource-rebuilder {XOVI}/exthome/snip2tex /home/root/.snip2tex")
    t = HERE / "tablet"
    run(["scp", *ssh_opts(), str(t / "snip2tex.qmd"), str(t / "snip2tex.rcc"),
         f"{target}:{XOVI}/exthome/qt-resource-rebuilder/"])
    run(["scp", *ssh_opts(), str(t / "snip.sh"), f"{target}:{XOVI}/exthome/snip2tex/snip.sh"])
    server = f"http://{usb_ip(a.host)}:{a.port}"
    sh(f"chmod +x {XOVI}/exthome/snip2tex/snip.sh && echo {server} > {XOVI}/exthome/snip2tex/server")
    cfg = json.dumps({"replace_snip": True, "replace_graph": False, "selection_padding": 5,
                      "popup_seconds": 30, "grab_delay_ms": 700}, sort_keys=True)
    run(["ssh", *ssh_opts(), target, f"cat > {XOVI}/exthome/snip2tex/config.json"], input=cfg, text=True)

    # load the patch: restart xochitl under xovi
    sh(f"systemctl reset-failed xochitl; cd /home/root && (nohup sh {XOVI}/start >/dev/null 2>&1 &)")
    print(f"done. tablet -> {server}. Start the snip2tex app on this computer.")


if __name__ == "__main__":
    main()
