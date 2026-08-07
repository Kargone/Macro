import os
import sys
import tkinter as tk
from macro_app import MacroApp   # or whatever your main file/class is named

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