# Changelog

## 2026-10-07

### Fixed
- Text in the input fields (Speed, Repeat Count) was white on a white background.
- Clicking the Speed arrows broke Play: the box showed values like `1.1x`, which can't be read as a number. Speed and Repeat Count are now checked before playback starts, and an invalid value (including a speed of 0) shows an error instead of crashing.
- Raw Move playback couldn't turn the camera in raw-input games like Minecraft, because pynput's `move()` only sets the cursor position. It now sends real relative mouse input through `SendInput`.
- Raw Move recording dropped most mouse movement (roughly 7 of every 8 updates on a 1000Hz mouse), so recorded camera turns came out too small. Movement between samples is now added up instead of thrown away.
- Raw Move recording ignores absolute-position packets from Remote Desktop, VMs and pen tablets, which previously showed up as huge jumps.
- Pressing F9 twice quickly started recording twice and leaked a mouse listener, so clicks were recorded twice.
- A crash mid-save could corrupt `macro_recordings.json`, and the next launch would then overwrite it with an empty list. Saves now write to a temp file and swap it in, and an unreadable file is kept as a `.corrupt-<timestamp>` backup.
- Pressing Stop or Pause at the same moment playback pressed a key could leave that key held down.
- Stopping a recording could occasionally crash, because the key hook thread changed the recording data while it was being saved.
- Playback lagged on long macros because every played action re-styled every row of the event table.
- The DPI-awareness setup now lives in one shared function (`set_dpi_awareness`) used by both launchers, so they can't drift apart again.

### Changed
- Added a `.gitignore` and stopped tracking `__pycache__` and `macro_recordings.json` in git (the files are still on disk).

## 2026-09-06

### Fixed
- Ctrl+C / Ctrl+V and other Ctrl+letter shortcuts were recorded as raw control characters (e.g. `\x16` for Ctrl+V) instead of the literal key, so playback silently failed to trigger the shortcut instead of pasting/copying. The recorder now converts these back to the real letter on capture; the existing "Scripture Notes" recording was patched in place so it didn't need to be re-recorded.
- Recording could silently stop capturing keys and mouse partway through, and the F7-F10 hotkeys could stop responding for the rest of the session, with no visible error. Root cause: any key combo with no printable character (Ctrl+1, Ctrl+=, dead keys, certain Alt combos, etc.) crashed the global keyboard hook with an `AttributeError`; pynput responds to any uncaught exception in that hook by permanently killing the listener. This crash could be triggered by a keystroke anywhere on the system, not just while recording. Fixed the crash at its source and wrapped the global key handler in a try/except as a safety net so this class of bug can't take down the listener again.

- Cursor could drift off target - or completely off-screen - and get "stuck" there for the rest of playback, on multi-monitor setups where monitors use different scaling factors. Cause: the app declared System-DPI-Aware instead of Per-Monitor-DPI-Aware v2, so Windows silently rescaled absolute cursor coordinates for both recording and playback relative to whichever monitor was primary at startup - correct on that monitor, wrong on any other with a different scale. Switched to Per-Monitor-DPI-Aware v2 (set before the Tk window is created).
- The DPI-awareness fix above only lived in `macro_app.py`'s own `__main__` block, which never runs when the app is launched through `run_app.pyw` (a separate launcher that imports `MacroApp` as a module) - `run_app.pyw` had its own untouched copy of the old, buggy DPI call. This is why the bug still reproduced through the normal launcher even after the fix. Applied the same Per-Monitor-DPI-Aware v2 fix there too.

### Added
- `diagnose_cursor_jump.py`: a diagnostic build of the app (not a fix) for tracking down a reported bug where the cursor jumps to the wrong monitor sometime after a Ctrl+V paste + wait. Logs live cursor position and the foreground window title to `cursor_jump_log.csv` during playback. A synthetic replay of the affected recording showed no jump introduced by the app itself, so this captures real evidence from an actual reproduction instead.
- **Loop Playback**: the macro can now repeat automatically a fixed number of times, or indefinitely until Stop (F10) is pressed, instead of always playing once. New "Loop Playback" checkbox and repeat-count field in Playback Settings; the status bar shows the current pass (e.g. "Playing (Loop 2/5)").
- **Raw mouse input capture** for games: a new "Raw Input Delta (games / FPS camera)" mouse-tracking mode records and replays relative mouse deltas via the Windows raw input API, for games like Minecraft that read mouse-look from raw HID movement and ignore or clip the absolute OS cursor position. The existing absolute cursor-position tracking mode is kept unchanged for normal desktop app use (clicking, dragging, menus).
