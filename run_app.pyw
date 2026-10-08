import tkinter as tk
from macro_app import MacroApp, set_dpi_awareness

if __name__ == "__main__":
    # Shared with macro_app.py's own __main__ block, so both entry points
    # always get the same DPI fix.
    set_dpi_awareness()

    root = tk.Tk()
    app = MacroApp(root)
    root.mainloop()
