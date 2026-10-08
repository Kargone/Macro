import os
import sys
import json
import time
import uuid
import ctypes
from ctypes import wintypes
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# Import pynput for global hooks and controller emulation
import pynput


# ==================== RAW MOUSE INPUT (WINDOWS) ====================
# Games with a camera/look control (Minecraft included) hide the cursor and
# read relative HID mouse deltas via the raw input API, ignoring the OS
# cursor's absolute screen position entirely. pynput's mouse listener only
# ever sees the (often clipped/motionless) cursor position, so it cannot
# capture camera-look movement in such games. To support that, we register
# for WM_INPUT messages directly against the app's own window and read the
# raw relative deltas out of each RAWINPUT packet.

GWLP_WNDPROC = -4
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
MOUSE_MOVE_ABSOLUTE = 0x01
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWMOUSE(ctypes.Structure):
    # usButtonFlags/usButtonData live in a ULONG-aligned union in the real
    # Win32 struct, so there are 2 bytes of padding after usFlags.
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("_padding", wintypes.USHORT),
        ("usButtonFlags", wintypes.USHORT),
        ("usButtonData", wintypes.USHORT),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT(ctypes.Structure):
    # MOUSEINPUT is the largest member of the real INPUT union, so a mouse-only
    # struct has the same size (40 bytes on 64-bit) that SendInput checks for.
    _fields_ = [
        ("type", wintypes.DWORD),
        ("mi", MOUSEINPUT),
    ]


# 64-bit-safe signatures. Getting these wrong truncates pointers on 64-bit
# Windows and reliably crashes the whole process, so they are set explicitly
# rather than relying on ctypes' 32-bit default of c_int.
_user32 = ctypes.windll.user32
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_user32.CallWindowProcW.restype = ctypes.c_ssize_t
_user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.RegisterRawInputDevices.restype = wintypes.BOOL
_user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
_user32.GetRawInputData.restype = wintypes.UINT
_user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]

_user32.SendInput.restype = wintypes.UINT
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def send_relative_mouse_move(dx, dy):
    """Inject a relative mouse move through SendInput. Unlike SetCursorPos
    (which pynput's move() uses), this produces real WM_INPUT deltas, which
    is what raw-input games like Minecraft read for camera look."""
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=int(dx), dy=int(dy), dwFlags=MOUSEEVENTF_MOVE))
    if _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise ctypes.WinError()


def set_dpi_awareness():
    """Per-Monitor-V2 DPI awareness keeps absolute cursor coordinates
    consistent across monitors that have different scaling factors. The
    older PROCESS_SYSTEM_DPI_AWARE level interprets/reports coordinates
    relative to whichever monitor was primary at process startup, so on a
    multi-monitor setup with mixed scaling, both the recorded coordinates and
    SetCursorPos-based playback drift off target (or off-screen entirely).
    Must be called before the Tk window is created. Every launcher calls this
    one function so the entry points can't drift apart again."""
    try:
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            raise OSError("SetProcessDpiAwarenessContext failed")
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


class PlaybackController:
    """Manages thread-safe playback of a macro recording session on a sorted absolute timeline."""
    def __init__(self, app):
        self.app = app
        self.mouse_controller = pynput.mouse.Controller()
        self.keyboard_controller = pynput.keyboard.Controller()

        self.playing = False
        self.paused = False

        self.thread = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()  # Unpaused initially

        self.timeline = []
        self.timeline_idx = 0
        self.playback_elapsed_s = 0.0
        self.playback_start_real_time = 0.0
        self.loop_count = 1
        self.loop_iteration = 1

        # Track currently held keys/buttons to prevent sticking on abort/stop
        self.simulated_pressed_keys = set()
        self.simulated_pressed_buttons = set()

        # Temp memory to restore hold states on resume
        self.paused_pressed_keys = []
        self.paused_pressed_buttons = []

        # Fast lookup from event object to index in original list
        self.event_index_map = {}

        # Held while injecting an action and while stop/pause release held
        # inputs, so the playback thread can never press a key just after
        # stop() released everything (leaving it stuck down).
        self.input_lock = threading.Lock()

        # Bumped on every start() so a finished thread's queued stop can't
        # end a newer playback that started in the meantime.
        self.generation = 0

    def start(self, events, speed_multiplier=1.0, loop_count=1):
        if self.playing:
            return

        self.generation += 1
        self.playing = True
        self.paused = False
        self.stop_event.clear()
        self.pause_event.set()

        # loop_count: 1 = play once (default), 0 = loop indefinitely until
        # stopped, N>1 = repeat the full sequence N times total.
        self.loop_count = loop_count
        self.loop_iteration = 1

        self.simulated_pressed_keys.clear()
        self.simulated_pressed_buttons.clear()
        self.paused_pressed_keys = []
        self.paused_pressed_buttons = []

        # Build fast index map for highlighting
        self.event_index_map = {id(ev): i for i, ev in enumerate(events)}

        # 1. Reconstruct the absolute timeline of discrete actions
        # Prefer absolute timestamps for overlap-safe playback. Fall back to legacy interval/hold.
        self.timeline = []
        current_time_s = 0.0
        for ev in events:
            event_type = ev.get("event")
            has_abs_hold = "start_ms" in ev and "end_ms" in ev
            has_abs_move = "time_ms" in ev

            if event_type in ("move", "raw_move"):
                if has_abs_move:
                    start_s = max(0.0, ev.get("time_ms", 0.0) / 1000.0)
                else:
                    interval_s = ev.get("interval_ms", 0.0) / 1000.0
                    # Legacy move scheduling
                    start_s = current_time_s + interval_s

                self.timeline.append({
                    "time": start_s,
                    "action": event_type,
                    "event": ev
                })
                current_time_s = max(current_time_s, start_s)
            else:
                if has_abs_hold:
                    start_s = max(0.0, ev.get("start_ms", 0.0) / 1000.0)
                    end_s = max(start_s, ev.get("end_ms", 0.0) / 1000.0)
                else:
                    interval_s = ev.get("interval_ms", 0.0) / 1000.0
                    hold_s = ev.get("hold_ms", 0.0) / 1000.0
                    start_s = current_time_s + interval_s
                    end_s = start_s + hold_s

                self.timeline.append({
                    "time": start_s,
                    "action": "press",
                    "event": ev
                })
                self.timeline.append({
                    "time": end_s,
                    "action": "release",
                    "event": ev
                })
                current_time_s = max(current_time_s, end_s)

        # Sort actions. Presses occur before releases if timestamps are identical
        self.timeline.sort(key=lambda x: (x["time"], 1 if x["action"] == "release" else 0))

        self.timeline_idx = 0
        self.playback_elapsed_s = 0.0
        self.playback_start_real_time = time.time()

        self.thread = threading.Thread(
            target=self._playback_loop,
            args=(events, speed_multiplier, self.generation),
            daemon=True
        )
        self.thread.start()

    def pause(self):
        if not self.playing or self.paused:
            return
        with self.input_lock:
            self.paused = True
            self.pause_event.clear()
            # Capture precise playback duration elapsed up to this pause event
            self.playback_elapsed_s += (time.time() - self.playback_start_real_time)

            # Capture and release currently held simulated keys/buttons so they don't bleed during pause
            self.paused_pressed_keys = list(self.simulated_pressed_keys)
            self.paused_pressed_buttons = list(self.simulated_pressed_buttons)

            for key in self.paused_pressed_keys:
                try:
                    self.keyboard_controller.release(key)
                    self.simulated_pressed_keys.discard(key)
                except Exception:
                    pass

            for btn in self.paused_pressed_buttons:
                try:
                    self.mouse_controller.release(btn)
                    self.simulated_pressed_buttons.discard(btn)
                except Exception:
                    pass

        self.app.on_playback_paused()

    def resume(self):
        if not self.playing or not self.paused:
            return
        with self.input_lock:
            self.paused = False

            # Restore pre-pause key and mouse hold states
            for key in self.paused_pressed_keys:
                try:
                    self.keyboard_controller.press(key)
                    self.simulated_pressed_keys.add(key)
                except Exception:
                    pass

            for btn in self.paused_pressed_buttons:
                try:
                    self.mouse_controller.press(btn)
                    self.simulated_pressed_buttons.add(btn)
                except Exception:
                    pass

            self.paused_pressed_keys = []
            self.paused_pressed_buttons = []

            self.playback_start_real_time = time.time()
            self.pause_event.set()
        self.app.on_playback_resumed()

    def stop(self):
        if not self.playing:
            return
        with self.input_lock:
            self.playing = False
            self.paused = False
            self.stop_event.set()
            self.pause_event.set()

            # Safe release of any stuck keys/mouse buttons
            for key in list(self.simulated_pressed_keys):
                try:
                    self.keyboard_controller.release(key)
                except Exception:
                    pass
            self.simulated_pressed_keys.clear()

            for btn in list(self.simulated_pressed_buttons):
                try:
                    self.mouse_controller.release(btn)
                except Exception:
                    pass
            self.simulated_pressed_buttons.clear()

            self.paused_pressed_keys = []
            self.paused_pressed_buttons = []

        self.app.on_playback_stopped()

    def _finish(self, generation):
        # Queued by the playback thread when it ends; ignore it if a newer
        # playback has already been started.
        if generation == self.generation:
            self.stop()

    def _playback_loop(self, events, speed_multiplier, generation):
        n_actions = len(self.timeline)

        while True:
            while self.timeline_idx < n_actions and not self.stop_event.is_set():
                if self.paused:
                    self.pause_event.wait()
                    if self.stop_event.is_set():
                        break
                    continue

                action = self.timeline[self.timeline_idx]
                scheduled_time_s = action["time"] / speed_multiplier

                # Calculate current total elapsed time (accumulated before pause + current session)
                real_elapsed = time.time() - self.playback_start_real_time
                total_elapsed_s = self.playback_elapsed_s + real_elapsed

                remaining_s = scheduled_time_s - total_elapsed_s

                if remaining_s > 0:
                    # Hybrid sleep: coarse sleep, then busy-wait for sub-ms precision
                    if remaining_s > 0.01:
                        time.sleep(0.01)
                    else:
                        while remaining_s > 0 and not self.stop_event.is_set() and not self.paused:
                            real_elapsed = time.time() - self.playback_start_real_time
                            total_elapsed_s = self.playback_elapsed_s + real_elapsed
                            remaining_s = scheduled_time_s - total_elapsed_s
                    continue

                # Execute action. Re-check stop/pause under the lock: either
                # may have fired while we were waiting, and pressing now would
                # leave the input held after they released everything.
                with self.input_lock:
                    if self.stop_event.is_set():
                        break
                    if self.paused:
                        continue
                    self._execute_action(action)
                    self.timeline_idx += 1

                # Highlight event row in GUI using precomputed index
                try:
                    ev_id = id(action["event"])
                    orig_idx = self.event_index_map.get(ev_id, None)
                    if orig_idx is not None:
                        self.app.root.after(0, self.app.highlight_event, orig_idx)
                except Exception:
                    pass

            if self.stop_event.is_set() or n_actions == 0:
                break

            # Completed one full pass. Loop again if the user asked to repeat.
            if self.loop_count == 1 or (self.loop_count != 0 and self.loop_iteration >= self.loop_count):
                break

            self.loop_iteration += 1
            self.timeline_idx = 0
            self.playback_elapsed_s = 0.0
            self.playback_start_real_time = time.time()
            self.app.root.after(0, self.app.on_loop_iteration)

        # Finished or interrupted
        self.app.root.after(0, self._finish, generation)

    def _resolve_key_obj(self, key_val, is_special):
        if is_special:
            if isinstance(key_val, str) and key_val.startswith("vk_"):
                # Combos with no printable char (e.g. Ctrl+<digit>) are recorded
                # by raw virtual-key code rather than a Key enum name.
                vk = int(key_val.split("_", 1)[1])
                return pynput.keyboard.KeyCode(vk=vk)
            return getattr(pynput.keyboard.Key, key_val)
        return key_val

    def _execute_action(self, action):
        act_type = action["action"]
        event = action["event"]

        if act_type == "press":
            event_type = event.get("event")
            if event_type == "key":
                key_val = event.get("key")
                is_special = event.get("special", False)
                try:
                    key_obj = self._resolve_key_obj(key_val, is_special)
                    self.keyboard_controller.press(key_obj)
                    self.simulated_pressed_keys.add(key_obj)
                except Exception as e:
                    print(f"Error pressing key {key_val}: {e}")
            elif event_type == "click":
                btn_name = event.get("button", "left")
                x = event.get("x", 0.0)
                y = event.get("y", 0.0)
                try:
                    btn_obj = getattr(pynput.mouse.Button, btn_name)
                    self.mouse_controller.position = (x, y)
                    self.mouse_controller.press(btn_obj)
                    self.simulated_pressed_buttons.add(btn_obj)
                except Exception as e:
                    print(f"Error pressing click {btn_name}: {e}")

        elif act_type == "release":
            event_type = event.get("event")
            if event_type == "key":
                key_val = event.get("key")
                is_special = event.get("special", False)
                try:
                    key_obj = self._resolve_key_obj(key_val, is_special)
                    self.keyboard_controller.release(key_obj)
                    self.simulated_pressed_keys.discard(key_obj)
                except Exception as e:
                    print(f"Error releasing key {key_val}: {e}")
            elif event_type == "click":
                btn_name = event.get("button", "left")
                try:
                    btn_obj = getattr(pynput.mouse.Button, btn_name)
                    self.mouse_controller.release(btn_obj)
                    self.simulated_pressed_buttons.discard(btn_obj)
                except Exception as e:
                    print(f"Error releasing click {btn_name}: {e}")

        elif act_type == "move":
            x = event.get("x", 0.0)
            y = event.get("y", 0.0)
            try:
                self.mouse_controller.position = (x, y)
            except Exception as e:
                print(f"Error moving mouse: {e}")

        elif act_type == "raw_move":
            dx = event.get("dx", 0)
            dy = event.get("dy", 0)
            try:
                # Relative SendInput move (not pynput's move(), which is just
                # SetCursorPos) so it feeds into the raw input pipeline the
                # way mouse-look games (e.g. Minecraft) expect.
                send_relative_mouse_move(dx, dy)
            except Exception as e:
                print(f"Error applying raw mouse delta: {e}")


class MacroApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Desktop Macro Recorder & Player")
        self.root.geometry("960x640")
        self.root.minsize(800, 500)

        self.recordings_file = os.path.join(os.path.dirname(__file__), "macro_recordings.json")
        self.sessions_data = {"sessions": []}
        self.current_session = None
        self.recording_active = False
        # True during the short delay between pressing Record and the hooks
        # actually starting, so a second F9 can't start recording twice.
        self.recording_pending = False

        # Recording callbacks run on pynput's hook threads while stopping runs
        # on the Tk thread, so all recording state is guarded by this lock.
        self.record_lock = threading.Lock()

        # Recording temporary states
        self.recorded_events = []
        self.recording_start_time = None
        self.last_event_time = None
        self.pressed_keys = {}
        self.pressed_mouse = {}
        self.last_move_time = 0
        self.last_move_pos = None

        # Mouse Listener
        self.mouse_listener = None

        # Raw mouse input state (for game/FPS camera capture). Deltas that
        # arrive between samples are summed here rather than dropped.
        self.last_raw_move_time = 0
        self.pending_raw_dx = 0
        self.pending_raw_dy = 0

        # Event table row currently highlighted during playback
        self.highlighted_event_item = None

        # Playback Controller
        self.playback_controller = PlaybackController(self)

        self.load_data()
        self.setup_styles()
        self._init_raw_mouse_input()
        self.create_widgets()

        # Start permanent global keyboard listener
        self._start_global_listener()

        if self.sessions_data["sessions"]:
            self.select_session(self.sessions_data["sessions"][0]["id"])

        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)

    # ==================== RAW MOUSE INPUT (GAME CAPTURE) ====================

    def _init_raw_mouse_input(self):
        """Subclass this window's WNDPROC to intercept WM_INPUT so we can read
        raw relative mouse deltas - the same channel FPS-style games read for
        camera look - instead of only the OS cursor's absolute position."""
        self.raw_mouse_input_available = False
        self._raw_wndproc_ref = None
        self._raw_old_wndproc = None
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()

            self._raw_wndproc_ref = WNDPROC(self._wnd_proc)
            old_wndproc = _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._raw_wndproc_ref)
            if not old_wndproc:
                raise ctypes.WinError(ctypes.get_last_error())
            self._raw_old_wndproc = old_wndproc

            device = RAWINPUTDEVICE(
                usUsagePage=HID_USAGE_PAGE_GENERIC,
                usUsage=HID_USAGE_GENERIC_MOUSE,
                dwFlags=RIDEV_INPUTSINK,
                hwndTarget=hwnd,
            )
            if not _user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device)):
                raise ctypes.WinError(ctypes.get_last_error())

            self.raw_mouse_input_available = True
        except Exception as e:
            print(f"Raw mouse input unavailable: {e}")
            # Restore the original wndproc if we managed to install ours
            # before something else failed.
            if self._raw_old_wndproc:
                try:
                    _user32.SetWindowLongPtrW(self.root.winfo_id(), GWLP_WNDPROC, self._raw_old_wndproc)
                except Exception:
                    pass
            self._raw_old_wndproc = None
            self._raw_wndproc_ref = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        # This runs inside Tkinter's native message loop. It must never raise
        # or block, and must always forward to the original wndproc, or the
        # whole GUI (not just recording) will misbehave or hang.
        if msg == WM_INPUT:
            try:
                self._handle_raw_input(lparam)
            except Exception:
                pass
        return _user32.CallWindowProcW(self._raw_old_wndproc, hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        size = wintypes.UINT(0)
        _user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            return

        buf = ctypes.create_string_buffer(size.value)
        copied = _user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if copied != size.value:
            return

        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEMOUSE:
            return
        # Remote Desktop, VMs and pen tablets report absolute positions
        # (0-65535) here instead of relative deltas. Treating those as deltas
        # would record huge camera jumps, so skip them.
        if raw.mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
            return

        dx, dy = raw.mouse.lLastX, raw.mouse.lLastY
        if dx or dy:
            self._on_raw_mouse_delta(dx, dy)

    def _on_raw_mouse_delta(self, dx, dy):
        if self.mouse_move_mode.get() != "raw":
            return

        with self.record_lock:
            if not self.recording_active:
                return

            self.pending_raw_dx += dx
            self.pending_raw_dy += dy

            now = time.time()
            # Cap sampling rate to 125 Hz to keep recordings a sane size. The
            # deltas in between are accumulated, not dropped, so the total
            # movement still matches what the mouse actually did.
            if now - self.last_raw_move_time < 0.008:
                return
            self._flush_raw_move(now)

    def _flush_raw_move(self, now):
        # Caller must hold record_lock.
        if not (self.pending_raw_dx or self.pending_raw_dy):
            return
        self.last_raw_move_time = now

        interval = (now - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0
        move_time_ms = 0.0
        if self.recording_start_time is not None:
            move_time_ms = max(0.0, (now - self.recording_start_time) * 1000.0)

        self.recorded_events.append({
            "event": "raw_move",
            "dx": int(self.pending_raw_dx),
            "dy": int(self.pending_raw_dy),
            "interval_ms": interval,
            "time_ms": move_time_ms
        })
        self.pending_raw_dx = 0
        self.pending_raw_dy = 0
        self.last_event_time = now

    def on_app_close(self):
        # Restore the original wndproc before Tk tears the window down, so no
        # WM_INPUT can ever call back into a Python callback after shutdown
        # starts.
        if self._raw_old_wndproc:
            try:
                _user32.SetWindowLongPtrW(self.root.winfo_id(), GWLP_WNDPROC, self._raw_old_wndproc)
            except Exception:
                pass
        self.root.destroy()

    def _normalize_key(self, key):
        """Return a stable (is_special, key_name) identity for matching key press/release pairs."""
        is_special = not hasattr(key, 'char') or key.char is None
        if is_special:
            # Key enum members (arrows, ctrl, f-keys, etc.) always have `.name`.
            # But some combos - e.g. Ctrl+<digit>, dead keys, some Alt combos -
            # report a plain KeyCode with char=None instead, which has no
            # `.name` attribute. Accessing key.name there used to raise
            # AttributeError, which pynput propagates out of the listener
            # thread and silently kills the *entire* global hotkey/recording
            # listener for the rest of the app's life. Fall back to the raw
            # virtual-key code instead of crashing.
            key_name = getattr(key, 'name', None)
            if key_name is None:
                vk = getattr(key, 'vk', None)
                key_name = f"vk_{vk}" if vk is not None else "unknown"
        else:
            key_name = key.char
            if isinstance(key_name, str) and len(key_name) == 1 and 0 < ord(key_name) < 27:
                # Holding Ctrl turns the letter key's reported char into a control
                # character (e.g. Ctrl+V -> '\x16') instead of the literal letter.
                # Recover the letter so playback re-presses it while Ctrl is held,
                # instead of injecting a raw control character that most apps ignore.
                key_name = chr(ord(key_name) + 96)
        return is_special, key_name

    def _key_identity_token(self, key):
        """Build a stable token for key press/release matching across layout/modifier variance."""
        is_special, key_name = self._normalize_key(key)
        if is_special:
            return ("special", key_name)

        vk = getattr(key, 'vk', None)
        if vk is not None:
            return ("vk", int(vk))

        if isinstance(key_name, str):
            return ("char", key_name.lower())

        return ("char", key_name)

    def load_data(self):
        if os.path.exists(self.recordings_file):
            try:
                with open(self.recordings_file, 'r', encoding='utf-8') as f:
                    self.sessions_data = json.load(f)
            except Exception as e:
                # Move the unreadable file aside before starting fresh, so the
                # next save can't overwrite (and permanently lose) it.
                backup_path = f"{self.recordings_file}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                try:
                    os.replace(self.recordings_file, backup_path)
                    backup_note = f"The unreadable file was kept as:\n{backup_path}"
                except OSError:
                    backup_note = "The unreadable file could not be backed up."
                messagebox.showerror("Error", f"Failed to load recordings: {e}\n\n{backup_note}\n\nStarting with an empty list.")
                self.sessions_data = {"sessions": []}
        else:
            self.sessions_data = {"sessions": []}
            self.save_data()

    def save_data(self):
        # Write to a temp file and swap it in, so a crash mid-save leaves the
        # previous recordings file intact instead of half-written.
        tmp_path = self.recordings_file + ".tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self.sessions_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.recordings_file)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save recordings: {e}")

    def setup_styles(self):
        # Configure a dark-themed visual design
        self.style = ttk.Style()
        self.style.theme_use('clam')

        # Dark color palette
        self.bg_primary = '#1e1e1e'
        self.bg_secondary = '#252526'
        self.bg_tertiary = '#2d2d30'
        self.fg_primary = '#f5f5f5'
        self.fg_secondary = '#aaaaaa'
        self.accent_blue = '#007acc'
        self.accent_hover = '#1c97ea'
        self.btn_green = '#47a447'
        self.btn_green_hover = '#5cb85c'
        self.btn_red = '#d2322d'
        self.btn_red_hover = '#d9534f'

        # Universal styles
        self.style.configure('.', background=self.bg_primary, foreground=self.fg_primary, font=('Segoe UI', 10))
        self.style.configure('TFrame', background=self.bg_primary)
        self.style.configure('Sidebar.TFrame', background=self.bg_secondary)
        self.style.configure('Header.TFrame', background=self.bg_secondary)

        self.style.configure('TLabel', background=self.bg_primary, foreground=self.fg_primary)
        self.style.configure('Sidebar.TLabel', background=self.bg_secondary, foreground=self.fg_primary)
        self.style.configure('Header.TLabel', background=self.bg_secondary, foreground=self.fg_primary, font=('Segoe UI', 12, 'bold'))

        # Standard Buttons
        self.style.configure('TButton', background=self.bg_tertiary, foreground=self.fg_primary, borderwidth=0, padding=(10, 5), focuscolor='none')
        self.style.map('TButton', background=[('active', '#4e4e50')])

        # Specialized Buttons
        self.style.configure('Primary.TButton', background=self.accent_blue, foreground='#ffffff', borderwidth=0)
        self.style.map('Primary.TButton', background=[('active', self.accent_hover)])

        self.style.configure('Success.TButton', background=self.btn_green, foreground='#ffffff', borderwidth=0)
        self.style.map('Success.TButton', background=[('active', self.btn_green_hover)])

        self.style.configure('Danger.TButton', background=self.btn_red, foreground='#ffffff', borderwidth=0)
        self.style.map('Danger.TButton', background=[('active', self.btn_red_hover)])

        # Checkbutton
        self.style.configure('TCheckbutton', background=self.bg_primary, foreground=self.fg_primary)
        self.style.map('TCheckbutton', background=[('active', self.bg_primary)])

        # Radiobutton
        self.style.configure('TRadiobutton', background=self.bg_primary, foreground=self.fg_primary)
        self.style.map('TRadiobutton', background=[('active', self.bg_primary)])

        # Input fields (white field background, so use black text)
        for input_style in ('TEntry', 'TSpinbox', 'TCombobox'):
            self.style.configure(input_style, foreground='#000000', fieldbackground='#ffffff', insertcolor='#000000')

        # Treeview (Table)
        self.style.configure('Treeview',background=self.bg_secondary, foreground=self.fg_primary, fieldbackground=self.bg_secondary, rowheight=24, borderwidth=0)
        self.style.map('Treeview', background=[('selected', self.accent_blue)], foreground=[('selected', '#ffffff')])
        self.style.configure('Treeview.Heading', background=self.bg_tertiary, foreground=self.fg_primary, relief='flat', font=('Segoe UI', 9, 'bold'))
        self.style.map('Treeview.Heading', background=[('active', '#4e4e50')])

    def create_widgets(self):
        # Configure Grid structure
        self.root.columnconfigure(0, weight=1, minsize=250)
        self.root.columnconfigure(1, weight=3, minsize=550)
        self.root.rowconfigure(0, weight=1)

        # ==================== SIDEBAR ====================
        sidebar = ttk.Frame(self.root, style='Sidebar.TFrame')
        sidebar.grid(row=0, column=0, sticky='nsew')
        sidebar.rowconfigure(2, weight=1)
        sidebar.columnconfigure(0, weight=1)

        # Sidebar Header
        sidebar_header = ttk.Frame(sidebar, style='Sidebar.TFrame', padding=10)
        sidebar_header.grid(row=0, column=0, sticky='ew')
        ttk.Label(sidebar_header, text="SAVED SESSIONS", style='Sidebar.TLabel', font=('Segoe UI', 11, 'bold')).pack(anchor='w')

        # Sidebar Action Buttons
        sidebar_buttons = ttk.Frame(sidebar, style='Sidebar.TFrame', padding=(10, 0, 10, 10))
        sidebar_buttons.grid(row=1, column=0, sticky='ew')

        btn_new = ttk.Button(sidebar_buttons, text="+ New Session", command=self.on_new_session)
        btn_new.pack(fill='x', pady=2)

        # Session Treeview (List)
        self.sessions_list = ttk.Treeview(sidebar, columns=('count',), show='tree', selectmode='browse')
        self.sessions_list.grid(row=2, column=0, sticky='nsew', padx=10, pady=5)
        self.sessions_list.bind('<<TreeviewSelect>>', self.on_session_list_select)

        # Scrollbar for List
        list_scroll = ttk.Scrollbar(sidebar, orient='vertical', command=self.sessions_list.yview)
        list_scroll.grid(row=2, column=1, sticky='ns', pady=5)
        self.sessions_list.configure(yscrollcommand=list_scroll.set)

        # Populate session list
        self.refresh_session_list()

        # ==================== MAIN CONTENT ====================
        main_content = ttk.Frame(self.root, padding=15)
        main_content.grid(row=0, column=1, sticky='nsew')
        main_content.columnconfigure(0, weight=1)
        main_content.rowconfigure(2, weight=1)

        # Header Area
        header_frame = ttk.Frame(main_content, style='Header.TFrame', padding=10)
        header_frame.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        header_frame.columnconfigure(0, weight=1)

        self.session_title_var = tk.StringVar(value="Select or Create a Session")
        lbl_title = ttk.Label(header_frame, textvariable=self.session_title_var, style='Header.TLabel')
        lbl_title.grid(row=0, column=0, sticky='w')

        # Header edit / delete buttons
        header_actions = ttk.Frame(header_frame, style='Sidebar.TFrame')
        header_actions.grid(row=0, column=1, sticky='e')

        self.btn_rename_session = ttk.Button(header_actions, text="Rename", command=self.on_rename_session)
        self.btn_rename_session.pack(side='left', padx=2)
        self.btn_delete_session = ttk.Button(header_actions, text="Delete", command=self.on_delete_session)
        self.btn_delete_session.pack(side='left', padx=2)

        # Dashboard & Controls Area
        controls_card = ttk.Frame(main_content, padding=10)
        controls_card.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        controls_card.columnconfigure(0, weight=1)
        controls_card.columnconfigure(1, weight=1)

        # Recording Options
        recording_options_frame = ttk.LabelFrame(controls_card, text=" Recording Settings ", padding=10)
        recording_options_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 5))

        self.opt_record_keys = tk.BooleanVar(value=True)
        self.opt_record_clicks = tk.BooleanVar(value=True)
        self.mouse_move_mode = tk.StringVar(value="off")
        self.opt_minimize_on_record = tk.BooleanVar(value=True)

        ttk.Checkbutton(recording_options_frame, text="Log Keyboard Keys", variable=self.opt_record_keys).pack(anchor='w', pady=2)
        ttk.Checkbutton(recording_options_frame, text="Log Mouse Clicks", variable=self.opt_record_clicks).pack(anchor='w', pady=2)

        ttk.Label(recording_options_frame, text="Mouse Movement Tracking:").pack(anchor='w', pady=(8, 0))
        ttk.Radiobutton(recording_options_frame, text="Off", variable=self.mouse_move_mode, value="off").pack(anchor='w')
        ttk.Radiobutton(recording_options_frame, text="Cursor Position (desktop apps)", variable=self.mouse_move_mode, value="cursor").pack(anchor='w')
        ttk.Radiobutton(recording_options_frame, text="Raw Input Delta (games / FPS camera)", variable=self.mouse_move_mode, value="raw", state='normal' if self.raw_mouse_input_available else 'disabled').pack(anchor='w')
        if not self.raw_mouse_input_available:
            ttk.Label(recording_options_frame, text="(Raw input hook unavailable on this system)", foreground=self.fg_secondary).pack(anchor='w')

        ttk.Checkbutton(recording_options_frame, text="Minimize Window on Record", variable=self.opt_minimize_on_record).pack(anchor='w', pady=(8, 2))

        # Playback Settings
        playback_options_frame = ttk.LabelFrame(controls_card, text=" Playback Settings ", padding=10)
        playback_options_frame.grid(row=0, column=1, sticky='nsew', padx=(5, 0))

        ttk.Label(playback_options_frame, text="Playback Speed Multiplier (x):").pack(anchor='w', pady=2)

        # No "x" suffix in the format: the spinbox text has to stay a plain
        # number or speed_var.get() fails and Play stops working.
        self.speed_var = tk.DoubleVar(value=1.0)
        speed_spin = ttk.Spinbox(playback_options_frame, from_=0.1, to=10.0, increment=0.1, format="%.1f", textvariable=self.speed_var, width=10)
        speed_spin.pack(anchor='w', pady=2)

        ttk.Separator(playback_options_frame, orient='horizontal').pack(fill='x', pady=8)

        self.opt_loop_playback = tk.BooleanVar(value=False)
        ttk.Checkbutton(playback_options_frame, text="Loop Playback", variable=self.opt_loop_playback).pack(anchor='w', pady=2)

        loop_row = ttk.Frame(playback_options_frame)
        loop_row.pack(anchor='w', fill='x', pady=2)
        ttk.Label(loop_row, text="Repeat Count (0 = infinite):").pack(side='left')
        self.loop_count_var = tk.IntVar(value=0)
        ttk.Spinbox(loop_row, from_=0, to=9999, increment=1, textvariable=self.loop_count_var, width=6).pack(side='left', padx=(5, 0))

        # Main Controller Buttons (Rec, Stop, Play, Pause, Clear)
        buttons_frame = ttk.Frame(main_content, padding=10)
        buttons_frame.grid(row=2, column=0, sticky='ew')

        self.btn_record = ttk.Button(buttons_frame, text="⏺ Record (F9)", style='Success.TButton', command=self.on_start_recording, width=15)
        self.btn_record.pack(side='left', padx=4)

        self.btn_stop = ttk.Button(buttons_frame, text="⏹ Stop (F10)", style='Danger.TButton', command=self.on_stop, width=15)
        self.btn_stop.pack(side='left', padx=4)
        self.btn_stop.configure(state='disabled')

        self.btn_play = ttk.Button(buttons_frame, text="▶ Play (F8)", style='Primary.TButton', command=self.on_play, width=15)
        self.btn_play.pack(side='left', padx=4)

        self.btn_pause = ttk.Button(buttons_frame, text="⏸ Pause (F7)", command=self.on_pause, width=15)
        self.btn_pause.pack(side='left', padx=4)
        self.btn_pause.configure(state='disabled')

        self.status_var = tk.StringVar(value="Status: Idle")
        lbl_status = ttk.Label(buttons_frame, textvariable=self.status_var, font=('Segoe UI', 10, 'bold'), padding=(15, 0))
        lbl_status.pack(side='left')

        # Event Table / Editor Frame
        table_frame = ttk.LabelFrame(main_content, text=" Recorded Event Sequences (F9: Rec | F10: Stop | F8: Play | F7: Pause/Resume) ", padding=10)
        table_frame.grid(row=3, column=0, sticky='nsew', pady=(10, 0))
        main_content.rowconfigure(3, weight=1)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # Event Treeview
        columns = ('idx', 'type', 'details', 'interval', 'hold')
        self.event_tree = ttk.Treeview(table_frame, columns=columns, show='headings', selectmode='browse')

        self.event_tree.heading('idx', text='#')
        self.event_tree.heading('type', text='Event')
        self.event_tree.heading('details', text='Details')
        self.event_tree.heading('interval', text='Interval (ms)')
        self.event_tree.heading('hold', text='Hold (ms)')

        self.event_tree.column('idx', width=50, minwidth=40, anchor='center')
        self.event_tree.column('type', width=100, minwidth=80, anchor='center')
        self.event_tree.column('details', width=220, minwidth=150, anchor='w')
        self.event_tree.column('interval', width=110, minwidth=100, anchor='e')
        self.event_tree.column('hold', width=110, minwidth=100, anchor='e')

        self.event_tree.grid(row=0, column=0, sticky='nsew')
        self.event_tree.tag_configure('playing', background='#104e8b', foreground='#ffffff')

        # Scrollbars for table
        tbl_scroll_y = ttk.Scrollbar(table_frame, orient='vertical', command=self.event_tree.yview)
        tbl_scroll_y.grid(row=0, column=1, sticky='ns')
        self.event_tree.configure(yscrollcommand=tbl_scroll_y.set)

        # Table Editor Buttons (Delete Event, Clear All)
        editor_buttons = ttk.Frame(table_frame, padding=(0, 5, 0, 0))
        editor_buttons.grid(row=1, column=0, sticky='ew')

        self.btn_delete_event = ttk.Button(editor_buttons, text="Delete Selected Event", command=self.on_delete_event)
        self.btn_delete_event.pack(side='left', padx=2)

        self.btn_clear_events = ttk.Button(editor_buttons, text="Clear All Events", command=self.on_clear_events)
        self.btn_clear_events.pack(side='left', padx=2)

        self.update_ui_state()

    # ==================== GLOBAL HOTKEY LISTENER ====================

    def _start_global_listener(self):
        # Starts a permanent keyboard listener running for the entire lifecycle of the application
        self.global_keyboard_listener = pynput.keyboard.Listener(
            on_press=self._global_on_press,
            on_release=self._global_on_release
        )
        self.global_keyboard_listener.daemon = True
        self.global_keyboard_listener.start()

    def _global_on_press(self, key):
        # This callback runs inside pynput's global low-level keyboard hook
        # thread, on EVERY keystroke made anywhere on the system for the
        # entire lifetime of the app. If anything raises here, pynput kills
        # the whole listener permanently (it is only started once, in
        # __init__) - silently disabling F7-F10 and recording capture for the
        # rest of the session with no visible error. Never let an exception
        # escape this method.
        try:
            self._handle_global_on_press(key)
        except Exception as e:
            print(f"Error in global key handler: {e}")

    def _handle_global_on_press(self, key):
        # Tiny yield to avoid starving other threads over long runs
        time.sleep(0.0001)

        # A: Active Recording State
        if self.recording_active:
            if key == pynput.keyboard.Key.f10:
                # F10 stops recording
                self.root.after(0, self.on_stop)
                return

            # Record keyboard key press
            self._on_key_press(key)

        # B: Active Playback State
        elif self.playback_controller.playing:
            if key == pynput.keyboard.Key.f10:
                # F10 aborts playback
                self.root.after(0, self.playback_controller.stop)
                return
            elif key == pynput.keyboard.Key.f7:
                # F7 pauses/resumes playback
                self.root.after(0, self.on_pause)
                return

        # C: Idle Application State (Hotkeys can trigger recording/playback)
        else:
            if key == pynput.keyboard.Key.f9:
                # F9 starts recording
                self.root.after(0, self.on_start_recording)
            elif key == pynput.keyboard.Key.f8:
                # F8 replays the active macro
                self.root.after(0, self.on_play)

    def _global_on_release(self, key):
        try:
            self._handle_global_on_release(key)
        except Exception as e:
            print(f"Error in global key release handler: {e}")

    def _handle_global_on_release(self, key):
        if self.recording_active:
            self._on_key_release(key)

    # ==================== EVENT HANDLERS & NAVIGATION ====================

    def refresh_session_list(self):
        # Clear list
        for item in self.sessions_list.get_children():
            self.sessions_list.delete(item)

        # Add sessions
        for sess in self.sessions_data.get("sessions", []):
            count = sess.get("event_count", 0)
            self.sessions_list.insert('', 'end', iid=sess["id"], text=sess["name"], values=(f"{count} events",))

    def on_session_list_select(self, event):
        selected = self.sessions_list.selection()
        if selected:
            self.select_session(selected[0])

    def select_session(self, session_id):
        session = next((s for s in self.sessions_data["sessions"] if s["id"] == session_id), None)
        if session:
            self.current_session = session
            self.session_title_var.set(session["name"])
            self.refresh_event_table()

            # Select item in sidebar list without causing infinite loops
            if self.sessions_list.selection() != (session_id,):
                self.sessions_list.selection_set(session_id)
                self.sessions_list.see(session_id)

            self.update_ui_state()

    def refresh_event_table(self):
        # Clear table
        self.highlighted_event_item = None
        for item in self.event_tree.get_children():
            self.event_tree.delete(item)

        if not self.current_session:
            return

        # Fill table with events
        for idx, ev in enumerate(self.current_session.get("events", [])):
            ev_type = ev.get("event", "").capitalize()

            # Format Details
            if ev_type == "Key":
                details = f"Key '{ev.get('key')}'" + (" (Special)" if ev.get("special") else "")
            elif ev_type == "Click":
                details = f"{ev.get('button').upper()} Click at ({int(ev.get('x', 0))}, {int(ev.get('y', 0))})"
            elif ev_type == "Move":
                details = f"Move to ({int(ev.get('x', 0))}, {int(ev.get('y', 0))})"
            elif ev_type == "Raw_move":
                ev_type = "Raw Move"
                details = f"Raw delta ({int(ev.get('dx', 0))}, {int(ev.get('dy', 0))})"
            else:
                details = "-"

            interval_str = f"{ev.get('interval_ms', 0.0):.2f}"
            hold_str = f"{ev.get('hold_ms', 0.0):.2f}" if "hold_ms" in ev else "-"

            self.event_tree.insert('', 'end', iid=f"ev_{idx}", values=(idx + 1, ev_type, details, interval_str, hold_str))

    def update_ui_state(self):
        has_session = self.current_session is not None
        current_events = []
        if has_session and isinstance(self.current_session, dict):
            current_events = self.current_session.get("events", [])
        has_events = len(current_events) > 0
        is_playing = self.playback_controller.playing
        is_paused = self.playback_controller.paused

        if self.recording_active:
            self.btn_record.configure(state='disabled')
            self.btn_stop.configure(state='normal')
            self.btn_play.configure(state='disabled')
            self.btn_pause.configure(state='disabled')
            self.btn_rename_session.configure(state='disabled')
            self.btn_delete_session.configure(state='disabled')
            self.btn_delete_event.configure(state='disabled')
            self.btn_clear_events.configure(state='disabled')
            self.sessions_list.configure(selectmode='none')
        elif is_playing:
            self.btn_record.configure(state='disabled')
            self.btn_stop.configure(state='normal')
            self.btn_play.configure(state='disabled')
            self.btn_pause.configure(state='normal')
            self.btn_pause.configure(text="⏸ Pause (F7)" if not is_paused else "▶ Resume (F7)")
            self.btn_rename_session.configure(state='disabled')
            self.btn_delete_session.configure(state='disabled')
            self.btn_delete_event.configure(state='disabled')
            self.btn_clear_events.configure(state='disabled')
            self.sessions_list.configure(selectmode='none')
        else:
            self.btn_record.configure(state='normal' if has_session else 'disabled')
            self.btn_stop.configure(state='disabled')
            self.btn_play.configure(state='normal' if has_events else 'disabled')
            self.btn_pause.configure(state='disabled')
            self.btn_pause.configure(text="⏸ Pause (F7)")
            self.btn_rename_session.configure(state='normal' if has_session else 'disabled')
            self.btn_delete_session.configure(state='normal' if has_session else 'disabled')
            self.btn_delete_event.configure(state='normal' if has_events else 'disabled')
            self.btn_clear_events.configure(state='normal' if has_events else 'disabled')
            self.sessions_list.configure(selectmode='browse')

    # ==================== SESSION OPERATIONS ====================

    def on_new_session(self):
        name = simpledialog.askstring("New Session", "Enter a name for the new session:")
        if not name:
            return

        new_id = str(uuid.uuid4())[:8]
        new_session = {
            "id": new_id,
            "name": name,
            "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event_count": 0,
            "events": []
        }
        self.sessions_data["sessions"].append(new_session)
        self.save_data()
        self.refresh_session_list()
        self.select_session(new_id)

    def on_rename_session(self):
        if not self.current_session:
            return

        new_name = simpledialog.askstring("Rename Session", "Enter new name for the session:", initialvalue=self.current_session["name"])
        if not new_name:
            return

        self.current_session["name"] = new_name
        self.save_data()
        self.refresh_session_list()
        self.select_session(self.current_session["id"])

    def on_delete_session(self):
        if not self.current_session:
            return

        confirm = messagebox.askyesno("Confirm Delete", f"Are you sure you want to delete session '{self.current_session['name']}'?")
        if not confirm:
            return

        idx = next((i for i, s in enumerate(self.sessions_data["sessions"]) if s["id"] == self.current_session["id"]), -1)
        if idx != -1:
            self.sessions_data["sessions"].pop(idx)
            self.save_data()
            self.current_session = None
            self.refresh_session_list()

            if self.sessions_data["sessions"]:
                self.select_session(self.sessions_data["sessions"][0]["id"])
            else:
                self.session_title_var.set("Select or Create a Session")
                self.refresh_event_table()
                self.update_ui_state()

    # ==================== RECORDING LOGIC ====================

    def on_start_recording(self):
        if not self.current_session or self.recording_active or self.recording_pending or self.playback_controller.playing:
            return
        self.recording_pending = True

        # Optional window minimization to keep screen clean
        if self.opt_minimize_on_record.get():
            self.root.iconify()

        # Small delay to let OS window transitions complete
        self.root.after(500, self._start_hooks)

    def _start_hooks(self):
        self.recording_pending = False

        with self.record_lock:
            self.recorded_events = []
            self.recording_start_time = time.time()
            self.last_event_time = time.time()  # Start interval calculation now
            self.pressed_keys.clear()
            self.pressed_mouse.clear()
            self.last_move_time = 0
            self.last_move_pos = None
            self.last_raw_move_time = 0
            self.pending_raw_dx = 0
            self.pending_raw_dy = 0
            self.recording_active = True

        self.status_var.set("Recording (F10 to stop)")
        self.update_ui_state()

        track_cursor = self.mouse_move_mode.get() == "cursor"

        # Start mouse hook if enabled. Raw-delta tracking doesn't need this
        # listener - it's fed independently by the WM_INPUT window hook.
        if self.opt_record_clicks.get() or track_cursor:
            self.mouse_listener = pynput.mouse.Listener(
                on_click=self._on_mouse_click if self.opt_record_clicks.get() else None,
                on_move=self._on_mouse_move if track_cursor else None
            )
            self.mouse_listener.start()

    def _on_key_press(self, key):
        # Exclude control hotkeys from being logged
        is_special, key_name = self._normalize_key(key)

        if key_name in ['f7', 'f8', 'f9', 'f10']:
            return

        if not self.opt_record_keys.get():
            return

        key_token = self._key_identity_token(key)
        with self.record_lock:
            if not self.recording_active:
                return

            # Prevent repeat press events from OS
            if key_token in self.pressed_keys:
                return

            now = time.time()
            self.pressed_keys[key_token] = {
                "press_time": now,
                "is_special": is_special,
                "key_name": key_name,
            }

    def _on_key_release(self, key):
        # Exclude control hotkeys from being logged
        is_special, key_name = self._normalize_key(key)

        if key_name in ['f7', 'f8', 'f9', 'f10']:
            return

        if not self.opt_record_keys.get():
            return

        key_token = self._key_identity_token(key)
        with self.record_lock:
            if not self.recording_active:
                return
            self._record_key_release(key_token, is_special, key_name)

    def _record_key_release(self, key_token, is_special, key_name):
        # Caller must hold record_lock.
        # Fallback for environments where release key char case differs from press key char.
        if key_token not in self.pressed_keys and not is_special and isinstance(key_name, str):
            key_token = ("char", key_name.lower())

        if key_token in self.pressed_keys:
            pressed_info = self.pressed_keys.pop(key_token)
            press_time = pressed_info["press_time"]
            is_special = pressed_info["is_special"]
            key_name = pressed_info["key_name"]
            now = time.time()
            hold = (now - press_time) * 1000.0
            start_ms = 0.0
            end_ms = 0.0
            if self.recording_start_time is not None:
                start_ms = max(0.0, (press_time - self.recording_start_time) * 1000.0)
                end_ms = max(start_ms, (now - self.recording_start_time) * 1000.0)

            # Calculate overlapping interval relative to last event's release
            interval = (press_time - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0

            self.recorded_events.append({
                "event": "key",
                "key": key_name,
                "special": is_special,
                "hold_ms": hold,
                "interval_ms": interval,
                "start_ms": start_ms,
                "end_ms": end_ms
            })
            self.last_event_time = now

    def _on_mouse_click(self, x, y, button, pressed):
        with self.record_lock:
            if self.recording_active:
                self._record_mouse_click(x, y, button, pressed)

    def _record_mouse_click(self, x, y, button, pressed):
        # Caller must hold record_lock.
        now = time.time()
        if pressed:
            self.pressed_mouse[button] = (now, x, y)
        else:
            if button in self.pressed_mouse:
                press_time, px, py = self.pressed_mouse.pop(button)
                hold = (now - press_time) * 1000.0
                start_ms = 0.0
                end_ms = 0.0
                if self.recording_start_time is not None:
                    start_ms = max(0.0, (press_time - self.recording_start_time) * 1000.0)
                    end_ms = max(start_ms, (now - self.recording_start_time) * 1000.0)

                # Calculate overlapping interval relative to last event's release
                interval = (press_time - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0

                self.recorded_events.append({
                    "event": "click",
                    "button": button.name,
                    "x": float(px),
                    "y": float(py),
                    "hold_ms": hold,
                    "interval_ms": interval,
                    "start_ms": start_ms,
                    "end_ms": end_ms
                })
                self.last_event_time = now

    def _on_mouse_move(self, x, y):
        with self.record_lock:
            if self.recording_active:
                self._record_mouse_move(x, y)

    def _record_mouse_move(self, x, y):
        # Caller must hold record_lock.
        now = time.time()
        # Cap sampling rate to 50 Hz (20ms interval) to avoid massive log files and filter minimal vibrations
        if now - self.last_move_time >= 0.02:
            if self.last_move_pos is None or (abs(x - self.last_move_pos[0]) > 3 or abs(y - self.last_move_pos[1]) > 3):
                interval = (now - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0
                move_time_ms = 0.0
                if self.recording_start_time is not None:
                    move_time_ms = max(0.0, (now - self.recording_start_time) * 1000.0)

                self.recorded_events.append({
                    "event": "move",
                    "x": float(x),
                    "y": float(y),
                    "interval_ms": interval,
                    "time_ms": move_time_ms
                })
                self.last_event_time = now
                self.last_move_time = now
                self.last_move_pos = (x, y)

    # ==================== STOPPING / PLAYBACK ACTIONS ====================

    def on_stop(self):
        # Triggered by F10 hotkey or Stop button
        if self.recording_active:
            self._stop_recording()
        elif self.playback_controller.playing:
            self.playback_controller.stop()

    def _stop_recording(self):
        # Flip the flag and finalize under the lock, so no hook-thread
        # callback can modify pressed_keys/recorded_events while we read them.
        with self.record_lock:
            self.recording_active = False

            # Record any raw mouse movement still waiting to be sampled
            now = time.time()
            self._flush_raw_move(now)

            # Clean up any leftover keys that were pressed during recording but not released before stopping
            for pressed_info in self.pressed_keys.values():
                press_time = pressed_info["press_time"]
                hold = (now - press_time) * 1000.0
                is_special = pressed_info["is_special"]
                key_name = pressed_info["key_name"]
                start_ms = 0.0
                end_ms = 0.0
                if self.recording_start_time is not None:
                    start_ms = max(0.0, (press_time - self.recording_start_time) * 1000.0)
                    end_ms = max(start_ms, (now - self.recording_start_time) * 1000.0)

                # Exclude control hotkeys from the recording list
                if key_name not in ['f7', 'f8', 'f9', 'f10']:
                    interval = (press_time - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0
                    self.recorded_events.append({
                        "event": "key",
                        "key": key_name,
                        "special": is_special,
                        "hold_ms": hold,
                        "interval_ms": interval,
                        "start_ms": start_ms,
                        "end_ms": end_ms
                    })
                    self.last_event_time = now

            self.pressed_keys.clear()
            self.pressed_mouse.clear()

        # Stop Mouse Listener
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
            except Exception:
                pass
            self.mouse_listener = None

        if not self.current_session:
            self.status_var.set("Status: Idle")
            self.update_ui_state()
            return

        # Append new events to current session
        self.current_session["events"].extend(self.recorded_events)
        self.current_session["event_count"] = len(self.current_session["events"])
        self.current_session["recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # Save and refresh
        self.save_data()
        self.refresh_session_list()
        self.refresh_event_table()

        # Restore and focus window
        if self.opt_minimize_on_record.get():
            self.root.deiconify()
            self.root.focus_force()

        self.status_var.set("Status: Idle")
        self.update_ui_state()
        messagebox.showinfo("Recording Finished", f"Successfully recorded {len(self.recorded_events)} events!")

    def on_play(self):
        if (not self.current_session or not self.current_session.get("events")
                or self.playback_controller.playing or self.recording_active or self.recording_pending):
            return

        speed = self._read_speed()
        if speed is None:
            return

        loop_count = 1
        if self.opt_loop_playback.get():
            loop_count = self._read_loop_count()
            if loop_count is None:
                return

        self.playback_controller.start(
            self.current_session["events"],
            speed_multiplier=speed,
            loop_count=loop_count
        )
        self.status_var.set("Playing" + self._loop_status_suffix())
        self.update_ui_state()

    def _read_speed(self):
        """Return the speed multiplier clamped to 0.1-10, or None (after
        telling the user) if the box doesn't hold a positive number."""
        try:
            speed = float(self.speed_var.get())
        except (tk.TclError, ValueError):
            speed = 0.0
        if not speed > 0:
            messagebox.showerror("Invalid Speed", "Playback speed must be a number greater than 0 (for example 1.0).")
            self.speed_var.set(1.0)
            return None
        speed = min(max(speed, 0.1), 10.0)
        self.speed_var.set(round(speed, 2))
        return speed

    def _read_loop_count(self):
        """Return the repeat count (0 = infinite), or None (after telling the
        user) if the box doesn't hold a whole number."""
        try:
            count = int(self.loop_count_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Invalid Repeat Count", "Repeat count must be a whole number (0 = infinite).")
            self.loop_count_var.set(0)
            return None
        count = max(0, count)
        self.loop_count_var.set(count)
        return count

    def _loop_status_suffix(self):
        pc = self.playback_controller
        if pc.loop_count == 1:
            return ""
        if pc.loop_count == 0:
            return f" (Loop {pc.loop_iteration})"
        return f" (Loop {pc.loop_iteration}/{pc.loop_count})"

    def on_loop_iteration(self):
        self.status_var.set("Playing" + self._loop_status_suffix())

    def on_pause(self):
        if self.playback_controller.playing:
            if self.playback_controller.paused:
                self.playback_controller.resume()
            else:
                self.playback_controller.pause()

    def on_playback_paused(self):
        self.status_var.set("Paused" + self._loop_status_suffix())
        self.update_ui_state()

    def on_playback_resumed(self):
        self.status_var.set("Playing" + self._loop_status_suffix())
        self.update_ui_state()

    def on_playback_stopped(self):
        self.status_var.set("Status: Idle")
        # Clear Treeview highlighter
        self._clear_event_highlight()

        self.update_ui_state()

    def _clear_event_highlight(self):
        item = self.highlighted_event_item
        self.highlighted_event_item = None
        if item and self.event_tree.exists(item):
            self.event_tree.item(item, tags=())

    def highlight_event(self, idx):
        # Highlight current playing event row. Only the previous and new rows
        # are touched, so long macros don't re-tag every row per action.
        if not self.playback_controller.playing:
            return
        item_id = f"ev_{idx}"
        if item_id == self.highlighted_event_item or not self.event_tree.exists(item_id):
            return

        self._clear_event_highlight()
        self.event_tree.item(item_id, tags=('playing',))
        self.highlighted_event_item = item_id
        self.event_tree.see(item_id)

    # ==================== TABLE / EDITOR EVENTS ====================

    def on_delete_event(self):
        selected = self.event_tree.selection()
        if not selected or not self.current_session:
            return

        # Parse index from row iid
        row_id = selected[0]
        idx = int(row_id.split('_')[1])

        # Confirm and remove
        self.current_session["events"].pop(idx)
        self.current_session["event_count"] = len(self.current_session["events"])

        self.save_data()
        self.refresh_session_list()

        # Re-select same session to refresh view
        self.select_session(self.current_session["id"])

    def on_clear_events(self):
        if not self.current_session:
            return

        confirm = messagebox.askyesno("Confirm Clear", "Are you sure you want to clear all recorded events from this session?")
        if not confirm:
            return

        self.current_session["events"] = []
        self.current_session["event_count"] = 0

        self.save_data()
        self.refresh_session_list()
        self.select_session(self.current_session["id"])


if __name__ == "__main__":
    set_dpi_awareness()

    root = tk.Tk()
    app = MacroApp(root)
    root.mainloop()