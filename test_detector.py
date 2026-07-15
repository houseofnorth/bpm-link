"""Offline check: feed the TempoEstimator synthetic beats at known BPMs."""
import numpy as np
from bpm_link import TempoEstimator

SR = 44100


def synth(bpm, seconds=10.0, swing_noise=0.0):
    """Kick on the beat + hat offbeat + pink-ish noise bed."""
    rng = np.random.default_rng(1)
    n = int(SR * seconds)
    audio = rng.standard_normal(n).astype(np.float32) * 0.01
    beat = 60.0 / bpm
    t = 0.0
    while t < seconds - 0.2:
        i = int(t * SR)
        dur = int(0.12 * SR)
        env = np.exp(-np.linspace(0, 8, dur))
        audio[i:i + dur] += 0.8 * env * np.sin(
            2 * np.pi * 55 * np.linspace(0, 0.12, dur)).astype(np.float32)
        # offbeat hat
        j = int((t + beat / 2) * SR)
        hd = int(0.03 * SR)
        if j + hd < n:
            audio[j:j + hd] += 0.2 * rng.standard_normal(hd).astype(np.float32) \
                * np.exp(-np.linspace(0, 10, hd)).astype(np.float32)
        t += beat + rng.normal(0, swing_noise)
    return audio


for bpm in [85, 100, 120, 126, 140, 160, 174]:
    est = TempoEstimator(SR, 70, 180)
    audio = synth(bpm)
    # simulate repeated 0.5 s update ticks over a sliding 8 s window
    win = int(est.window_sec * SR)
    for end in range(win, len(audio), int(0.5 * SR)):
        est.update(audio[end - win:end])
    err = abs(est.bpm - bpm)
    print(f"target {bpm:5.1f}  detected {est.bpm:6.1f}  conf {est.confidence:.2f}  "
          f"{'OK' if err < 1.0 else 'FAIL'}")
