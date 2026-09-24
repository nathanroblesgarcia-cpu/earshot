"""The "Record this call?" pop-up. Runs as its own process so Tk never shares a thread
with the tray icon. Exit code 0 = Record, 1 = Not now (or it timed out).

Usage: pythonw popup.py <app name> <timeout seconds> [who the call is with] [prep sheet url]
"""
import sys
import tkinter as tk
import webbrowser

BG, FG, MUTED, RED = "#1a1f25", "#e6e8eb", "#9aa3ad", "#dc2626"


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else "A call app"
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    who = sys.argv[3] if len(sys.argv) > 3 else ""
    prep = sys.argv[4] if len(sys.argv) > 4 else ""
    answer = {"code": 1}

    root = tk.Tk()
    root.overrideredirect(True)          # no title bar: a small card, not a window
    root.attributes("-topmost", True)
    root.configure(bg=BG, highlightthickness=1, highlightbackground="#2a3038")

    def done(code):
        answer["code"] = code
        root.destroy()

    frame = tk.Frame(root, bg=BG, padx=20, pady=16)
    frame.pack()
    heading = f"{app} call with {who}" if who else f"{app} is using your mic"
    tk.Label(frame, text=heading, bg=BG, fg=FG, font=("Segoe UI", 13, "bold")).pack(anchor="w")
    tk.Label(frame, text="Record this call with Earshot?", bg=BG, fg=MUTED,
             font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 12))
    buttons = tk.Frame(frame, bg=BG)
    buttons.pack(anchor="e")
    if prep:  # opens the prep sheet and leaves the pop-up up, so Record is still one tap
        tk.Button(buttons, text="Prep sheet", command=lambda: webbrowser.open(prep), bg=BG, fg=FG,
                  relief="flat", activebackground="#2a3038", activeforeground=FG,
                  font=("Segoe UI", 11), padx=14, pady=6, cursor="hand2").pack(side="left", padx=(0, 8))
    tk.Button(buttons, text="Not now", command=lambda: done(1), bg=BG, fg=FG, relief="flat",
              activebackground="#2a3038", activeforeground=FG, font=("Segoe UI", 11),
              padx=14, pady=6, cursor="hand2").pack(side="left", padx=(0, 8))
    tk.Button(buttons, text="●  Record", command=lambda: done(0), bg=RED, fg="white",
              relief="flat", activebackground="#b91c1c", activeforeground="white",
              font=("Segoe UI", 11, "bold"), padx=16, pady=6, cursor="hand2").pack(side="left")

    # Bottom-right corner, above the taskbar.
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{root.winfo_screenwidth() - w - 24}+{root.winfo_screenheight() - h - 72}")
    root.bind("<Escape>", lambda e: done(1))
    root.bind("<Return>", lambda e: done(0))
    root.after(timeout * 1000, lambda: done(1))
    root.focus_force()
    root.mainloop()
    sys.exit(answer["code"])


if __name__ == "__main__":
    main()
