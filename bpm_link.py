#!/usr/bin/env python3
"""
bpm-link: listen to an audio device (e.g. BlackHole loopback), detect the BPM
in real time, and broadcast it as MIDI clock (24 PPQN on a virtual MIDI port)
and optionally as Ableton Link tempo.

Usage:
  python bpm_link.py                     # auto-picks a device matching "blackhole"
  python bpm_link.py --list-devices
  python bpm_link.py --device "Scarlett" # substring match or numeric index
  python bpm_link.py --no-link
"""

import argparse
import asyncio
import collections
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import mido

try:
    import aalink
    HAVE_LINK = True
except ImportError:
    HAVE_LINK = False

FFT_SIZE = 1024
HOP = 256


# ---------------------------------------------------------------- audio in

class AudioRing:
    """Lock-protected ring buffer of the most recent mono audio."""

    def __init__(self, samplerate, seconds=12.0):
        self.samplerate = samplerate
        self.buf = np.zeros(int(samplerate * seconds), dtype=np.float32)
        self.lock = threading.Lock()
        self.total_written = 0

    def write(self, mono):
        with self.lock:
            n = len(mono)
            if n >= len(self.buf):
                self.buf[:] = mono[-len(self.buf):]
            else:
                self.buf = np.roll(self.buf, -n)
                self.buf[-n:] = mono
            self.total_written += n

    def latest(self, n_samples):
        with self.lock:
            return self.buf[-n_samples:].copy()


# ---------------------------------------------------------------- tempo

class TempoEstimator:
    """Spectral-flux onset envelope + autocorrelation tempo estimate."""

    def __init__(self, samplerate, min_bpm, max_bpm, window_sec=8.0):
        self.sr = samplerate
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.window_sec = window_sec
        self.frame_rate = samplerate / HOP
        self.hann = np.hanning(FFT_SIZE).astype(np.float32)
        self.history = collections.deque(maxlen=9)
        self.bpm = None          # smoothed, published value
        self.confidence = 0.0
        self.signal = False

    def _onset_envelope(self, audio):
        frames = np.lib.stride_tricks.sliding_window_view(audio, FFT_SIZE)[::HOP]
        mags = np.abs(np.fft.rfft(frames * self.hann, axis=1))
        logmag = np.log1p(10.0 * mags)
        flux = np.diff(logmag, axis=0)
        np.maximum(flux, 0.0, out=flux)
        env = flux.sum(axis=1)
        # remove slow trend so the autocorrelation sees rhythm, not level
        k = int(self.frame_rate)  # ~1 s moving average
        trend = np.convolve(env, np.ones(k) / k, mode="same")
        env = np.maximum(env - trend, 0.0)
        # light smoothing (~20 ms) so autocorrelation peaks aren't razor-thin
        w = np.hanning(5)
        return np.convolve(env, w / w.sum(), mode="same")

    def update(self, audio):
        rms = float(np.sqrt(np.mean(audio**2)))
        self.signal = rms > 1e-4
        if not self.signal:
            self.confidence = 0.0
            return

        env = self._onset_envelope(audio)
        env = env - env.mean()
        acf = np.correlate(env, env, mode="full")[len(env) - 1:]
        if acf[0] <= 0:
            return
        acf = acf / acf[0]

        min_lag = max(2, int(60.0 * self.frame_rate / self.max_bpm))
        max_lag = min((len(acf) - 3) // 3, int(60.0 * self.frame_rate / self.min_bpm))
        if max_lag <= min_lag:
            return

        lags = np.arange(min_lag, max_lag + 1)
        # comb score: reward lags whose multiples also line up (octave
        # disambiguation); take a local max around each multiple since the
        # true period is rarely an exact frame multiple
        def peak_at(mult):
            return np.max([acf[lags * mult + o] for o in (-2, -1, 0, 1, 2)], axis=0)

        score = acf[lags] + 0.5 * peak_at(2) + 0.33 * peak_at(3)
        bpms = 60.0 * self.frame_rate / lags
        # gentle prior centred on club tempos
        score = score * np.exp(-0.5 * (np.log2(bpms / 125.0) / 0.9) ** 2)

        best = int(np.argmax(score))
        lag = lags[best]
        # parabolic interpolation on the raw ACF for sub-lag precision
        if min_lag < lag < max_lag:
            y0, y1, y2 = acf[lag - 1], acf[lag], acf[lag + 1]
            denom = y0 - 2 * y1 + y2
            if abs(denom) > 1e-9:
                lag = lag + 0.5 * (y0 - y2) / denom

        raw_bpm = 60.0 * self.frame_rate / lag
        self.confidence = float(np.clip(acf[int(round(lag))], 0.0, 1.0))
        self.history.append(raw_bpm)

        median = float(np.median(self.history))
        # only move the published tempo when the median genuinely moved,
        # so MIDI clock and Link don't chatter on estimation noise
        if self.bpm is None or abs(median - self.bpm) > 0.25:
            self.bpm = round(median, 1)


# ---------------------------------------------------------------- midi clock

class MidiClock(threading.Thread):
    """Free-running 24 PPQN MIDI clock on a virtual output port."""

    def __init__(self, port_name):
        super().__init__(daemon=True)
        self.port = mido.open_output(port_name, virtual=True)
        self.bpm = 120.0
        self.running = threading.Event()
        self.stop_flag = threading.Event()
        self.on_beat = None  # called every 24th clock (once per beat)

    def run(self):
        self.running.wait()
        self.port.send(mido.Message("start"))
        next_t = time.perf_counter()
        pulses = 0
        while not self.stop_flag.is_set():
            interval = 60.0 / (self.bpm * 24.0)
            next_t += interval
            self.port.send(mido.Message("clock"))
            if pulses % 24 == 0 and self.on_beat:
                self.on_beat()
            pulses += 1
            while True:
                rem = next_t - time.perf_counter()
                if rem <= 0:
                    break
                time.sleep(rem - 0.001 if rem > 0.002 else 0)
            # if we fell badly behind (e.g. laptop slept), resync
            if time.perf_counter() - next_t > 0.25:
                next_t = time.perf_counter()
        self.port.send(mido.Message("stop"))
        self.port.close()

    def shutdown(self):
        self.stop_flag.set()
        self.running.set()  # unblock if never started


# ---------------------------------------------------------------- device pick

def pick_device(spec):
    devices = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if spec is not None:
        if spec.isdigit():
            return int(spec)
        matches = [i for i, d in inputs if spec.lower() in d["name"].lower()]
        if matches:
            return matches[0]
        sys.exit(f"No input device matching {spec!r}. Try --list-devices.")
    for i, d in inputs:
        if "blackhole" in d["name"].lower():
            return i
    print("No BlackHole device found. Available inputs:")
    for i, d in inputs:
        print(f"  [{i}] {d['name']}")
    sys.exit("Pick one with --device <name-or-index>.")


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  ({d['max_input_channels']} in, "
                  f"{d['default_samplerate']:.0f} Hz)")


# ---------------------------------------------------------------- main loop

async def main(args):
    device = pick_device(args.device)
    info = sd.query_devices(device)
    samplerate = int(info["default_samplerate"])
    ring = AudioRing(samplerate)
    estimator = TempoEstimator(samplerate, args.min_bpm, args.max_bpm)

    def callback(indata, frames, time_info, status):
        ring.write(indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0])

    stream = sd.InputStream(device=device, channels=min(2, info["max_input_channels"]),
                            samplerate=samplerate, blocksize=HOP,
                            dtype="float32", callback=callback)

    clock = MidiClock(args.port_name)
    clock.start()

    link = None
    if HAVE_LINK and not args.no_link:
        link = aalink.Link(120)
        link.enabled = True

    print(f"listening:  {info['name']}  @ {samplerate} Hz")
    print(f"midi out:   virtual port \"{args.port_name}\" (24 PPQN clock)")
    print(f"link:       {'enabled' if link else 'off' if args.no_link else 'unavailable (pip install aalink)'}")
    print()

    window = int(estimator.window_sec * samplerate)
    started = False
    with stream:
        while True:
            await asyncio.sleep(0.5)
            estimator.update(ring.latest(window))
            bpm = estimator.bpm
            if bpm and estimator.signal and estimator.confidence >= args.min_conf:
                clock.bpm = bpm
                if not started:
                    clock.running.set()
                    started = True
                if link:
                    link.tempo = bpm
            peers = f"  link peers: {link.num_peers}" if link else ""
            state = f"{bpm:6.1f} BPM" if bpm else "  ...   "
            sig = "" if estimator.signal else "  [no signal]"
            print(f"\r  ♪ {state}   conf {estimator.confidence:4.2f}{peers}{sig}   ",
                  end="", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Audio → BPM → MIDI clock + Ableton Link")
    p.add_argument("--device", help="input device name substring or index "
                                    "(default: first device matching 'blackhole')")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--port-name", default="BPM-Link Clock")
    p.add_argument("--min-bpm", type=float, default=70.0)
    p.add_argument("--max-bpm", type=float, default=180.0)
    p.add_argument("--no-link", action="store_true", help="disable Ableton Link")
    p.add_argument("--min-conf", type=float, default=0.25,
                   help="confidence needed before tempo is broadcast (default 0.25)")
    args = p.parse_args()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nbye")
