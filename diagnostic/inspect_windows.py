"""Read-only Windows inventory; also used by inspect_ui.py (Python 3.12)."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys

BANNER = "READ-ONLY DIAGNOSTIC - NO ACTIONS WERE PERFORMED"
BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class WindowRecord:
    hwnd: int
    title: str
    process_name: str
    pid: int
    class_name: str
    created: float | None


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def require_runtime() -> None:
    if sys.platform != "win32":
        raise RuntimeError("This diagnostic requires an authorized Windows computer.")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Python 3.12 is required. Run run_diagnostic.bat.")


def user32():
    """Explicit pointer-sized signatures work with both 32- and 64-bit Python."""
    dll = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "SetProcessDpiAwarenessContext": ([wintypes.HANDLE], wintypes.BOOL),
        "GetDpiForWindow": ([wintypes.HWND], wintypes.UINT),
        "GetDpiForSystem": ([], wintypes.UINT),
        "PrintWindow": ([wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes = arguments
        function.restype = result
    return dll


def enable_dpi_awareness() -> str:
    """Only configures this diagnostic process; never changes display settings."""
    try:
        if user32().SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "Per-monitor V2 (diagnostic process only)"
        return f"Existing process DPI context retained (WinError {ctypes.get_last_error()})"
    except (AttributeError, OSError) as exc:
        return f"DPI awareness unavailable: {exc}"


def quoted(value) -> str:
    # Escape newlines/control characters in application-supplied titles.
    return json.dumps(value, ensure_ascii=False)


def enumerate_windows() -> list[WindowRecord]:
    import psutil
    import win32gui
    import win32process

    records = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)
        except win32gui.error:
            # A window may disappear while EnumWindows is running.
            return True
        process_name, created = "<unavailable>", None
        try:
            process = psutil.Process(pid)
            created = process.create_time()
            process_name = process.name()
        except (psutil.Error, OSError) as exc:
            process_name = f"<unavailable: {type(exc).__name__}>"
        records.append(WindowRecord(int(hwnd), title, process_name, pid, class_name, created))
        return True

    win32gui.EnumWindows(visit, None)
    return sorted(records, key=lambda item: (item.title.casefold(), item.pid, item.hwnd))


def window_line(index: int, window: WindowRecord) -> str:
    return (
        f"[{index}] title={quoted(window.title)} | process={quoted(window.process_name)}"
        f" | PID={window.pid} | class={quoted(window.class_name)} | HWND=0x{window.hwnd:X}"
    )


def save_windows(windows: list[WindowRecord], path: Path) -> None:
    lines = [BANNER, "Visible top-level windows (including untitled/disabled windows):"]
    lines.extend(window_line(index, window) for index, window in enumerate(windows, 1))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)


def choose_window(windows: list[WindowRecord]) -> WindowRecord | None:
    """Input belongs to this console; no keyboard input is sent to applications."""
    if not windows:
        print("No visible windows were found.")
        return None
    while True:
        try:
            answer = input("\nEnter the target window number here, or Q to quit: ").strip()
        except EOFError:
            return None
        if answer.casefold() == "q":
            return None
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(windows):
            return windows[index - 1]
        print(f"Enter a number from 1 to {len(windows)}, or Q.")


def validate_target(target: WindowRecord) -> None:
    """Fail closed if the selected window disappeared or its process changed."""
    import psutil
    import win32gui
    import win32process

    if target.hwnd <= 0 or target.pid <= 0 or target.created is None:
        raise RuntimeError("Cannot verify this window's process identity; inspection stopped.")
    if not win32gui.IsWindow(target.hwnd):
        raise RuntimeError("The selected window has closed. Run the diagnostic again.")
    _, pid = win32process.GetWindowThreadProcessId(target.hwnd)
    if pid != target.pid or psutil.Process(pid).create_time() != target.created:
        raise RuntimeError("The selected window's process identity changed; inspection stopped.")
    if win32gui.GetClassName(target.hwnd) != target.class_name:
        raise RuntimeError("The selected window's class changed; inspection stopped.")
    if not win32gui.IsWindowVisible(target.hwnd):
        raise RuntimeError("The selected window is no longer visible; inspection stopped.")


def new_run_directory(parent: Path = BASE_DIR / "results") -> Path:
    path = parent / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path.mkdir(parents=True, exist_ok=False)
    return path


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Destination for windows.txt")
    args = parser.parse_args()
    print(BANNER)
    try:
        require_runtime()
        enable_dpi_awareness()
        path = args.output or new_run_directory() / "windows.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        save_windows(enumerate_windows(), path)
        print(f"\nSaved: {path.resolve()}")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        print(BANNER)


if __name__ == "__main__":
    raise SystemExit(main())
