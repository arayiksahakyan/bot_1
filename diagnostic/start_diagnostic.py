"""Windowed entry point for Start_Diagnostic.exe. No client command line required."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread

if __package__:
    from . import client
    from . import inspect_ui as diagnostic
    from . import inspect_windows as windows
else:
    import client
    import inspect_ui as diagnostic
    import inspect_windows as windows


class DiagnosticApp:
    def __init__(self, root, directory: Path, dpi_status: str):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.directory, self.dpi_status = root, directory, dpi_status
        self.results = Queue()
        self.busy = False
        self.detection = None
        root.title("SPIMEX Diagnostic")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        self.message = tk.StringVar(value=client.PROGRESS)
        ttk.Label(frame, textvariable=self.message, wraplength=480, justify="left", font=("Segoe UI", 12)).pack(anchor="w")
        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=460)
        self.choices = ttk.Combobox(frame, state="readonly", width=62)
        self.choices.bind("<<ComboboxSelected>>", self.choice_changed)
        self.start_button = ttk.Button(frame, text="Start", command=self.start_chosen, state="disabled")
        self.close_button = ttk.Button(frame, text="Close", command=self.close)
        ttk.Label(frame, text=windows.BANNER, wraplength=480, font=("Segoe UI", 8)).pack(anchor="w", pady=(18, 0))
        # No focus_force, lift, topmost, target activation, or modal grab.
        root.after(100, self.begin_detection)

    def begin_detection(self):
        self.background(client.detect_windows, self.detected)

    def background(self, operation, callback):
        self.busy = True
        self.message.set(client.PROGRESS)
        self.progress.pack(pady=(16, 0))
        self.progress.start(12)

        def run():
            try:
                result = operation()
            except Exception as exc:
                result = client.friendly_failure(self.directory, exc)
            self.results.put((callback, result))

        # Tk widgets are accessed only by the main thread. Keep the parent alive
        # until its bounded diagnostic workers finish using the unpacked bundle.
        Thread(target=run, name="diagnostic-collection", daemon=False).start()
        self.root.after(100, self.poll)

    def poll(self):
        try:
            callback, result = self.results.get_nowait()
        except Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.progress.stop()
        self.progress.pack_forget()
        if isinstance(result, client.Outcome) and result.state == "error":
            self.finished(result)
        else:
            callback(result)

    def detected(self, detection: client.Detection):
        self.detection = detection
        if not detection.candidates:
            self.finished(client.Outcome("not_found", client.NOT_FOUND))
        elif detection.automatic_target is not None:
            self.collect(detection.automatic_target)
        else:
            self.message.set("Select the SPIMEX window")
            labels = [f"{index}. {window.title or 'Trading window'}" for index, window in enumerate(detection.candidates, 1)]
            self.choices.configure(values=labels)
            self.choices.pack(pady=(16, 12))
            self.start_button.pack(anchor="e")

    def choice_changed(self, _event=None):
        self.start_button.configure(state="normal" if self.choices.current() >= 0 else "disabled")

    def start_chosen(self):
        if self.busy or self.detection is None or self.choices.current() < 0:
            return
        self.collect(self.detection.target_at(self.choices.current()))

    def collect(self, target):
        self.choices.pack_forget()
        self.start_button.pack_forget()
        self.background(
            lambda: client.collect_for_client(target, self.detection.inventory, self.directory, self.dpi_status),
            self.finished,
        )

    def finished(self, outcome: client.Outcome):
        self.choices.pack_forget()
        self.start_button.pack_forget()
        self.message.set(outcome.message)
        self.close_button.pack(anchor="e", pady=(18, 0))

    def callback_error(self, error_type, error, tb):
        # Tk's default callback handler prints a traceback; replace it completely.
        outcome = client.friendly_failure(self.directory, error.with_traceback(tb))
        if self.busy:
            self.message.set("Please wait while the current collection finishes...")
        else:
            self.finished(outcome)

    def close(self):
        if self.busy:
            self.message.set(client.PROGRESS + "\nPlease wait until collection finishes before closing.")
            return
        self.root.destroy()


def run_gui(directory: Path) -> int:
    import tkinter as tk

    windows.require_runtime()
    dpi_status = windows.enable_dpi_awareness()
    root = tk.Tk()
    DiagnosticApp(root, directory, dpi_status)
    root.mainloop()
    return 0


def show_startup_error(message: str) -> None:
    """Last-resort native dialog if Tk cannot start; no exception text is shown."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        show = ctypes.WinDLL("user32").MessageBoxW
        show.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        show.restype = ctypes.c_int
        show(None, message, "SPIMEX Diagnostic", 0x10)


def dispatch(argv: list[str], directory: Path) -> int:
    # Frozen subprocesses re-enter the executable. Route them before creating Tk
    # so a worker can never open a second GUI or begin a recursive collection.
    if argv and argv[0] == "--diagnostic-worker":
        if len(argv) != 4 or argv[1] not in {"tree", "screenshot"} or argv[2] != "--run-dir":
            raise ValueError("Invalid internal diagnostic worker arguments")
        run_dir = Path(argv[3])
        with (run_dir / client.ERROR_LOG).open("a", encoding="utf-8") as stream:
            previous = sys.stderr
            try:
                sys.stderr = stream
                return diagnostic.worker_main(argv[1], run_dir)
            finally:
                sys.stderr = previous
    if argv:
        raise ValueError("Unexpected diagnostic arguments")
    return run_gui(directory)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    directory = client.application_directory()
    with ExitStack() as handles:
        previous_streams = (sys.stdin, sys.stdout, sys.stderr)
        try:
            # --noconsole gives Python None streams. Provide safe streams before
            # importing pywinauto/comtypes or executing any existing print calls.
            if sys.stdin is None:
                sys.stdin = handles.enter_context(open(os.devnull, "r"))
            if sys.stdout is None:
                sys.stdout = handles.enter_context(open(os.devnull, "w", encoding="utf-8"))
            if sys.stderr is None:
                sys.stderr = handles.enter_context(open(os.devnull, "w", encoding="utf-8"))
            return dispatch(arguments, directory)
        except Exception as exc:
            outcome = client.friendly_failure(directory, exc)
            if "--diagnostic-worker" not in arguments:
                try:
                    show_startup_error(outcome.message)
                except Exception as dialog_error:
                    client.write_error(directory, "Startup dialog failed", dialog_error)
            return 1
        finally:
            sys.stdin, sys.stdout, sys.stderr = previous_streams


if __name__ == "__main__":
    raise SystemExit(main())
