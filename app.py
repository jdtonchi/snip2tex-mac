"""Snip2TeX app: runs the server and the model, with a window showing the
dashboard and a ∑ in the menu bar. Closing the window keeps it running;
launching it again (or the menu's Open Snip2TeX) brings the window back.
Launched by Snip2TeX Engine.app (see make_app.sh)."""
import threading, webbrowser

import rumps
from AppKit import (NSAlert, NSApp, NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBezierPath, NSColor, NSImage, NSMakeRect, NSObject,
                    NSStatusBar, NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

import power
import snip2tex


SIGMA = [(10, 6), (38, 6), (38, 14), (35, 14), (35, 9), (15.6, 9), (28.1, 24), (15.6, 39), (35, 39),
         (35, 34), (38, 34), (38, 42), (10, 42), (10, 40.4), (24.2, 24), (10, 7.6)]


def log(*a):
    print("[app]", *a, flush=True)  # ends up in ~/.snip2tex/app.log


def sigma_image(size=18):
    """The tablet's ∑ glyph as a template image (macOS tints it for light/dark)."""
    img = NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    path = NSBezierPath.bezierPath()
    k = size / 48
    for i, (x, y) in enumerate(SIGMA):
        pt = (x * k, size - y * k)  # flip: SVG y grows down
        path.moveToPoint_(pt) if i == 0 else path.lineToPoint_(pt)
    path.closePath()
    NSColor.blackColor().set()
    path.fill()
    img.unlockFocus()
    img.setTemplate_(True)
    return img


class WindowDelegate(NSObject):
    def windowWillClose_(self, _):
        # back to menu-bar only: no Dock icon while the window is closed
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)


class WebUIDelegate(NSObject):
    """WKWebView drops alert()/confirm() unless the app shows them itself;
    without this the dashboard's Quit confirmation silently answered "no"."""

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
            self, web, message, frame, done):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.runModal()
        done()

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
            self, web, message, frame, done):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        done(alert.runModal() == 1000)  # NSAlertFirstButtonReturn


class Snip2TeXApp(rumps.App):
    def __init__(self):
        super().__init__("snip2tex", title="∑", quit_button=None)
        self.window = None
        self.ns_item = None
        self.status_item = rumps.MenuItem("Starting…")
        self.tablet_item = rumps.MenuItem("Tablet: checking…")
        self.lid_item = rumps.MenuItem("Stay Awake with Lid Closed", callback=self.toggle_lid)
        self.menu = [
            self.status_item,
            self.tablet_item,
            None,
            rumps.MenuItem("Open Snip2TeX", callback=lambda _: self.show_window()),
            rumps.MenuItem("Open Gallery…", callback=lambda _: self.show_window("/gallery")),
            rumps.MenuItem("Open in Browser", callback=self.open_browser),
            rumps.MenuItem("Reload Model", callback=lambda _: snip2tex.load_model()),
            None,
            self.lid_item,
            None,
            rumps.MenuItem("Quit Snip2TeX", callback=self.quit),
        ]
        # the server calls this when the launcher asks for the window
        snip2tex.show_hook = lambda: AppHelper.callAfter(self.show_window)
        snip2tex.start(open_browser=False)
        rumps.Timer(self.refresh, 2).start()
        rumps.Timer(self._after_launch, 0.3).start()

    def _after_launch(self, timer):
        timer.stop()
        try:
            self.make_status_item()
        except Exception as e:
            log("status item failed:", repr(e))
        self.refresh()
        self.show_window()
        rumps.Timer(self._report_status_item, 3).start()

    def make_status_item(self):
        """Our own menu bar item, made after launch finished. rumps' item sets
        its title with the deprecated NSStatusItem.setTitle_, which leaves it
        invisible on macOS 26; drop it and attach rumps' menu to ours."""
        bar = NSStatusBar.systemStatusBar()
        old = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if old is not None:
            bar.removeStatusItem_(old)
        item = bar.statusItemWithLength_(-1)  # NSVariableStatusItemLength
        item.setAutosaveName_("Snip2TeXStatusItem")
        button = item.button()
        button.setImage_(sigma_image())
        button.setImagePosition_(2)  # NSImageLeft: glyph, then a "…" / "!" suffix
        button.setToolTip_("Snip2TeX")
        item.setMenu_(self._menu._menu)
        item.setVisible_(True)
        self.ns_item = item

    def _report_status_item(self, timer):
        timer.stop()
        item = self.ns_item
        if item is None:
            log("status item: none")
            return
        b = item.button()
        w = b.window()
        log("status item: visible", item.isVisible(), "length", item.length(),
            "button frame", tuple(b.frame().size), "image", b.image() is not None,
            "window", tuple(w.frame().origin) + tuple(w.frame().size) if w else None,
            "on screen", w.isVisible() if w else None,
            "bar thickness", NSStatusBar.systemStatusBar().thickness())

    def show_window(self, page="/"):
        url = f"http://localhost:{snip2tex.config['port']}{page}"
        if self.window is None:
            style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                     NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 1040, 760), style, NSBackingStoreBuffered, False)
            self.window.setTitle_("Snip2TeX")
            self.window.setReleasedWhenClosed_(False)
            self.window.center()
            self.window.setFrameAutosaveName_("Snip2TeXMain")
            self.delegate = WindowDelegate.alloc().init()
            self.window.setDelegate_(self.delegate)
            self.web = WKWebView.alloc().initWithFrame_configuration_(
                self.window.contentView().bounds(), WKWebViewConfiguration.alloc().init())
            self.web.setAutoresizingMask_(18)  # width + height sizable
            self.ui_delegate = WebUIDelegate.alloc().init()
            self.web.setUIDelegate_(self.ui_delegate)
            self.window.contentView().addSubview_(self.web)
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        # a Dock icon and Cmd-Tab entry while the window is open
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def toggle_lid(self, _):
        # pmset can show an admin prompt the first time: keep it off the UI thread
        want = not power.sleep_disabled()
        threading.Thread(target=power.set_sleep_disabled, args=(want,), daemon=True).start()

    def quit(self, _):
        power.restore()  # never leave the Mac unable to sleep
        rumps.quit_application()

    def open_browser(self, _):
        webbrowser.open(f"http://localhost:{snip2tex.config['port']}")

    def refresh(self, _=None):
        m = snip2tex.llm().status()
        name = (m.get("model") or "").rstrip("/").split("/")[-1]
        self.status_item.title = f"Model: {m['state']}" + (f" · {name}" if name else "")
        suffix = "" if m["state"] == "ready" else "…" if m["state"] in ("loading", "downloading") else "!"
        progress = list(snip2tex.deps.progress.values())
        if progress:
            self.status_item.title = progress[0]
        t = snip2tex.watcher.status()
        self.tablet_item.title = f"Tablet: {t['state']}"
        if t["state"] not in ("ready", "not connected") and not suffix:
            suffix = "!" if t["state"] in ("error", "xovi failed to start") else "…"
        try:
            self.lid_item.state = power.sleep_disabled()
        except Exception:
            pass
        if self.ns_item is not None:
            self.ns_item.button().setTitle_(suffix)


if __name__ == "__main__":
    Snip2TeXApp().run()
