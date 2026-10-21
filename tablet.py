"""Keeps the tablet side working without a terminal.

While the reMarkable is plugged in over USB, every few seconds:
- if xochitl is running without xovi (the tablet rebooted), start xovi again;
- keep the tablet pointed at this Mac's current USB address;
- push the button settings from the dashboard;
- note when the firmware changed, since the patch may need redoing.
Uses the key in ~/.snip2tex/ssh (installed in /home/root/.ssh/authorized_keys).
"""
import json, socket, subprocess, threading, time
from pathlib import Path

KEY = Path.home() / ".snip2tex" / "ssh" / "id_ed25519"
PATCHED_FIRMWARE = "3.27.3.0"
SERVER_FILE = "/home/root/xovi/exthome/snip2tex/server"
TABLET_CONFIG = "/home/root/xovi/exthome/snip2tex/config.json"  # read by the QML on each press
# Wi-Fi mode (remote.py): snip.sh falls back to this URL + token without USB
REMOTE_FILE = "/home/root/xovi/exthome/snip2tex/server_remote"
TOKEN_FILE = "/home/root/xovi/exthome/snip2tex/token"

# Mac config key -> tablet config key
TABLET_KEYS = {
    "tablet_replace_snip": "replace_snip",
    "tablet_replace_graph": "replace_graph",
    "tablet_selection_padding": "selection_padding",
    "tablet_popup_seconds": "popup_seconds",
    "tablet_grab_delay_ms": "grab_delay_ms",
    "tablet_exact_selection": "exact_selection",
}

# one round trip that reports everything the watcher needs, one key=value per line
PROBE = r"""
echo "uptime=$(cut -d. -f1 /proc/uptime)"
echo "firmware=$(sed -n 's/^IMG_VERSION="\(.*\)"/\1/p' /etc/os-release)"
P=$(pidof xochitl)
echo "xochitl=${P:-none}"
# seconds since xochitl started (field 22 of stat is in clock ticks, 100/s)
[ -n "$P" ] && echo "age=$(( $(cut -d. -f1 /proc/uptime) - $(cut -d' ' -f22 /proc/$P/stat) / 100 ))"
[ -n "$P" ] && grep -q xovi.so /proc/$P/maps && echo xovi=running || echo xovi=off
[ -x /home/root/xovi/start ] || [ -f /home/root/xovi/start ] && echo installed=yes || echo installed=no
[ -f /home/root/xovi/exthome/qt-resource-rebuilder/snip2tex.qmd ] && echo patch=yes || echo patch=no
echo "server=$(cat %s 2>/dev/null)"
echo "tabletconfig=$(cat %s 2>/dev/null | tr -d '\n')"
echo "remote=$(cat %s 2>/dev/null)"
echo "token=$(cat %s 2>/dev/null)"
""" % (SERVER_FILE, TABLET_CONFIG, REMOTE_FILE, TOKEN_FILE)


class TabletWatcher:
    def __init__(self, config, mac_usb_ip, interval=8):
        self.config = config            # live dict; reads tablet_host, port, auto_start_xovi
        self.mac_usb_ip = mac_usb_ip    # callable -> this Mac's 10.11.99.x address or None
        self.interval = interval
        self.state = "not connected"
        self.detail = ""
        self.firmware = ""
        self._last_start = 0.0
        self._starts_this_boot = 0
        self._boot_mark = None
        self.remote_pushed = False  # the tablet has the current Wi-Fi URL and token

    def ssh(self, cmd, timeout=10, stdin=None):
        r = subprocess.run(
            ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=accept-new",
             f"root@{self.config['tablet_host']}", cmd],
            capture_output=True, text=True, timeout=timeout, input=stdin)
        if r.returncode == 255:
            raise ConnectionError(r.stderr.strip() or "ssh failed")
        return r.stdout

    def reachable(self):
        try:
            with socket.create_connection((self.config["tablet_host"], 22), timeout=1):
                return True
        except OSError:
            return False

    def check(self):
        if not self.reachable():
            self.state, self.detail = "not connected", "plug in the USB cable"
            return
        if not KEY.exists():
            self.state, self.detail = "no ssh key", f"missing {KEY}"
            return
        info = dict(l.split("=", 1) for l in self.ssh(PROBE).splitlines() if "=" in l)
        self.firmware = info.get("firmware", "")
        uptime = int(info.get("uptime") or 0)
        boot = int(time.time()) - uptime
        if self._boot_mark is None or abs(boot - self._boot_mark) > 30:
            self._boot_mark, self._starts_this_boot = boot, 0

        # keep the tablet pointed at this Mac (USB DHCP can hand out a new address)
        ip = self.mac_usb_ip()
        want = f"http://{ip}:{self.config['port']}" if ip else ""
        if want and info.get("server") != want and info.get("installed") == "yes":
            self.ssh(f"echo {want} > {SERVER_FILE}")

        # push the button settings from the dashboard
        want_cfg = json.dumps({v: self.config[k] for k, v in TABLET_KEYS.items() if k in self.config}, sort_keys=True)
        if info.get("installed") == "yes" and info.get("tabletconfig") != want_cfg:
            self.ssh(f"cat > {TABLET_CONFIG}", stdin=want_cfg)

        # Wi-Fi mode: give the tablet the Funnel URL and token (or take them away)
        on = self.config.get("remote_enabled") and self.config.get("remote_url") and self.config.get("remote_token")
        want_remote = self.config["remote_url"] if on else ""
        want_token = self.config["remote_token"] if on else ""
        if info.get("installed") == "yes" and (info.get("remote") != want_remote or info.get("token") != want_token):
            if on:
                self.ssh(f"umask 077; cat > {TOKEN_FILE}", stdin=want_token)
                self.ssh(f"cat > {REMOTE_FILE}", stdin=want_remote)
            else:
                self.ssh(f"rm -f {REMOTE_FILE} {TOKEN_FILE}")
        self.remote_pushed = bool(on)

        notes = []
        if self.firmware and self.firmware != PATCHED_FIRMWARE:
            notes.append(f"firmware is {self.firmware}, patch was made for {PATCHED_FIRMWARE}")
        if info.get("patch") != "yes":
            notes.append("snip2tex patch missing on tablet")

        if info.get("installed") != "yes":
            self.state = "xovi not installed"
        elif info.get("xovi") == "running":
            self.state = "ready"
        elif not self.config.get("auto_start_xovi", True):
            self.state = "xovi off (auto-start disabled)"
        elif info.get("xochitl") == "none" or uptime < 45 or int(info.get("age") or 0) < 30:
            # also covers someone restarting xochitl by hand: two restarts at
            # once trip systemd's start limit and reboot the tablet
            self.state = "tablet starting up"
        elif self._starts_this_boot >= 2:
            # don't loop: two tries per boot, then leave it to a human
            self.state = "xovi failed to start"
        elif time.time() - self._last_start > 90:
            self._last_start = time.time()
            self._starts_this_boot += 1
            self.state = "starting xovi"
            # reset-failed: repeated xochitl restarts trip systemd's start limit
            self.ssh("systemctl reset-failed xochitl; cd /home/root && "
                     "(nohup sh xovi/start >/dev/null 2>&1 &)", timeout=15)
        self.detail = "; ".join(notes)

    def run(self):
        while True:
            try:
                self.check()
            except Exception as e:
                self.state, self.detail = "error", str(e)[:200]
            time.sleep(self.interval)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self

    def status(self):
        return {"state": self.state, "detail": self.detail, "firmware": self.firmware}
