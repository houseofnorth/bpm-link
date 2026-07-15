# bpm-link

Listens to an audio device (BlackHole loopback, a mixer feed, any input),
detects the tempo in real time, and broadcasts it two ways:

## GUI app

`dist/BPM Link.app` — a menu-bar app (`bpm_link_gui.py`, native AppKit):

- live BPM in the status bar, menu shows device picker / Link toggle / quit
- **Big BPM Window** (⌘B in the menu): always-on-top Bauhaus-style panel —
  giant Futura digits, red dot pulsing on the beat, yellow confidence bar.
  Drag to move, double-click to hide.
- **Verbose Info** (⌘I): adds device / sample rate / Link peers / clock
  status to the window footer
- settings persist across launches (device, Link on/off, verbose, window)

Rebuild after code changes:

```sh
.venv/bin/pyinstaller --windowed --name "BPM Link" \
  --hidden-import mido.backends.rtmidi \
  --osx-bundle-identifier com.fubbi.bpmlink --noconfirm bpm_link_gui.py
PLIST="dist/BPM Link.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'BPM Link listens to an audio device to detect the tempo.'" "$PLIST"
codesign --force --deep -s - "dist/BPM Link.app"
```

- **MIDI clock** — a virtual MIDI output port `BPM-Link Clock` sending
  standard 24 PPQN clock. Select it as a sync source in any DAW/app/gear.
- **Ableton Link** — proposes the detected tempo to all Link peers on the
  local network (Ableton Live, Traktor, TouchDesigner, etc.).

## Run

A standalone executable is installed on PATH (symlinked from `dist/bpm-link`
to `/opt/homebrew/bin/bpm-link`):

```sh
bpm-link                      # auto-picks BlackHole
bpm-link --list-devices
bpm-link --device "Scarlett"
```

Or from source:

```sh
cd ~/Documents/Claude/Projects/bpm-link
.venv/bin/python bpm_link.py
```

Rebuild the executable after code changes:

```sh
.venv/bin/pyinstaller --onefile --name bpm-link \
  --hidden-import mido.backends.rtmidi bpm_link.py
```

Status line shows live BPM, detection confidence, and Link peer count.
Ctrl-C to quit (sends MIDI stop and closes the port).

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--device` | `blackhole` match | input device, name substring or index |
| `--port-name` | `BPM-Link Clock` | virtual MIDI port name |
| `--min-bpm` / `--max-bpm` | 70 / 180 | detection range |
| `--min-conf` | 0.25 | confidence needed before tempo is broadcast |
| `--no-link` | off | disable Ableton Link |

## Notes

- To capture system audio you need a loopback driver:
  `brew install blackhole-2ch`, then set BlackHole (or a Multi-Output
  device including it) as the Mac's output.
- The MIDI clock is *tempo*-synced, not *phase*-locked: it runs at the
  detected BPM but its downbeat is free-running. Link peers phase-align
  among themselves as usual.
- Detection uses a spectral-flux onset envelope + autocorrelation with
  octave disambiguation; verified accurate to ±0.1 BPM on synthetic
  material from 85–174 BPM (`test_detector.py`).

## Setup from scratch

```sh
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
