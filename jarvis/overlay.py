"""Overlay — JARVIS v4: the buddy, a small always-on-top
window that floats over the owner's work.

Thoth's buddy idea, built from what Windows Python already
carries: Tkinter ships with the standard Windows install, so
a borderless, topmost, slightly transparent window needs no
new dependency and no native build. It polls the app's own
/api/state (loopback) and shows JARVIS's TRUE state — the
same truthfulness law as the UI: the word it shows is the
work actually happening. Double-click opens the full UI;
right-click quits.

The words and colours are decided by `buddy_text`, a pure
function — tested without a display. The Tk shell is thin
and honest: without tkinter it says so and exits.

Run:  python -m jarvis.overlay [--port 8765]
"""

from __future__ import annotations

import json
import urllib.request

POLL_MS = 1500

_STATES = {
    "listening": ("● Sun raha hoon", "#4ade80"),
    "speaking": ("● Bol raha hoon", "#60a5fa"),
    "thinking": ("● Soch raha hoon", "#facc15"),
    "creating": ("● Bana raha hoon", "#fb923c"),
    "researching": ("● Dhoond raha hoon", "#fb923c"),
    "sending": ("● Bhej raha hoon", "#fb923c"),
    "writing": ("● Likh raha hoon", "#fb923c"),
    "working": ("● Kaam kar raha hoon", "#fb923c"),
    "standby": ("● Standby", "#9ca3af"),
}


def buddy_text(state: dict) -> tuple:
    """(main_line, colour, sub_line) from an /api/state dict.
    Pure: the same state always paints the same buddy."""
    state = state or {}
    voice = state.get("voice") or {}
    if voice.get("active"):
        word = "speaking" if voice.get("speaking") else "listening"
    else:
        word = str(state.get("activity") or "standby").lower()
    line, colour = _STATES.get(word, _STATES["standby"])
    unread = int(state.get("notifications") or 0)
    sub = f"{unread} nayi khabar" if unread else "JARVIS"
    provider = str(state.get("provider") or "")
    if provider:
        sub = f"{sub} · {provider}"
    return line, colour, sub


def _fetch_state(port: int) -> dict:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}))   # loopback: no proxy
    with opener.open(f"http://127.0.0.1:{port}/api/state",
                     timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="jarvis.overlay")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"Buddy overlay nahi khul sakta: tkinter nahi "
              f"mila ({exc}). Python ke Windows install ke "
              "saath ye aata hai — 'Modify > tcl/tk' chala kar "
              "dekho, sir.")
        return 1
    root = tk.Tk()
    root.title("JARVIS buddy")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.92)
    except Exception:
        pass
    root.configure(bg="#0b1220")
    width, height = 250, 66
    x = root.winfo_screenwidth() - width - 18
    root.geometry(f"{width}x{height}+{x}+18")
    main_var = tk.StringVar(value="● Standby")
    sub_var = tk.StringVar(value="JARVIS")
    label = tk.Label(root, textvariable=main_var,
                     fg="#4ade80", bg="#0b1220",
                     font=("Segoe UI", 11, "bold"), anchor="w")
    label.pack(fill="x", padx=10, pady=(8, 0))
    sub = tk.Label(root, textvariable=sub_var, fg="#9ca3af",
                   bg="#0b1220", font=("Segoe UI", 8),
                   anchor="w")
    sub.pack(fill="x", padx=10)
    drag = {"x": 0, "y": 0}

    def _down(event):
        drag["x"], drag["y"] = event.x, event.y

    def _move(event):
        root.geometry(f"+{event.x_root - drag['x']}"
                      f"+{event.y_root - drag['y']}")

    def _open_ui(_event=None):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{args.port}/")

    def _menu(event):
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="Poora JARVIS kholo",
                         command=_open_ui)
        menu.add_command(label="Buddy band karo",
                         command=root.destroy)
        menu.tk_popup(event.x_root, event.y_root)

    for widget in (root, label, sub):
        widget.bind("<Button-1>", _down)
        widget.bind("<B1-Motion>", _move)
        widget.bind("<Double-Button-1>", _open_ui)
        widget.bind("<Button-3>", _menu)

    def _poll():
        try:
            line, colour, subline = buddy_text(
                _fetch_state(args.port))
            main_var.set(line)
            label.configure(fg=colour)
            sub_var.set(subline)
        except Exception:
            main_var.set("● JARVIS band hai")
            label.configure(fg="#9ca3af")
            sub_var.set("UI chalu karo, sir")
        root.after(POLL_MS, _poll)

    _poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
