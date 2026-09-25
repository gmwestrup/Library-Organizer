#!/usr/bin/env python3
"""
Library Organizer - Windows launcher.

Starts the SAME web app the Docker edition runs, locally on your PC
(http://127.0.0.1:8765), and opens it in your browser. One codebase, one
interface, every feature. Close the small status window to stop.

Needs Python 3.9+ and the packages in requirements.txt
(double-click install-requirements.bat once).
"""
import os
import sys
import time
import threading
import webbrowser
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
PORT = int(os.environ.get("PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"


def _msg(title, text, error=False):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, text)
        root.destroy()
    except Exception:
        print(text)


missing = [m for m in ("flask", "mutagen", "waitress") if importlib.util.find_spec(m) is None]
if missing:
    _msg("Library Organizer - setup needed",
         "These Python packages are missing:  " + ", ".join(missing) +
         "\n\nDouble-click install-requirements.bat (or run:\n"
         "    pip install -r requirements.txt\nin a Command Prompt), then start me again.",
         error=True)
    sys.exit(1)

import app as webapp   # noqa: E402  (the shared web app)


def serve():
    from waitress import serve as _serve
    _serve(webapp.app, host="127.0.0.1", port=PORT, threads=8)


threading.Thread(target=serve, daemon=True).start()
time.sleep(0.8)
webbrowser.open(URL)

# A tiny status window: closing it stops the server.
try:
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("Library Organizer")
    root.geometry("440x170")
    root.resizable(False, False)
    ttk.Label(root, text=f"Library Organizer v{webapp.VERSION} is running.",
              font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
    ttk.Label(root, text=f"It opened in your browser at  {URL}").pack()
    ttk.Label(root, text="If it didn't, click below. Close this window to stop.").pack(pady=(2, 8))
    ttk.Button(root, text="Open in browser", command=lambda: webbrowser.open(URL)).pack()
    root.mainloop()
except Exception:
    print(f"Running at {URL} - press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
