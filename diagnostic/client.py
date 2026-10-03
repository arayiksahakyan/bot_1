"""Client workflow without GUI dependencies or actions on the trading UI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
from zipfile import ZipFile

if __package__:
    from . import inspect_ui as diagnostic
    from . import inspect_windows as windows
else:
    import inspect_ui as diagnostic
    import inspect_windows as windows

ZIP_NAME = "spimex_diagnostic.zip"
ERROR_LOG = "diagnostic_error.log"
PROGRESS = "Collecting diagnostic information..."
NOT_FOUND = (
    "SPIMEX window was not found.\n"
    "Please open SPIMEX, navigate to the required screen,\n"
    "and run this program again."
)
SUCCESS = "Diagnostics completed successfully.\nPlease send the file:\nspimex_diagnostic.zip"
PARTIAL = (
    "Diagnostics were collected, but some information was unavailable.\n"
    "Please send the file:\nspimex_diagnostic.zip"
)
FAILED = (
    "Diagnostics could not be completed.\n"
    "Please keep SPIMEX open and try again.\n"
    "If this happens again, send diagnostic_error.log to your support contact.\n"
    "No new diagnostic ZIP was saved."
)
SAVE_FAILED = (
    "The diagnostic file could not be saved here.\n"
    "Please move Start_Diagnostic.exe to your Documents folder and try again.\n"
    "No new diagnostic ZIP was saved."
)

BRAND = re.compile(r"spimex|спимекс|спбмтсб|петербургская\s+биржа", re.IGNORECASE)
TRADING = re.compile(
    r"\btrading\b|торгов(?:ый|ая|ое|ые|ого|ой|ую)?\s+"
    r"(?:терминал|систем|платформ|окн|сесси)|товарно[\s-]+сырьев",
    re.IGNORECASE,
)
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "iexplore.exe"}
NON_TRADING = {
    "start_diagnostic.exe", "explorer.exe", "notepad.exe", "notepad++.exe",
    "code.exe", "pycharm64.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
}


@dataclass(frozen=True)
class Detection:
    inventory: list[windows.WindowRecord]
    candidates: list[windows.WindowRecord]

    @property
    def automatic_target(self) -> windows.WindowRecord | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    def target_at(self, index: int) -> windows.WindowRecord:
        if not 0 <= index < len(self.candidates):
            raise ValueError("A listed window must be chosen before starting.")
        return self.candidates[index]


@dataclass(frozen=True)
class Outcome:
    state: str
    message: str
    archive: Path | None = None


def application_directory() -> Path:
    # A one-file build extracts __file__ under _MEIPASS; that is NOT the destination.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def likely_trading_windows(inventory: list[windows.WindowRecord]) -> list[windows.WindowRecord]:
    result = []
    seen = set()
    for window in inventory:
        process = window.process_name.casefold()
        if window.pid == os.getpid() or process in NON_TRADING or window.hwnd in seen:
            continue
        title_match = BRAND.search(window.title)
        trading_match = TRADING.search(window.title)
        if process in BROWSERS:
            # A SPIMEX website/document tab alone is insufficient evidence.
            matches = title_match and trading_match
        else:
            matches = title_match or trading_match or BRAND.search(process) or BRAND.search(window.class_name)
        if matches:
            result.append(window)
            seen.add(window.hwnd)
    return result


def detect_windows() -> Detection:
    windows.require_runtime()
    inventory = windows.enumerate_windows()
    return Detection(inventory, likely_trading_windows(inventory))


def write_error(directory: Path, message: str, error: BaseException | None = None) -> Path | None:
    """Never turn a logging failure into a traceback shown to the client."""
    detail = message + "\n"
    if error is not None:
        detail += "".join(traceback.format_exception(error))
    fallback = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "SPIMEX Diagnostic"
    for folder in (directory, fallback):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ERROR_LOG
            with path.open("a", encoding="utf-8") as stream:
                stream.write(detail + "\n")
            return path
        except OSError:
            continue
    return None


def friendly_failure(directory: Path, error: BaseException, *, saving: bool = False) -> Outcome:
    write_error(directory, "Diagnostic failed", error)
    return Outcome("error", SAVE_FAILED if saving and isinstance(error, OSError) else FAILED)


def collect_for_client(
    target: windows.WindowRecord, inventory: list[windows.WindowRecord],
    directory: Path, dpi_status: str,
) -> Outcome:
    """Collect with the existing engine and atomically publish beside the EXE."""
    try:
        # Same filesystem ensures atomic replacement. No configuration or loose
        # reports are left for the client to manage; only the ZIP and error log.
        with tempfile.TemporaryDirectory(prefix=".spimex_run_", dir=directory, ignore_cleanup_errors=True) as temporary:
            run_dir = Path(temporary)
            try:
                status = diagnostic.collect_diagnostic(
                    run_dir, dpi_status, selected_target=target, inventory=inventory,
                )
                archive = run_dir / ZIP_NAME
                with ZipFile(archive) as zipped:
                    required = {"windows.txt", "system_info.txt", "ui_tree.txt", "diagnostic_log.txt"}
                    if not required.issubset(zipped.namelist()) or zipped.testzip() is not None:
                        raise RuntimeError("Diagnostic archive is missing reports or failed validation.")
                    if status == 0 and "screenshot.png" not in zipped.namelist():
                        raise RuntimeError("Complete result was reported without its screenshot.")
                if status not in (0, 2):
                    raise RuntimeError(f"Collection returned unexpected status {status}")
                destination = directory / ZIP_NAME
                archive.replace(destination)
                return Outcome("success" if status == 0 else "partial", SUCCESS if status == 0 else PARTIAL, destination)
            finally:
                errors = run_dir / ERROR_LOG
                if errors.exists():
                    try:
                        detail = errors.read_text(encoding="utf-8", errors="replace")
                        if detail.strip():
                            write_error(directory, detail)
                    except OSError as exc:
                        write_error(directory, "Could not copy the per-run error log", exc)
    except Exception as exc:
        return friendly_failure(directory, exc, saving=True)
