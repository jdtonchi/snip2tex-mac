"""Keep the Mac awake with the lid closed, so the tablet buttons keep working
from a bag in a lecture. Uses `pmset -a disablesleep`, which needs root: the
first toggle asks for an admin password once (macOS prompt) and installs a
sudoers rule allowing exactly `pmset -a disablesleep 0|1` without a password.

Safeguards: sleep comes back when the app quits, and when on battery at or
below LOW_BATTERY percent."""
import re, subprocess, tempfile, threading, time

PMSET = "/usr/bin/pmset"
SUDOERS = "/etc/sudoers.d/snip2tex-pmset"
RULE = f"%admin ALL=(root) NOPASSWD: {PMSET} -a disablesleep 0, {PMSET} -a disablesleep 1\n"
LOW_BATTERY = 15

note = ""  # last thing worth telling the user (shown in the dashboard)


def sleep_disabled():
    out = subprocess.run([PMSET, "-g"], capture_output=True, text=True).stdout
    return bool(re.search(r"SleepDisabled\s+1", out))


def battery():
    """(percent, on_battery) or (None, False) on a Mac without one."""
    out = subprocess.run([PMSET, "-g", "batt"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)%", out)
    return (int(m.group(1)) if m else None), "Battery Power" in out


def _sudo(value):
    return subprocess.run(["/usr/bin/sudo", "-n", PMSET, "-a", "disablesleep", value],
                          capture_output=True, text=True).returncode == 0


def _install_rule_and_set(value):
    """One admin prompt: install the sudoers rule (checked by visudo first),
    then apply the setting."""
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as f:
        f.write(RULE)
        tmp = f.name
    script = (f"/usr/sbin/visudo -cf {tmp} && /usr/bin/install -m 0440 -o root -g wheel {tmp} {SUDOERS} "
              f"&& {PMSET} -a disablesleep {value}")
    apple = ('do shell script "' + script + '" with administrator privileges with prompt '
             '"Snip2TeX wants to keep your Mac awake with the lid closed. This asks once; '
             'afterwards the button works without a password."')
    r = subprocess.run(["/usr/bin/osascript", "-e", apple], capture_output=True, text=True)
    return r.returncode == 0


def set_sleep_disabled(disabled):
    global note
    value = "1" if disabled else "0"
    ok = _sudo(value) or _install_rule_and_set(value)
    note = "" if ok else "macOS didn't allow it (password prompt cancelled?)"
    return ok and sleep_disabled() == disabled


def status():
    pct, on_batt = battery()
    return {"sleep_disabled": sleep_disabled(), "battery": pct, "on_battery": on_batt,
            "low_battery": LOW_BATTERY, "note": note}


def guard(interval=60):
    """Turn sleep back on when the battery runs low while it's disabled."""
    global note

    def run():
        while True:
            try:
                pct, on_batt = battery()
                if on_batt and pct is not None and pct <= LOW_BATTERY and sleep_disabled():
                    if _sudo("0"):
                        note = f"sleep turned back on: battery at {pct}%"
            except Exception:
                pass
            time.sleep(interval)
    threading.Thread(target=run, daemon=True).start()


def restore():
    """On quit: never leave the Mac unable to sleep. Passwordless only."""
    try:
        if sleep_disabled():
            _sudo("0")
    except Exception:
        pass
