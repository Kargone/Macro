# Desktop Macro Recorder & Player

A modern, high-performance, and dark-themed desktop application written in Python (`tkinter` + `pynput`) that records, manages, edits, and plays back keyboard and mouse event sequences globally.

## Core Features

- **Modern Dark Visual Design**: Custom dark-themed layout reminiscent of modern development environments.
- **Global Input Hooking**: Log and playback keys and mouse events even when the application is minimized or in the background.
- **Precision Hold & Interval**: Tracks precise milliseconds of hold durations (`hold_ms`) and interval gaps (`interval_ms`).
- **Concurrent Key Support (Multi-Key)**: Safely records and handles overlapping/simultaneous key holds.
- **Capped Mouse Movement Sampling**: Tracks mouse movement smoothly at 50Hz (20ms intervals) with a delta filter, capturing continuous curves without bloating file sizes.
- **Interactive Sequence Editor**: View the entire sequence in a structured table, selectively remove individual events, or clear entire sessions.
- **Robust Playback Controller**: Support for Play, Pause, Resume, and Stop. Fully responsive to thread state.
- **Adjustable Playback Gaps**: Speed multiplier spinbox (0.1x to 10.0x) to stretch or shrink all interval and hold durations on-the-fly.
- **Minimize-on-Record**: Automatically minimizes on recording start, with a restore and focus flash once finished.
- **Seamless Serialization**: Loads from and saves to `macro_recordings.json` using the pre-existing database schema.

---

## Universal Key Control (Global Hotkeys)

- **⏺ Start Recording**: Press **`F9`** globally from anywhere on your desktop (automatically minimizes window).
- **▶ Play/Replay Recording**: Press **`F8`** globally to instantly play back the active session.
- **⏹ Stop Recording / Playback**: Press **`F10`** globally to instantly abort recording or playback (Unified Abort Key).

*Note: The control hotkeys (`F8`, `F9`, and `F10`) are automatically excluded from your recordings to keep your macros clean.*

---

## Getting Started

### 1. Requirements

Ensure you have Python 3 and `pynput` installed:

```bash
pip install pynput
```

### 2. Silent/Invisible Launching (No CMD window)

To run the application silently in the background on Windows without an annoying command prompt window popup:

1. Double-click `run_app.pyw` to launch the application.

### 3. Manual Launch

To run normally with the standard terminal console:

```bash
python macro_app.py
```
