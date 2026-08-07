import os
import sys
import json
import time
import uuid
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# Import pynput for global hooks and controller emulation
import pynput


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

        # Track currently held keys/buttons to prevent sticking on abort/stop
        self.simulated_pressed_keys = set()
        self.simulated_pressed_buttons = set()

        # Temp memory to restore hold states on resume
        self.paused_pressed_keys = []
        self.paused_pressed_buttons = []

        # Fast lookup from event object to index in original list
        self.event_index_map = {}

    def start(self, events, speed_multiplier=1.0):
        if self.playing:
            return

        self.playing = True
        self.paused = False
        self.stop_event.clear()
        self.pause_event.set()

        self.simulated_pressed_keys.clear()
        self.simulated_pressed_buttons.clear()
        self.paused_pressed_keys = []
        self.paused_pressed_buttons = []

        # Build fast index map for highlighting
        self.event_index_map = {id(ev): i for i, ev in enumerate(events)}

        # 1. Reconstruct the absolute timeline of discrete actions
        self.timeline = []
        current_time_s = 0.0
        for ev in events:
            interval_s = ev.get("interval_ms", 0.0) / 1000.0
            event_type = ev.get("event")

            if event_type == "move":
                # Instanstaneous move event
                start_s = current_time_s + interval_s
                self.timeline.append({
                    "time": start_s,
                    "action": "move",
                    "event": ev
                })
                current_time_s = start_s
            else:
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
                current_time_s = end_s

        # Sort actions. Presses occur before releases if timestamps are identical
        self.timeline.sort(key=lambda x: (x["time"], 1 if x["action"] == "release" else 0))

        self.timeline_idx = 0
        self.playback_elapsed_s = 0.0
        self.playback_start_real_time = time.time()

        self.thread = threading.Thread(
            target=self._playback_loop,
            args=(events, speed_multiplier),
            daemon=True
        )
        self.thread.start()

    def pause(self):
        if not self.playing or self.paused:
            return
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

        self.pause_event.set()
        self.playback_start_real_time = time.time()
        self.app.on_playback_resumed()

    def stop(self):
        if not self.playing:
            return
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

    def _playback_loop(self, events, speed_multiplier):
        n_actions = len(self.timeline)

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

            # Execute action
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

        # Finished or interrupted
        self.app.root.after(0, self.stop)

    def _execute_action(self, action):
        act_type = action["action"]
        event = action["event"]

        if act_type == "press":
            event_type = event.get("event")
            if event_type == "key":
                key_val = event.get("key")
                is_special = event.get("special", False)
                try:
                    key_obj = getattr(pynput.keyboard.Key, key_val) if is_special else key_val
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
                    key_obj = getattr(pynput.keyboard.Key, key_val) if is_special else key_val
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

        # Recording temporary states
        self.recorded_events = []
        self.last_event_time = None
        self.pressed_keys = {}
        self.pressed_mouse = {}
        self.last_move_time = 0
        self.last_move_pos = None

        # Mouse Listener
        self.mouse_listener = None

        # Playback Controller
        self.playback_controller = PlaybackController(self)

        self.load_data()
        self.setup_styles()
        self.create_widgets()

        # Start permanent global keyboard listener
        self._start_global_listener()

        if self.sessions_data["sessions"]:
            self.select_session(self.sessions_data["sessions"][0]["id"])

    def load_data(self):
        if os.path.exists(self.recordings_file):
            try:
                with open(self.recordings_file, 'r', encoding='utf-8') as f:
                    self.sessions_data = json.load(f)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load recordings: {e}\nCreating a new empty file.")
                self.sessions_data = {"sessions": []}
        else:
            self.sessions_data = {"sessions": []}
            self.save_data()

    def save_data(self):
        try:
            with open(self.recordings_file, 'w', encoding='utf-8') as f:
                json.dump(self.sessions_data, f, indent=2)
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

        # Treeview (Table)
        self.style.configure('Treeview', background=self.bg_secondary, foreground=self.fg_primary, fieldbackground=self.bg_secondary, rowheight=24, borderwidth=0)
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
        self.opt_record_movement = tk.BooleanVar(value=False)
        self.opt_minimize_on_record = tk.BooleanVar(value=True)

        ttk.Checkbutton(recording_options_frame, text="Log Keyboard Keys", variable=self.opt_record_keys).pack(anchor='w', pady=2)
        ttk.Checkbutton(recording_options_frame, text="Log Mouse Clicks", variable=self.opt_record_clicks).pack(anchor='w', pady=2)
        ttk.Checkbutton(recording_options_frame, text="Track Mouse Movement (Raw)", variable=self.opt_record_movement).pack(anchor='w', pady=2)
        ttk.Checkbutton(recording_options_frame, text="Minimize Window on Record", variable=self.opt_minimize_on_record).pack(anchor='w', pady=2)

        # Playback Settings
        playback_options_frame = ttk.LabelFrame(controls_card, text=" Playback Settings ", padding=10)
        playback_options_frame.grid(row=0, column=1, sticky='nsew', padx=(5, 0))

        ttk.Label(playback_options_frame, text="Playback Speed Multiplier:").pack(anchor='w', pady=2)

        self.speed_var = tk.DoubleVar(value=1.0)
        speed_spin = ttk.Spinbox(playback_options_frame, from_=0.1, to=10.0, increment=0.1, format="%.1fx", textvariable=self.speed_var, width=10)
        speed_spin.pack(anchor='w', pady=2)

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
        # Tiny yield to avoid starving other threads over long runs
        time.sleep(0.0001)

        # Determine the key identifier
        is_special = not hasattr(key, 'char') or key.char is None
        key_name = key.name if is_special else key.char

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
            else:
                details = "-"

            interval_str = f"{ev.get('interval_ms', 0.0):.2f}"
            hold_str = f"{ev.get('hold_ms', 0.0):.2f}" if "hold_ms" in ev else "-"

            self.event_tree.insert('', 'end', iid=f"ev_{idx}", values=(idx + 1, ev_type, details, interval_str, hold_str))

    def update_ui_state(self):
        has_session = self.current_session is not None
        has_events = has_session and len(self.current_session.get("events", [])) > 0
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
        if not self.current_session or self.recording_active:
            return

        # Optional window minimization to keep screen clean
        if self.opt_minimize_on_record.get():
            self.root.iconify()

        # Small delay to let OS window transitions complete
        self.root.after(500, self._start_hooks)

    def _start_hooks(self):
        self.recording_active = True
        self.status_var.set("Recording (F10 to stop)")
        self.update_ui_state()

        self.recorded_events = []
        self.last_event_time = time.time()  # Start interval calculation now
        self.pressed_keys.clear()
        self.pressed_mouse.clear()
        self.last_move_time = 0
        self.last_move_pos = None

        # Start mouse hook if enabled
        if self.opt_record_clicks.get() or self.opt_record_movement.get():
            self.mouse_listener = pynput.mouse.Listener(
                on_click=self._on_mouse_click if self.opt_record_clicks.get() else None,
                on_move=self._on_mouse_move if self.opt_record_movement.get() else None
            )
            self.mouse_listener.start()

    def _on_key_press(self, key):
        # Exclude control hotkeys from being logged
        is_special = not hasattr(key, 'char') or key.char is None
        key_name = key.name if is_special else key.char

        if key_name in ['f7', 'f8', 'f9', 'f10']:
            return

        if not self.opt_record_keys.get():
            return

        # Prevent repeat press events from OS
        if key in self.pressed_keys:
            return

        now = time.time()
        self.pressed_keys[key] = now

    def _on_key_release(self, key):
        # Exclude control hotkeys from being logged
        is_special = not hasattr(key, 'char') or key.char is None
        key_name = key.name if is_special else key.char

        if key_name in ['f7', 'f8', 'f9', 'f10']:
            return

        if not self.recording_active or not self.opt_record_keys.get():
            return

        if key in self.pressed_keys:
            press_time = self.pressed_keys.pop(key)
            now = time.time()
            hold = (now - press_time) * 1000.0

            # Calculate overlapping interval relative to last event's release
            interval = (press_time - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0

            self.recorded_events.append({
                "event": "key",
                "key": key_name,
                "special": is_special,
                "hold_ms": hold,
                "interval_ms": interval
            })
            self.last_event_time = now

    def _on_mouse_click(self, x, y, button, pressed):
        if not self.recording_active:
            return

        now = time.time()
        if pressed:
            self.pressed_mouse[button] = (now, x, y)
        else:
            if button in self.pressed_mouse:
                press_time, px, py = self.pressed_mouse.pop(button)
                hold = (now - press_time) * 1000.0

                # Calculate overlapping interval relative to last event's release
                interval = (press_time - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0

                self.recorded_events.append({
                    "event": "click",
                    "button": button.name,
                    "x": float(px),
                    "y": float(py),
                    "hold_ms": hold,
                    "interval_ms": interval
                })
                self.last_event_time = now

    def _on_mouse_move(self, x, y):
        if not self.recording_active:
            return

        now = time.time()
        # Cap sampling rate to 50 Hz (20ms interval) to avoid massive log files and filter minimal vibrations
        if now - self.last_move_time >= 0.02:
            if self.last_move_pos is None or (abs(x - self.last_move_pos[0]) > 3 or abs(y - self.last_move_pos[1]) > 3):
                interval = (now - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0

                self.recorded_events.append({
                    "event": "move",
                    "x": float(x),
                    "y": float(y),
                    "interval_ms": interval
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
        self.recording_active = False

        # Stop Mouse Listener
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
            except Exception:
                pass
            self.mouse_listener = None

        # Clean up any leftover keys that were pressed during recording but not released before stopping
        now = time.time()
        for key, press_time in self.pressed_keys.items():
            hold = (now - press_time) * 1000.0
            is_special = not hasattr(key, 'char') or key.char is None
            key_name = key.name if is_special else key.char

            # Exclude control hotkeys from the recording list
            if key_name not in ['f7', 'f8', 'f9', 'f10']:
                interval = (press_time - self.last_event_time) * 1000.0 if self.last_event_time is not None else 0.0
                self.recorded_events.append({
                    "event": "key",
                    "key": key_name,
                    "special": is_special,
                    "hold_ms": hold,
                    "interval_ms": interval
                })
                self.last_event_time = now

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
        if not self.current_session or not self.current_session.get("events") or self.playback_controller.playing:
            return

        self.status_var.set("Playing")
        self.update_ui_state()
        self.playback_controller.start(
            self.current_session["events"],
            speed_multiplier=self.speed_var.get()
        )

    def on_pause(self):
        if self.playback_controller.playing:
            if self.playback_controller.paused:
                self.playback_controller.resume()
            else:
                self.playback_controller.pause()

    def on_playback_paused(self):
        self.status_var.set("Paused")
        self.update_ui_state()

    def on_playback_resumed(self):
        self.status_var.set("Playing")
        self.update_ui_state()

    def on_playback_stopped(self):
        self.status_var.set("Status: Idle")
        # Clear Treeview highlighters
        for item in self.event_tree.get_children():
            self.event_tree.item(item, tags=())

        self.update_ui_state()

    def highlight_event(self, idx):
        # Highlight current playing event row
        item_id = f"ev_{idx}"
        if self.event_tree.exists(item_id):
            # Remove highlighters from others
            for item in self.event_tree.get_children():
                if item == item_id:
                    self.event_tree.item(item, tags=('playing',))
                else:
                    self.event_tree.item(item, tags=())

            self.event_tree.tag_configure('playing', background='#104e8b', foreground='#ffffff')
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
    # Ensure Tkinter runs cleanly on high DPI screens
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    app = MacroApp(root)
    root.mainloop()