"""Offline check: recovered beat phase must match the true click times."""
import numpy as np
from bpm_link import TempoEstimator, BeatGrid

SR = 44100

for bpm, start in [(120, 0.137), (126, 0.05), (98, 0.31), (140, 0.22), (174, 0.09)]:
    rng = np.random.default_rng(7)
    seconds = 20.0
    n = int(SR * seconds)
    audio = rng.standard_normal(n).astype(np.float32) * 0.01
    beat = 60.0 / bpm
    true_beats = np.arange(start, seconds - 0.2, beat)
    for t in true_beats:
        i = int(t * SR)
        dur = int(0.1 * SR)
        env = np.exp(-np.linspace(0, 9, dur))
        audio[i:i + dur] += 0.9 * env * np.sin(
            2 * np.pi * 60 * np.linspace(0, 0.1, dur)).astype(np.float32)

    est = TempoEstimator(SR, 70, 180)
    grid = BeatGrid()
    win = int(est.window_sec * SR)
    errs = []
    for end in range(win, n, int(0.5 * SR)):
        est.update(audio[end - win:end])
        if est.bpm and est.beat_offset is not None:
            t_end = end / SR                      # "wall clock" of window end
            measured = t_end - est.beat_offset
            grid.update(measured, est.bpm, est.phase_quality)
            if grid.locked:
                err = measured - true_beats[np.argmin(np.abs(true_beats - measured))]
                errs.append(err * 1000)
    mean_abs = np.mean(np.abs(errs[3:]))          # after settling
    print(f"bpm {bpm:5.1f} start {start*1000:5.1f}ms  "
          f"phase err mean {mean_abs:5.1f} ms  worst {np.max(np.abs(errs[3:])):5.1f} ms  "
          f"{'OK' if mean_abs < 15 else 'FAIL'}")
