from __future__ import annotations

import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import ttk


APP_NAME = "Neo RAG-Anything"
URL = "http://127.0.0.1:7860"


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def run_server() -> None:
    from webui_server import main

    main()


class Launcher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        icon_path = resource_path("art/werewolf-icon.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass
        self.geometry("460x260")
        self.resizable(False, False)
        self.process: subprocess.Popen | None = None
        self.status = tk.StringVar(value="Stopped")
        self._build_ui()
        self.after(1000, self._poll)

    def _build_ui(self) -> None:
        self.configure(bg="#080d16")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#080d16")
        style.configure("TLabel", background="#080d16", foreground="#d6deeb", font=("Segoe UI", 10))
        style.configure("Title.TLabel", foreground="#7ee787", font=("Segoe UI", 18, "bold"))
        style.configure("Status.TLabel", foreground="#56d4dd", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=8, font=("Segoe UI", 10, "bold"))

        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="Local web GUI service controller").pack(anchor="w", pady=(2, 18))

        status_row = ttk.Frame(root)
        status_row.pack(fill="x", pady=(0, 16))
        ttk.Label(status_row, text="Status:").pack(side="left")
        ttk.Label(status_row, textvariable=self.status, style="Status.TLabel").pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(root)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Start", command=self.start_server).pack(side="left", expand=True, fill="x", padx=(0, 8))
        ttk.Button(buttons, text="Restart", command=self.restart_server).pack(side="left", expand=True, fill="x", padx=8)
        ttk.Button(buttons, text="Stop", command=self.stop_server).pack(side="left", expand=True, fill="x", padx=8)
        ttk.Button(buttons, text="Open GUI", command=self.open_gui).pack(side="left", expand=True, fill="x", padx=(8, 0))

        ttk.Label(root, text=URL).pack(anchor="w", pady=(18, 0))
        self.protocol("WM_DELETE_WINDOW", self.close)

    def start_server(self) -> None:
        if self.process and self.process.poll() is None:
            self.status.set("Running")
            self.open_gui()
            return
        args = [sys.executable, "--server"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).with_name("neo_rag_launcher.py")), "--server"]
        self.process = subprocess.Popen(args, cwd=str(Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent))
        self.status.set("Starting...")
        threading.Thread(target=self._open_when_ready, daemon=True).start()

    def restart_server(self) -> None:
        self.stop_server()
        self.after(900, self.start_server)

    def stop_server(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.status.set("Stopped")

    def open_gui(self) -> None:
        webbrowser.open(URL)

    def close(self) -> None:
        self.stop_server()
        self.destroy()

    def _open_when_ready(self) -> None:
        for _ in range(60):
            if service_ready():
                self.status.set("Running")
                self.open_gui()
                return
            time.sleep(1)
        self.status.set("Starting or busy")

    def _poll(self) -> None:
        if self.process and self.process.poll() is None:
            if self.status.get() not in {"Starting...", "Starting or busy"}:
                self.status.set("Running")
        elif self.status.get() == "Running":
            self.status.set("Stopped")
        self.after(1000, self._poll)


def service_ready() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> None:
    if "--server" in sys.argv:
        run_server()
        return
    Launcher().mainloop()


if __name__ == "__main__":
    main()
