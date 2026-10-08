"""
Diagnostic build of the macro app: logs live cursor position and the
foreground window title during playback to cursor_jump_log.csv, so we can
pinpoint exactly when/why the cursor jumps to the wrong monitor after a
Ctrl+V paste.

Usage: run this instead of macro_app.py, play the "Scripture Notes" session
for real (with your actual target app focused), and reproduce the jump once.
Then close the app and share/inspect cursor_jump_log.csv - it will show the
cursor position and whatever window had focus at that exact moment.
"""

import os
import csv
import time
import ctypes
import threading
import importlib.util
from ctypes import wintypes

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
except Exception:
    pass

import tkinter as tk

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("macro_app", os.path.join(_here, "macro_app.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

LOG_PATH = os.path.join(_here, "cursor_jump_log.csv")
user32 = ctypes.windll.user32


def get_foreground_title():
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


class DiagnosticApp(mod.MacroApp):
    def __init__(self, root):
        super().__init__(root)
        self._log_file = open(LOG_PATH, "w", newline="", encoding="utf-8")
        self._log_writer = csv.writer(self._log_file)
        self._log_writer.writerow(["t_seconds", "x", "y", "timeline_idx", "foreground_window"])
        self._logging = False

    def on_play(self):
        super().on_play()
        if self.playback_controller.playing and not self._logging:
            self._logging = True
            threading.Thread(target=self._log_loop, daemon=True).start()

    def _log_loop(self):
        pt = wintypes.POINT()
        t0 = time.time()
        last_pos = None
        while self.playback_controller.playing:
            user32.GetCursorPos(ctypes.byref(pt))
            pos = (pt.x, pt.y)
            if pos != last_pos:
                fg = get_foreground_title()
                self._log_writer.writerow([round(time.time() - t0, 4), pos[0], pos[1], self.playback_controller.timeline_idx, fg])
                self._log_file.flush()
                last_pos = pos
            time.sleep(0.004)
        self._logging = False

    def on_app_close(self):
        try:
            self._log_file.close()
        except Exception:
            pass
        super().on_app_close()


if __name__ == "__main__":
    root = tk.Tk()
    app = DiagnosticApp(root)
    root.mainloop()
