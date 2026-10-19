#!/usr/bin/env python3
"""Install the snip2tex tablet side over USB (macOS, Windows, Linux).

Needs: a reMarkable Paper Pro in developer mode, plugged in over USB, with
xovi installed in /home/root/xovi (https://github.com/asivery/rm-xovi-extensions)
including qt-resource-rebuilder, qt-command-executor, framebuffer-spy and
xovi-message-broker. The tablet's root password (Settings > General > Help >
Copyrights and licenses) is used once, to install an SSH key; it isn't saved.

The app's first-launch setup in the dashboard calls install(); from a terminal:
    python install_tablet.py [--host 10.11.99.1] [--port 8799]
"""
import argparse, getpass, json, socket, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = Path.home() / ".snip2tex" / "ssh" / "id_ed25519"
XOVI = "/home/root/xovi"
EXTENSIONS = ["qt-resource-rebuilder", "qt-command-executor", "framebuffer-spy", "xovi-message-broker"]
PASSWORD_HELP = ("The tablet's root password is under Settings > General > Help > Copyrights and licenses. "
                 "Back it up somewhere off the tablet: you need it to get back in over SSH, "
                 "and it changes after a factory reset.")


class SetupError(Exception):
    pass


def ssh_opts():
    return ["-i", str(KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]


def usb_ip(host):
    """This computer's address on the tablet's USB network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return s.getsockname()[0]


def reachable(host):
    try:
        with socket.create_connection((host, 22), timeout=2):
            return True
    except OSError:
        return False


def key_works(host):
    if not KEY.exists():
        return False
    r = subprocess.run(["ssh", *ssh_opts(), f"root@{host}", "true"], capture_output=True, timeout=15)
    return r.returncode == 0


def make_key():
    if not KEY.exists():
        KEY.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "snip2tex", "-f", str(KEY)],
                       check=True, capture_output=True)
    return KEY.with_suffix(".pub").read_text().strip()


def install_key(host, password, log):
    """Log in once with the password (paramiko) and add our public key."""
    import paramiko
    pub = make_key()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username="root", password=password, timeout=10,
                       look_for_keys=False, allow_agent=False)
    except paramiko.AuthenticationException:
        raise SetupError("wrong password. " + PASSWORD_HELP)
    except Exception as e:
        raise SetupError(f"couldn't connect to the tablet at {host}: {e}")
    try:
        cmd = (f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
               f"(grep -qxF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> ~/.ssh/authorized_keys) && "
               f"chmod 600 ~/.ssh/authorized_keys")
        _, out, err = client.exec_command(cmd)
        if out.channel.recv_exit_status() != 0:
            raise SetupError("couldn't add the key: " + err.read().decode(errors="replace"))
    finally:
        client.close()
    log("key installed")


def install(host="10.11.99.1", port=8799, password=None, log=print):
    """Set the tablet up. password: only needed when our key isn't installed
    yet (None in a terminal asks for it). Raises SetupError."""
    target = f"root@{host}"
    if not reachable(host):
        raise SetupError(f"no tablet at {host}: plug it in over USB and turn on developer mode")
    log("tablet found")
    if not key_works(host):
        if password is None:
            print(PASSWORD_HELP)
            password = getpass.getpass("tablet root password: ")
        if not password:
            raise SetupError("the tablet's root password is needed once. " + PASSWORD_HELP)
        install_key(host, password, log)
        if not key_works(host):
            raise SetupError("the key was added but logging in with it failed")
    else:
        log("key already installed")

    def sh(cmd, stdin=None):
        r = subprocess.run(["ssh", *ssh_opts(), target, cmd], capture_output=True, text=True,
                           input=stdin, timeout=60)
        if r.returncode != 0:
            raise SetupError(f"tablet command failed: {cmd[:60]}: {(r.stderr or r.stdout).strip()[-200:]}")
        return r.stdout

    if "yes" not in sh(f"[ -f {XOVI}/start ] && echo yes || echo no"):
        raise SetupError(f"xovi isn't installed in {XOVI}. Install it first: "
                         "https://github.com/asivery/rm-xovi-extensions")
    for ext in EXTENSIONS:
        sh(f"[ -f {XOVI}/extensions.d/{ext}.so ] || mv {XOVI}/inactive-extensions/{ext}.so {XOVI}/extensions.d/ 2>/dev/null; true")
    missing = sh(" ".join(f"[ -f {XOVI}/extensions.d/{e}.so ] || echo {e};" for e in EXTENSIONS)).split()
    if missing:
        raise SetupError(f"missing xovi extensions: {', '.join(missing)}")
    log("xovi extensions ready")

    sh(f"mkdir -p {XOVI}/exthome/qt-resource-rebuilder {XOVI}/exthome/snip2tex /home/root/.snip2tex")
    t = HERE / "tablet"
    for src, dst in ((t / "snip2tex.qmd", f"{XOVI}/exthome/qt-resource-rebuilder/snip2tex.qmd"),
                     (t / "snip2tex.rcc", f"{XOVI}/exthome/qt-resource-rebuilder/snip2tex.rcc"),
                     (t / "snip.sh", f"{XOVI}/exthome/snip2tex/snip.sh")):
        r = subprocess.run(["scp", *ssh_opts(), str(src), f"{target}:{dst}"], capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            raise SetupError(f"copying {src.name} failed: {r.stderr.strip()[-200:]}")
    server = f"http://{usb_ip(host)}:{port}"
    sh(f"chmod +x {XOVI}/exthome/snip2tex/snip.sh && echo {server} > {XOVI}/exthome/snip2tex/server")
    sh(f"[ -f {XOVI}/exthome/snip2tex/config.json ] || cat > {XOVI}/exthome/snip2tex/config.json",
       stdin=json.dumps({"replace_snip": True, "replace_graph": False, "selection_padding": 5,
                         "popup_seconds": 30, "grab_delay_ms": 700}, sort_keys=True))
    log("patch copied")

    # load the patch: restart xochitl under xovi
    sh(f"systemctl reset-failed xochitl; cd /home/root && (nohup sh {XOVI}/start >/dev/null 2>&1 &)")
    log(f"done: the tablet restarts its interface and talks to {server}")
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.11.99.1")
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    try:
        install(a.host, a.port)
    except SetupError as e:
        sys.exit(f"setup failed: {e}")
