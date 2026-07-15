#!/usr/bin/env python3
"""
BPM Link — macOS menu-bar app.

Live BPM in the status bar, an optional always-on-top Bauhaus-style window
with a big tempo readout, MIDI clock on a virtual port, Ableton Link.
Reuses the detection engine from bpm_link.py.
"""

import asyncio
import threading
import time

import numpy as np
import sounddevice as sd

import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSAttributedString,
    NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont,
    NSFontAttributeName, NSForegroundColorAttributeName, NSKernAttributeName,
    NSMakePoint, NSMakeRect, NSMenu, NSMenuItem, NSObject, NSPanel, NSScreen,
    NSStatusBar, NSTimer, NSVariableStatusItemLength, NSView,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSUserDefaults

from bpm_link import AudioRing, TempoEstimator, MidiClock

try:
    import aalink
    HAVE_LINK = True
except ImportError:
    HAVE_LINK = False

NSWindowStyleMaskNonactivatingPanel = 1 << 7
NSFloatingWindowLevel = 5


# ---------------------------------------------------------------- palette

def _rgb(r, g, b):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)

PAPER  = _rgb(0.956, 0.942, 0.905)
INK    = _rgb(0.09, 0.09, 0.09)
RED    = _rgb(0.804, 0.153, 0.106)
BLUE   = _rgb(0.102, 0.216, 0.478)
YELLOW = _rgb(0.937, 0.757, 0.102)


def futura(size, weight="Bold"):
    return (NSFont.fontWithName_size_(f"Futura-{weight}", size)
            or NSFont.boldSystemFontOfSize_(size))


def attr(text, font, color, kern=0.0):
    return NSAttributedString.alloc().initWithString_attributes_(text, {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
        NSKernAttributeName: kern,
    })


# ---------------------------------------------------------------- link

class LinkWorker(threading.Thread):
    """Ableton Link inside its own asyncio loop thread."""

    def __init__(self):
        super().__init__(daemon=True)
        self.loop = None
        self.link = None
        self.ready = threading.Event()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        def make():
            self.link = aalink.Link(120)
            self.link.enabled = True
            self.ready.set()

        self.loop.call_soon(make)
        self.loop.run_forever()

    def set_tempo(self, bpm):
        if self.ready.is_set():
            self.loop.call_soon_threadsafe(
                lambda: setattr(self.link, "tempo", float(bpm)))

    def set_enabled(self, on):
        if self.ready.is_set():
            self.loop.call_soon_threadsafe(
                lambda: setattr(self.link, "enabled", bool(on)))

    @property
    def peers(self):
        return self.link.num_peers if self.ready.is_set() else 0


# ---------------------------------------------------------------- engine

class Engine:
    """Audio in → tempo estimate → MIDI clock + Link, on worker threads."""

    def __init__(self, min_conf=0.25):
        self.lock = threading.Lock()
        self.stream = None
        self.ring = None
        self.estimator = None
        self.device_name = None
        self.samplerate = None
        self.error = None
        self.min_conf = min_conf
        self.min_bpm, self.max_bpm = 70.0, 180.0
        self.link_on = True
        self.started = False
        self.last_beat = 0.0

        self.clock = MidiClock("BPM-Link Clock")
        self.clock.on_beat = lambda: setattr(self, "last_beat", time.time())
        self.clock.start()

        self.link = LinkWorker() if HAVE_LINK else None
        if self.link:
            self.link.start()

        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._analyze_loop, daemon=True)
        self._worker.start()

    # -- device handling

    @staticmethod
    def input_devices():
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0]

    def set_device(self, index):
        with self.lock:
            if self.stream:
                self.stream.close()
                self.stream = None
            try:
                info = sd.query_devices(index)
                sr = int(info["default_samplerate"])
                ring = AudioRing(sr)

                def callback(indata, frames, time_info, status):
                    ring.write(indata.mean(axis=1)
                               if indata.shape[1] > 1 else indata[:, 0])

                self.stream = sd.InputStream(
                    device=index, channels=min(2, info["max_input_channels"]),
                    samplerate=sr, blocksize=256, dtype="float32",
                    callback=callback)
                self.stream.start()
                self.ring = ring
                self.samplerate = sr
                self.estimator = TempoEstimator(sr, self.min_bpm, self.max_bpm)
                self.device_name = info["name"]
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                self.device_name = None
                self.ring = self.estimator = None

    # -- analysis

    def _analyze_loop(self):
        while not self._stop.is_set():
            time.sleep(0.5)
            with self.lock:
                ring, est = self.ring, self.estimator
            if not ring:
                continue
            est.update(ring.latest(int(est.window_sec * est.sr)))
            if est.bpm and est.signal and est.confidence >= self.min_conf:
                self.clock.bpm = est.bpm
                if not self.started:
                    self.clock.running.set()
                    self.started = True
                if self.link and self.link_on:
                    self.link.set_tempo(est.bpm)

    # -- state for the UI

    def state(self):
        est = self.estimator
        return {
            "bpm": est.bpm if est else None,
            "conf": est.confidence if est else 0.0,
            "signal": est.signal if est else False,
            "device": self.device_name,
            "samplerate": self.samplerate,
            "peers": self.link.peers if self.link else 0,
            "link_on": self.link_on and self.link is not None,
            "clock_running": self.started,
            "error": self.error,
            "last_beat": self.last_beat,
        }

    def shutdown(self):
        self._stop.set()
        with self.lock:
            if self.stream:
                self.stream.close()
        self.clock.shutdown()


# ---------------------------------------------------------------- big window

W, H = 210, 104


class BauhausView(NSView):
    """The floating tile: dark ink, giant Futura digits, pulsing beat dot."""

    def initWithEngine_(self, engine):
        self = objc.super(BauhausView, self).initWithFrame_(
            NSMakeRect(0, 0, W, H))
        self.engine = engine
        self.verbose = True
        return self

    def isFlipped(self):
        return False

    def mouseDown_(self, event):
        if event.clickCount() >= 2:
            self.window().orderOut_(None)
        else:
            self.window().performWindowDragWithEvent_(event)

    def drawRect_(self, rect):
        s = self.engine.state()

        INK.set()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, W, H))
        BLUE.set()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, 8, H))

        # beat dot: red circle that pulses on every MIDI-clock beat
        bpm = s["bpm"] or 120.0
        decay = (time.time() - s["last_beat"]) / (60.0 / bpm * 0.6)
        r = 6 + 5 * max(0.0, 1.0 - decay) if s["clock_running"] else 6
        (RED if s["signal"] else PAPER.colorWithAlphaComponent_(0.25)).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(32 - r, 70 - r, 2 * r, 2 * r)).fill()

        # the number
        if s["bpm"] and s["signal"]:
            text = f"{s['bpm']:.1f}"
        elif s["error"]:
            text = "!"
        else:
            text = "—"
        big = attr(text, futura(54, "CondensedExtraBold"),
                   RED if s["error"] else PAPER)
        size = big.size()
        big.drawAtPoint_(NSMakePoint(W - 14 - size.width, 34))

        if self.verbose:
            dim = PAPER.colorWithAlphaComponent_(0.55)
            if s["error"]:
                l1, l2 = "ERROR", s["error"][:34].upper()
            else:
                l1 = (f"{(s['device'] or 'NO DEVICE').upper()[:22]}"
                      f"  ·  {int((s['samplerate'] or 0)/1000)}K")
                l2 = (f"LINK {s['peers']}  ·  CONF {s['conf']:.2f}"
                      f"  ·  {'RUN' if s['clock_running'] else 'IDLE'}"
                      f"{'' if s['signal'] else '  ·  NO SIGNAL'}")
            attr(l1, futura(8, "Medium"), dim, kern=1.0)\
                .drawAtPoint_(NSMakePoint(20, 20))
            attr(l2, futura(8, "Medium"), dim, kern=1.0)\
                .drawAtPoint_(NSMakePoint(20, 8))


# ---------------------------------------------------------------- app

class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, note):
        self.defaults = NSUserDefaults.standardUserDefaults()
        self.engine = Engine()

        # restore settings
        self.engine.link_on = self.defaults.objectForKey_("link") in (None, True, 1)
        saved_device = self.defaults.stringForKey_("device")
        self._pick_initial_device(saved_device)

        # status bar item
        self.status = NSStatusBar.systemStatusBar()\
            .statusItemWithLength_(NSVariableStatusItemLength)
        self.status.button().setTitle_("♪ —")
        self._last_title = ""

        # floating window
        self.panel = NSPanel.alloc()\
            .initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, H),
                NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
                NSBackingStoreBuffered, False)
        self.view = BauhausView.alloc().initWithEngine_(self.engine)
        self.view.verbose = self.defaults.objectForKey_("verbose") in (None, True, 1)
        self.panel.setContentView_(self.view)
        self.on_top = self.defaults.objectForKey_("ontop") in (None, True, 1)
        self.panel.setLevel_(NSFloatingWindowLevel if self.on_top else 0)
        self.panel.setHidesOnDeactivate_(False)
        screen = NSScreen.mainScreen().visibleFrame()
        self.panel.setFrameOrigin_(NSMakePoint(
            screen.origin.x + screen.size.width - W - 30,
            screen.origin.y + screen.size.height - H - 40))
        if self.defaults.objectForKey_("window") in (None, True, 1):
            self.panel.orderFront_(None)

        self._build_menu()

        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "tick:", None, True)

    # -- devices

    def _pick_initial_device(self, saved_name):
        devices = Engine.input_devices()
        index = None
        if saved_name:
            index = next((i for i, n in devices if n == saved_name), None)
        if index is None:
            index = next((i for i, n in devices
                          if "blackhole" in n.lower()), None)
        if index is not None:
            self.engine.set_device(index)
        else:
            self.engine.error = "no BlackHole device — pick an input"

    # -- menu

    def _build_menu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        self.info_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("starting…", None, "")
        self.info_item.setEnabled_(False)
        menu.addItem_(self.info_item)
        menu.addItem_(NSMenuItem.separatorItem())

        self.window_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Big BPM Window", "toggleWindow:", "b")
        self.window_item.setTarget_(self)
        self.window_item.setState_(1 if self.panel.isVisible() else 0)
        menu.addItem_(self.window_item)

        self.verbose_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Verbose Info", "toggleVerbose:", "i")
        self.verbose_item.setTarget_(self)
        self.verbose_item.setState_(1 if self.view.verbose else 0)
        menu.addItem_(self.verbose_item)

        self.ontop_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Float on Top", "toggleOnTop:", "t")
        self.ontop_item.setTarget_(self)
        self.ontop_item.setState_(1 if self.on_top else 0)
        menu.addItem_(self.ontop_item)
        menu.addItem_(NSMenuItem.separatorItem())

        device_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Input Device", None, "")
        self.device_menu = NSMenu.alloc().init()
        self.device_menu.setAutoenablesItems_(False)
        self._fill_device_menu()
        device_item.setSubmenu_(self.device_menu)
        menu.addItem_(device_item)

        self.link_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Ableton Link", "toggleLink:", "l")
        self.link_item.setTarget_(self)
        self.link_item.setState_(1 if self.engine.link_on and HAVE_LINK else 0)
        self.link_item.setEnabled_(HAVE_LINK)
        menu.addItem_(self.link_item)
        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Quit BPM Link", "quitApp:", "q")
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        self.status.setMenu_(menu)

    def _fill_device_menu(self):
        self.device_menu.removeAllItems()
        for index, name in Engine.input_devices():
            item = NSMenuItem.alloc()\
                .initWithTitle_action_keyEquivalent_(name, "pickDevice:", "")
            item.setTarget_(self)
            item.setTag_(index)
            item.setState_(1 if name == self.engine.device_name else 0)
            self.device_menu.addItem_(item)
        self.device_menu.addItem_(NSMenuItem.separatorItem())
        refresh = NSMenuItem.alloc()\
            .initWithTitle_action_keyEquivalent_("Refresh Devices", "refreshDevices:", "")
        refresh.setTarget_(self)
        self.device_menu.addItem_(refresh)

    # -- actions

    def toggleWindow_(self, sender):
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        else:
            self.panel.orderFront_(None)
        self.window_item.setState_(1 if self.panel.isVisible() else 0)
        self.defaults.setBool_forKey_(self.panel.isVisible(), "window")

    def toggleVerbose_(self, sender):
        self.view.verbose = not self.view.verbose
        self.verbose_item.setState_(1 if self.view.verbose else 0)
        self.defaults.setBool_forKey_(self.view.verbose, "verbose")
        self.view.setNeedsDisplay_(True)

    def toggleOnTop_(self, sender):
        self.on_top = not self.on_top
        self.panel.setLevel_(NSFloatingWindowLevel if self.on_top else 0)
        self.ontop_item.setState_(1 if self.on_top else 0)
        self.defaults.setBool_forKey_(self.on_top, "ontop")

    def toggleLink_(self, sender):
        self.engine.link_on = not self.engine.link_on
        if self.engine.link:
            self.engine.link.set_enabled(self.engine.link_on)
        self.link_item.setState_(1 if self.engine.link_on else 0)
        self.defaults.setBool_forKey_(self.engine.link_on, "link")

    def pickDevice_(self, sender):
        # sounddevice must re-scan so a fresh index is valid
        sd._terminate(); sd._initialize()
        self.engine.set_device(sender.tag())
        if self.engine.device_name:
            self.defaults.setObject_forKey_(self.engine.device_name, "device")
        self._fill_device_menu()

    def refreshDevices_(self, sender):
        sd._terminate(); sd._initialize()
        self._fill_device_menu()

    def quitApp_(self, sender):
        self.engine.shutdown()
        NSApplication.sharedApplication().terminate_(None)

    # -- periodic UI update

    def tick_(self, timer):
        s = self.engine.state()
        if s["error"]:
            title = "♪ !"
        elif s["bpm"] and s["signal"]:
            title = f"♪ {s['bpm']:.1f}"
        else:
            title = "♪ —"
        if title != self._last_title:
            self.status.button().setTitle_(title)
            self._last_title = title
            self.info_item.setTitle_(self._info_line(s))
        if self.panel.isVisible():
            self.view.setNeedsDisplay_(True)

    @staticmethod
    def _info_line(s):
        if s["error"]:
            return s["error"]
        bpm = f"{s['bpm']:.1f} BPM" if s["bpm"] else "listening…"
        sig = "" if s["signal"] else " · no signal"
        return f"{bpm} · conf {s['conf']:.2f} · {s['peers']} link peers{sig}"


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
