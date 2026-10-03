"""Read-only UIA tree, selected-window image, system report and ZIP for Windows."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback
from zipfile import ZIP_DEFLATED, ZipFile

if __package__:
    from . import inspect_windows as windows
else:
    import inspect_windows as windows

TREE_TIMEOUT = 120
SCREENSHOT_TIMEOUT = 30
MAX_CONTROLS = 50_000
MAX_DEPTH = 100
REPORT_FILES = ("windows.txt", "ui_tree.txt", "screenshot.png", "system_info.txt", "diagnostic_log.txt")


def record_error(run_dir: Path, message: str) -> None:
    """Keep technical details in a file, including when no console is attached."""
    with (run_dir / "diagnostic_error.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
        if sys.exc_info()[0] is not None:
            stream.write(traceback.format_exc() + "\n")


def log(run_dir: Path, message: str) -> None:
    print(message, flush=True)
    with (run_dir / "diagnostic_log.txt").open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def save_system_info(path: Path, dpi_status: str) -> bool:
    import win32api

    complete = True
    lines = [
        windows.BANNER,
        f"UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Windows version: {platform.platform()}",
        f"Windows build: {sys.getwindowsversion()}",
        f"Python version: {sys.version}",
        f"Python architecture: {ctypes.sizeof(ctypes.c_void_p) * 8}-bit",
        f"Diagnostic DPI awareness: {dpi_status}",
    ]
    try:
        metrics = win32api.GetSystemMetrics
        lines.append(f"Primary screen resolution: {metrics(0)} x {metrics(1)} pixels")
        lines.append(
            f"Virtual desktop: origin=({metrics(76)}, {metrics(77)}), "
            f"resolution={metrics(78)} x {metrics(79)} pixels"
        )
        dpi = windows.user32().GetDpiForSystem()
        if not dpi:
            raise RuntimeError("System DPI was unavailable.")
        lines.append(f"System DPI: {dpi}; system scaling: {dpi / 96 * 100:.1f}%")
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        scale_function = shcore.GetScaleFactorForMonitor
        scale_function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
        scale_function.restype = ctypes.c_long
        for number, (monitor, _, rect) in enumerate(win32api.EnumDisplayMonitors(), 1):
            info = win32api.GetMonitorInfo(monitor)
            scale = ctypes.c_int()
            result = scale_function(int(monitor), ctypes.byref(scale))
            if result != 0:
                complete = False
                record_error(path.parent, f"Monitor {number} scaling unavailable: HRESULT {result}")
            scaling = f"{scale.value}%" if result == 0 else f"unavailable (HRESULT {result})"
            lines.append(
                f"Monitor {number}: device={info['Device']}; bounds={rect}; "
                f"resolution={rect[2] - rect[0]} x {rect[3] - rect[1]}; scaling={scaling}"
            )
    except Exception as exc:
        complete = False
        lines.append(f"Display information incomplete: {type(exc).__name__}: {exc}")
        record_error(path.parent, lines[-1])
    for package in ("pywinauto", "pywin32", "comtypes", "psutil", "Pillow", "six"):
        try:
            lines.append(f"{package}: {version(package)}")
        except Exception:
            lines.append(f"{package}: unavailable")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return complete


def append_target_info(run_dir: Path, target: windows.WindowRecord) -> bool:
    with (run_dir / "system_info.txt").open("a", encoding="utf-8") as stream:
        stream.write("\nSelected " + windows.window_line(1, target) + "\n")
        try:
            dpi = windows.user32().GetDpiForWindow(target.hwnd)
            if not dpi:
                raise RuntimeError("GetDpiForWindow returned zero")
            stream.write(
                f"Selected window DPI: {dpi}; relative scale: {dpi / 96 * 100:.1f}% "
                "(depends on the target application's DPI awareness)\n"
            )
            return True
        except Exception as exc:
            stream.write(f"Selected window DPI unavailable: {type(exc).__name__}: {exc}\n")
            record_error(run_dir, f"Selected window DPI: {exc}")
            return False


class UIAMetadata:
    """Strict reads through pywinauto without its silent COM-error defaults.

    UIAElementInfo.children()/some properties suppress provider errors. Direct
    Current* getters and iter_children() let the report mark those reads incomplete.
    """

    FIELDS = {
        "name": "CurrentName", "automation_id": "CurrentAutomationId",
        "class_name": "CurrentClassName", "rectangle": "CurrentBoundingRectangle",
        "enabled": "CurrentIsEnabled",
    }

    def __init__(self, info, control_types):
        self.info = info
        self.control_types = control_types

    def __getattr__(self, field):
        if field == "control_type":
            type_id = self.info.element.CurrentControlType
            return self.control_types.get(type_id, f"Unknown({type_id})")
        if field not in self.FIELDS:
            raise AttributeError(field)
        value = getattr(self.info.element, self.FIELDS[field])
        return bool(value) if field == "enabled" else value

    def children(self):
        return [UIAMetadata(child, self.control_types) for child in self.info.iter_children()]


def write_ui_tree(root, path: Path, max_controls=MAX_CONTROLS, max_depth=MAX_DEPTH) -> bool:
    """Read only UIAElementInfo properties/children; never obtain action patterns."""
    complete, count = True, 0
    with path.open("w", encoding="utf-8", buffering=1) as stream:
        def emit(line):
            stream.write(line + "\n")
            print(line, flush=True)

        emit(windows.BANNER)
        emit("Selected HWND subtree; bounds=[left, top, right, bottom] in screen coordinates.")
        emit("Only currently exposed UIA controls; virtualized items are not realized or scrolled.")
        stack = [(root, 0)]
        while stack:
            if count >= max_controls:
                emit(f"INCOMPLETE: control limit ({max_controls}) reached.")
                complete = False
                break
            element, depth = stack.pop()
            properties = {}
            for field in ("control_type", "name", "automation_id", "class_name", "rectangle", "enabled"):
                try:
                    value = getattr(element, field)
                    if field == "rectangle":
                        value = [value.left, value.top, value.right, value.bottom]
                    properties[field] = value
                except Exception as exc:
                    properties[field] = f"<unavailable: {type(exc).__name__}: {exc}>"
                    record_error(path.parent, f"UIA property {field}: {exc}")
                    complete = False
            emit("  " * depth + json.dumps(properties, ensure_ascii=False))
            count += 1
            if depth >= max_depth:
                emit("  " * depth + f"INCOMPLETE: depth limit ({max_depth}) reached.")
                complete = False
                continue
            try:
                # No visibility/enabled filtering: include every exposed child.
                children = element.children()
                stack.extend((child, depth + 1) for child in reversed(children))
            except Exception as exc:
                emit("  " * (depth + 1) + f"INCOMPLETE: children unavailable: {type(exc).__name__}: {exc}")
                record_error(path.parent, f"UIA children: {exc}")
                complete = False
        emit(f"{'COMPLETE' if complete else 'INCOMPLETE'}: {count} controls recorded.")
    return complete


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32), ("width", ctypes.c_int32),
        ("height", ctypes.c_int32), ("planes", ctypes.c_uint16),
        ("bit_count", ctypes.c_uint16), ("compression", ctypes.c_uint32),
        ("size_image", ctypes.c_uint32), ("x_pixels_per_meter", ctypes.c_int32),
        ("y_pixels_per_meter", ctypes.c_int32), ("colors_used", ctypes.c_uint32),
        ("colors_important", ctypes.c_uint32),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [("header", BitmapInfoHeader), ("colors", ctypes.c_uint32 * 1)]


def gdi32():
    dll = ctypes.WinDLL("gdi32", use_last_error=True)
    signatures = {
        "CreateCompatibleDC": ([wintypes.HDC], wintypes.HDC),
        "CreateDIBSection": (
            [wintypes.HDC, ctypes.POINTER(BitmapInfo), wintypes.UINT,
             ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD], wintypes.HANDLE,
        ),
        "SelectObject": ([wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        "DeleteObject": ([wintypes.HANDLE], wintypes.BOOL),
        "DeleteDC": ([wintypes.HDC], wintypes.BOOL),
        "GdiFlush": ([], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes = arguments
        function.restype = result
    return dll


def capture_target(target: windows.WindowRecord, path: Path) -> None:
    """PrintWindow renders ONLY the selected HWND into our private bitmap.

    No screen/desktop DC copying, focus changes, restoration, or fallback capture.
    GDI SelectObject below selects our own bitmap into a memory DC, not a UI control.
    """
    from PIL import Image
    import win32gui

    windows.validate_target(target)
    if win32gui.IsIconic(target.hwnd):
        raise RuntimeError("Screenshot unavailable: selected window is minimized; it was not restored.")
    rect = win32gui.GetWindowRect(target.hwnd)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width <= 0 or height <= 0 or width * height > 40_000_000:
        raise RuntimeError(f"Screenshot unavailable: invalid or excessive window size {width} x {height}.")
    gdi = gdi32()
    window_dc = win32gui.GetWindowDC(target.hwnd)
    if not window_dc:
        raise RuntimeError("Screenshot unavailable: cannot obtain the selected window DC.")
    memory_dc = bitmap = previous = None
    try:
        memory_dc = gdi.CreateCompatibleDC(window_dc)
        if not memory_dc:
            raise RuntimeError("Cannot allocate a private memory DC.")
        info = BitmapInfo()
        info.header.size = ctypes.sizeof(BitmapInfoHeader)
        info.header.width, info.header.height = width, -height  # Top-down DIB.
        info.header.planes, info.header.bit_count = 1, 32
        pixels = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(window_dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
        if not bitmap or not pixels.value:
            raise RuntimeError("Cannot allocate the private screenshot bitmap.")
        previous = gdi.SelectObject(memory_dc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise RuntimeError("Cannot attach the private bitmap to the memory DC.")
        ctypes.memset(pixels, 0, width * height * 4)
        # PW_RENDERFULLCONTENT (Windows 8.1+); the only source is this HWND.
        if not windows.user32().PrintWindow(target.hwnd, memory_dc, 2):
            raise RuntimeError("PrintWindow failed; no desktop screenshot was taken.")
        if not gdi.GdiFlush():
            raise RuntimeError("Screenshot bitmap rendering did not finish.")
        windows.validate_target(target)
        if win32gui.IsIconic(target.hwnd) or win32gui.GetWindowRect(target.hwnd) != rect:
            raise RuntimeError("Window bounds changed during capture; image discarded.")
        image = Image.frombytes(
            "RGB", (width, height), ctypes.string_at(pixels, width * height * 4),
            "raw", "BGRX", width * 4, 1,
        )
    finally:
        if previous:
            gdi.SelectObject(memory_dc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory_dc:
            gdi.DeleteDC(memory_dc)
        win32gui.ReleaseDC(target.hwnd, window_dc)
    with image:
        if all(low == high for low, high in image.getextrema()):
            raise RuntimeError("PrintWindow returned a blank/uniform image; capture is unsupported or protected.")
        temporary = path.with_suffix(".tmp.png")
        image.save(temporary, format="PNG")
        temporary.replace(path)


def worker_command(kind: str, run_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--diagnostic-worker", kind, "--run-dir", str(run_dir)]
    return [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", kind, "--run-dir", str(run_dir)]


def run_worker(kind: str, run_dir: Path, timeout: int) -> bool:
    """Bound provider hangs without terminating or manipulating the target app."""
    command = worker_command(kind, run_dir)
    # Workers have no console window and no stdin, so cannot acquire focus/input.
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="backslashreplace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "PYTHONIOENCODING": "utf-8:backslashreplace"},
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()  # Only the diagnostic child process we just created.
        output, _ = process.communicate()
        print(output, end="", flush=True)
        message = f"INCOMPLETE: {kind} worker exceeded {timeout}s; diagnostic worker stopped."
        log(run_dir, message)
        record_error(run_dir, message + "\n" + output)
        if kind == "tree":
            with (run_dir / "ui_tree.txt").open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")
        return False
    except BaseException:
        process.kill()
        process.communicate()
        raise
    print(output, end="", flush=True)
    code = process.returncode
    if code:
        log(run_dir, f"INCOMPLETE: {kind} worker exited with code {code}.")
        record_error(run_dir, f"{kind} worker exited with code {code}.\n{output}")
    return code == 0


def worker_main(kind: str, run_dir: Path) -> int:
    try:
        windows.require_runtime()
        windows.enable_dpi_awareness()
        target = windows.WindowRecord(**json.loads((run_dir / "target.json").read_text(encoding="utf-8")))
        windows.validate_target(target)
        if kind == "tree":
            from pywinauto.uia_defines import IUIA
            from pywinauto.uia_element_info import UIAElementInfo

            root = UIAElementInfo(target.hwnd)
            if root.process_id != target.pid or root.handle != target.hwnd:
                raise RuntimeError("UIA root does not match the selected window.")
            complete = write_ui_tree(UIAMetadata(root, IUIA().known_control_type_ids), run_dir / "ui_tree.txt")
            if not complete:
                record_error(run_dir, "UIA tree incomplete; see ui_tree.txt for failed reads or collection limits.")
            windows.validate_target(target)
            return 0 if complete else 2
        capture_target(target, run_dir / "screenshot.png")
        log(run_dir, "Screenshot saved: selected HWND only. Visually verify the image after the run.")
        return 0
    except Exception as exc:
        message = f"INCOMPLETE: {kind}: {type(exc).__name__}: {exc}"
        record_error(run_dir, message)
        log(run_dir, message)
        if kind == "tree":
            with (run_dir / "ui_tree.txt").open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")
        return 2


def package_results(run_dir: Path) -> Path:
    """Explicit allowlist: never include dependencies, other runs or temporary images."""
    archive = run_dir / "spimex_diagnostic.zip"
    temporary = run_dir / "spimex_diagnostic.tmp.zip"
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as zipped:
        for filename in (*REPORT_FILES, "diagnostic_error.log"):
            path = run_dir / filename
            if path.is_file():
                zipped.write(path, arcname=filename)
    temporary.replace(archive)
    return archive


def collect_diagnostic(
    run_dir: Path, dpi_status: str, *,
    selected_target: windows.WindowRecord | None = None,
    inventory: list[windows.WindowRecord] | None = None,
) -> int:
    status = 2
    try:
        log(run_dir, windows.BANNER)
        log(run_dir, "Collecting window metadata and display information.")
        system_ok = save_system_info(run_dir / "system_info.txt", dpi_status)
        candidates = windows.enumerate_windows() if inventory is None else inventory
        windows.save_windows(candidates, run_dir / "windows.txt")
        target = selected_target if selected_target is not None else windows.choose_window(candidates)
        if target is None:
            log(run_dir, "CANCELLED: no target selected; UI inspection and screenshot were not attempted.")
            return status
        windows.validate_target(target)
        log(run_dir, "Selected " + windows.window_line(1, target))
        (run_dir / "target.json").write_text(json.dumps(asdict(target), ensure_ascii=False), encoding="utf-8")
        target_info_ok = append_target_info(run_dir, target)
        log(run_dir, f"Reading the selected UI tree (timeout: {TREE_TIMEOUT}s)...")
        tree_ok = run_worker("tree", run_dir, TREE_TIMEOUT)
        log(run_dir, f"Capturing the selected application window (timeout: {SCREENSHOT_TIMEOUT}s)...")
        screenshot_ok = run_worker("screenshot", run_dir, SCREENSHOT_TIMEOUT)
        status = 0 if tree_ok and screenshot_ok and system_ok and target_info_ok else 2
        log(run_dir, "COMPLETE: requested reports saved." if status == 0 else "INCOMPLETE: see diagnostic_log.txt and ui_tree.txt.")
    except (KeyboardInterrupt, EOFError):
        log(run_dir, "CANCELLED: existing diagnostic results will be packaged.")
    except Exception as exc:
        record_error(run_dir, f"Collection failed: {exc}")
        log(run_dir, f"INCOMPLETE: {type(exc).__name__}: {exc}")
    finally:
        log(run_dir, windows.BANNER)
        archive = package_results(run_dir)
        print(f"\nResults folder: {run_dir}\nArchive: {archive}", flush=True)
    return status


def main() -> int:
    windows.configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("tree", "screenshot"), help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.run_dir is None:
            parser.error("--worker requires --run-dir")
        return worker_main(args.worker, args.run_dir)
    print(windows.BANNER)
    try:
        windows.require_runtime()
        dpi_status = windows.enable_dpi_awareness()
        run_dir = windows.new_run_directory()
        print("Enter the window number in this diagnostic console. Q cancels.")
        return collect_diagnostic(run_dir, dpi_status)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        print(windows.BANNER)


if __name__ == "__main__":
    raise SystemExit(main())
