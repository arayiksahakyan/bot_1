"""Safety/behavior checks with fake providers; no live application is accessed."""

import ast
from contextlib import redirect_stdout
import ctypes
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from PIL import Image

from diagnostic import inspect_ui as ui
from diagnostic import inspect_windows as windows

TARGET = windows.WindowRecord(0x123456789, "Торговый терминал", "terminal.exe", 77, "TradingWindow", 100.0)


class ReadOnlyElement:
    """A provider exposing only the six permitted fields and a children reader."""

    __slots__ = ("name", "control_type", "automation_id", "class_name", "rectangle", "enabled", "_children")

    def __init__(self, name="Заявки", children=(), enabled=True):
        self.name = name
        self.control_type = "Pane"
        self.automation_id = "pane-1"
        self.class_name = "CustomPane"
        self.rectangle = SimpleNamespace(left=-1920, top=0, right=-960, bottom=800)
        self.enabled = enabled
        self._children = children

    def children(self):
        if isinstance(self._children, Exception):
            raise self._children
        return self._children


class TemporaryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))


class DiagnosticTest(TemporaryTestCase):
    def test_uia_provider_failures_are_not_silently_reported_as_empty_metadata(self):
        class Provider:
            CurrentControlType = 50000
            CurrentAutomationId = "test-button"
            CurrentClassName = "Button"
            CurrentBoundingRectangle = SimpleNamespace(left=0, top=0, right=10, bottom=10)
            CurrentIsEnabled = False

            @property
            def CurrentName(self):
                raise RuntimeError("COM read failed")

        info = SimpleNamespace(element=Provider(), iter_children=lambda: iter(()))
        reader = ui.UIAMetadata(info, {50000: "Button"})
        self.assertEqual(reader.control_type, "Button")
        self.assertFalse(reader.enabled)
        self.assertFalse(ui.write_ui_tree(reader, self.folder / "ui_tree.txt"))
        self.assertIn("COM read failed", (self.folder / "ui_tree.txt").read_text())
        info.iter_children = Mock(side_effect=RuntimeError("COM traversal failed"))
        with self.assertRaisesRegex(RuntimeError, "COM traversal failed"):
            reader.children()

    def test_system_report_has_windows_python_resolution_and_per_monitor_scaling(self):
        api = Mock()
        metrics = {0: 1920, 1: 1080, 76: -1920, 77: 0, 78: 3840, 79: 1080}
        api.GetSystemMetrics.side_effect = metrics.__getitem__
        api.EnumDisplayMonitors.return_value = [(1, None, (-1920, 0, 0, 1080)), (2, None, (0, 0, 1920, 1080))]
        api.GetMonitorInfo.side_effect = lambda monitor: {"Device": f"DISPLAY{monitor}"}
        scale = Mock()
        def get_scale(monitor, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))[0] = 150 if monitor == 1 else 100
            return 0
        scale.GetScaleFactorForMonitor.side_effect = get_scale
        user = SimpleNamespace(GetDpiForSystem=lambda: 144)
        with (
            patch.dict(sys.modules, {"win32api": api}),
            patch.object(sys, "getwindowsversion", return_value="10.0 test build", create=True),
            patch.object(ui.platform, "platform", return_value="Windows-test"),
            patch.object(ui.ctypes, "WinDLL", return_value=scale, create=True),
            patch.object(windows, "user32", return_value=user),
        ):
            path = self.folder / "system_info.txt"
            self.assertTrue(ui.save_system_info(path, "test DPI context"))
            report = path.read_text()
            for text in ("Windows-test", "Python version:", "1920 x 1080", "System DPI: 144", "150.0%", "scaling=150%", "scaling=100%"):
                self.assertIn(text, report)
            api.GetSystemMetrics.side_effect = OSError("display unavailable")
            self.assertFalse(ui.save_system_info(path, "test DPI context"))
            self.assertIn("Display information incomplete", path.read_text())

    def test_missing_target_dpi_is_reported_without_an_invented_scale(self):
        with patch.object(windows, "user32", return_value=SimpleNamespace(GetDpiForWindow=lambda hwnd: 0)):
            self.assertFalse(ui.append_target_info(self.folder, TARGET))
        report = (self.folder / "system_info.txt").read_text()
        self.assertIn("DPI unavailable", report)
        self.assertNotIn("0.0%", report)

    def test_tree_has_all_fields_and_disabled_children_and_matches_console(self):
        root = ReadOnlyElement(children=[ReadOnlyElement('Buy\n"quoted"', enabled=False)])
        path = self.folder / "ui_tree.txt"
        self.assertTrue(ui.write_ui_tree(root, path))
        result = path.read_text(encoding="utf-8")
        self.assertEqual(result, self.output.getvalue())
        records = [json.loads(line) for line in result.splitlines() if line.lstrip().startswith("{")]
        self.assertEqual(len(records), 2)
        self.assertEqual(set(records[0]), {"name", "control_type", "automation_id", "class_name", "rectangle", "enabled"})
        self.assertEqual(records[0]["name"], "Заявки")
        self.assertEqual(records[0]["rectangle"], [-1920, 0, -960, 800])
        self.assertFalse(records[1]["enabled"])
        self.assertEqual(records[1]["name"], 'Buy\n"quoted"')

    def test_tree_property_and_branch_failures_preserve_siblings(self):
        broken = ReadOnlyElement(children=RuntimeError("provider unavailable"))
        del broken.automation_id
        root = ReadOnlyElement(children=[broken, ReadOnlyElement("still readable")])
        path = self.folder / "ui_tree.txt"
        self.assertFalse(ui.write_ui_tree(root, path))
        report = path.read_text(encoding="utf-8")
        self.assertIn("<unavailable: AttributeError", report)
        self.assertIn("provider unavailable", report)
        self.assertIn("still readable", report)
        self.assertIn("INCOMPLETE: 3 controls", report)

    def test_tree_cycle_and_node_limits_are_explicit(self):
        root = ReadOnlyElement()
        root._children = [root]
        path = self.folder / "ui_tree.txt"
        self.assertFalse(ui.write_ui_tree(root, path, max_depth=2))
        self.assertIn("depth limit (2)", path.read_text())
        self.assertFalse(ui.write_ui_tree(root, path, max_controls=2))
        self.assertIn("control limit (2)", path.read_text())

    def test_console_selection_rejects_invalid_numbers_and_supports_cancel(self):
        with patch("builtins.input", side_effect=["", "0", "-1", "999", "abc", "1"]):
            self.assertEqual(windows.choose_window([TARGET]), TARGET)
        with patch("builtins.input", return_value="q"):
            self.assertIsNone(windows.choose_window([TARGET]))
        with patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(windows.choose_window([TARGET]))

    def test_window_inventory_includes_untitled_and_marks_access_denied(self):
        gui = Mock()
        gui.error = OSError
        gui.IsWindowVisible.side_effect = lambda hwnd: hwnd != 2
        gui.GetWindowText.side_effect = lambda hwnd: "" if hwnd == 1 else TARGET.title
        gui.GetClassName.return_value = TARGET.class_name
        gui.EnumWindows.side_effect = lambda callback, arg: [callback(hwnd, arg) for hwnd in (1, 2, 3)]
        process = SimpleNamespace(name=lambda: "terminal.exe", create_time=lambda: 100.0)
        psutil = SimpleNamespace(Error=PermissionError, Process=Mock(side_effect=[process, PermissionError()]))
        processes = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (9, hwnd + 100))
        with patch.dict(sys.modules, {"win32gui": gui, "win32process": processes, "psutil": psutil}):
            records = windows.enumerate_windows()
        self.assertEqual([record.hwnd for record in records], [1, 3])
        self.assertEqual(records[0].title, "")
        self.assertIn("unavailable", records[1].process_name)
        self.assertIsNone(records[1].created)
        path = self.folder / "windows.txt"
        windows.save_windows(records, path)
        report = path.read_text(encoding="utf-8")
        for value in ('title=""', 'process="terminal.exe"', "PID=101", "TradingWindow", TARGET.title):
            self.assertIn(value, report)

    def test_identity_checks_refuse_closed_reused_hidden_and_unknown_targets(self):
        gui = Mock()
        gui.IsWindow.return_value = True
        gui.IsWindowVisible.return_value = True
        gui.GetClassName.return_value = TARGET.class_name
        process = Mock()
        process.create_time.return_value = TARGET.created
        processes = Mock()
        processes.GetWindowThreadProcessId.return_value = (1, TARGET.pid)
        modules = {"win32gui": gui, "win32process": processes, "psutil": SimpleNamespace(Process=lambda pid: process)}
        with patch.dict(sys.modules, modules):
            windows.validate_target(TARGET)
            for target in (replace(TARGET, created=None), replace(TARGET, hwnd=0), replace(TARGET, pid=0)):
                with self.assertRaises(RuntimeError):
                    windows.validate_target(target)
            for obj, attribute, invalid, valid in (
                (gui, "IsWindow", False, True),
                (gui, "IsWindowVisible", False, True),
                (gui, "GetClassName", "OtherWindow", TARGET.class_name),
                (process, "create_time", 101.0, TARGET.created),
                (processes, "GetWindowThreadProcessId", (1, 888), (1, TARGET.pid)),
            ):
                getattr(obj, attribute).return_value = invalid
                with self.assertRaises(RuntimeError):
                    windows.validate_target(TARGET)
                getattr(obj, attribute).return_value = valid

    def test_archive_only_includes_current_run_report_allowlist(self):
        for name in ui.REPORT_FILES:
            (self.folder / name).write_bytes(b"current report")
        for name in ("target.json", "credentials.txt", "screenshot.tmp.png", "old.zip"):
            (self.folder / name).write_bytes(b"must not be archived")
        archive = ui.package_results(self.folder)
        with ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()), set(ui.REPORT_FILES))
            self.assertIsNone(zipped.testzip())
        another = windows.new_run_directory(self.folder)
        (another / "diagnostic_log.txt").write_text("Screenshot failed")
        with ZipFile(ui.package_results(another)) as zipped:
            self.assertEqual(zipped.namelist(), ["diagnostic_log.txt"])

    def test_timeout_stops_only_owned_worker_and_keeps_partial_tree(self):
        path = self.folder / "ui_tree.txt"
        path.write_text("already recorded\n")
        worker = Mock()
        worker.communicate.side_effect = [subprocess.TimeoutExpired("diagnostic", 1), ("partial tree\n", None)]
        with patch.object(ui.subprocess, "Popen", return_value=worker) as start:
            self.assertFalse(ui.run_worker("tree", self.folder, 1))
        worker.kill.assert_called_once_with()
        self.assertEqual(start.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(start.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertNotIn("shell", start.call_args.kwargs)
        self.assertIn("inspect_ui.py", start.call_args.args[0][2])
        self.assertTrue(path.read_text().startswith("already recorded\n"))
        self.assertIn("INCOMPLETE", path.read_text())

    def test_worker_failure_and_success_are_reported(self):
        worker = Mock()
        worker.communicate.return_value = ("tree output\n", None)
        with patch.object(ui.subprocess, "Popen", return_value=worker):
            worker.returncode = 2
            self.assertFalse(ui.run_worker("tree", self.folder, 1))
            worker.returncode = 0
            self.assertTrue(ui.run_worker("tree", self.folder, 1))
        worker.kill.assert_not_called()

    def prepare_coordinator(self, choice=TARGET):
        def system_report(path, _):
            path.write_text("Windows test fixture\n")
            return True
        self.enterContext(patch.object(ui, "save_system_info", side_effect=system_report))
        self.enterContext(patch.object(windows, "enumerate_windows", return_value=[TARGET]))
        self.enterContext(patch.object(windows, "choose_window", return_value=choice))
        self.enterContext(patch.object(windows, "validate_target"))
        self.enterContext(patch.object(ui, "append_target_info", return_value=True))

    def test_cancel_packages_metadata_without_inspection(self):
        self.prepare_coordinator(choice=None)
        with patch.object(ui, "run_worker") as worker:
            self.assertEqual(ui.collect_diagnostic(self.folder, "test"), 2)
        worker.assert_not_called()
        with ZipFile(self.folder / "spimex_diagnostic.zip") as zipped:
            self.assertEqual(set(zipped.namelist()), {"windows.txt", "system_info.txt", "diagnostic_log.txt"})
            self.assertIn(b"CANCELLED", zipped.read("diagnostic_log.txt"))

    def test_failed_tree_still_attempts_screenshot_and_packages_partial_results(self):
        self.prepare_coordinator()
        def worker(kind, run_dir, timeout):
            if kind == "tree":
                (run_dir / "ui_tree.txt").write_text("INCOMPLETE: access denied")
                return False
            (run_dir / "screenshot.png").write_bytes(b"mock image")
            return True
        with patch.object(ui, "run_worker", side_effect=worker) as work:
            self.assertEqual(ui.collect_diagnostic(self.folder, "test"), 2)
        self.assertEqual([call.args[0] for call in work.call_args_list], ["tree", "screenshot"])
        with ZipFile(self.folder / "spimex_diagnostic.zip") as zipped:
            self.assertEqual(set(zipped.namelist()), set(ui.REPORT_FILES))
            self.assertIn(b"INCOMPLETE", zipped.read("diagnostic_log.txt"))

    def test_complete_collection_archive_contains_reports_and_banner(self):
        self.prepare_coordinator()
        def worker(kind, run_dir, timeout):
            name = "ui_tree.txt" if kind == "tree" else "screenshot.png"
            (run_dir / name).write_bytes(b"mock report")
            return True
        with patch.object(ui, "run_worker", side_effect=worker):
            self.assertEqual(ui.collect_diagnostic(self.folder, "test"), 0)
        with ZipFile(self.folder / "spimex_diagnostic.zip") as zipped:
            self.assertEqual(set(zipped.namelist()), set(ui.REPORT_FILES))
            self.assertIn(windows.BANNER.encode(), zipped.read("diagnostic_log.txt"))

    def test_wrong_platform_fails_before_loading_windows_dependencies(self):
        with patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                windows.require_runtime()
        with patch.object(sys, "platform", "win32"), patch.object(sys, "version_info", (3, 13)):
            with self.assertRaisesRegex(RuntimeError, "Python 3.12"):
                windows.require_runtime()


class CaptureTest(TemporaryTestCase):
    def setUp(self):
        super().setUp()
        # Four real BGRX pixels, with distinct top/bottom rows for orientation checks.
        self.raw = bytes([0, 0, 255, 0, 0, 255, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
        self.storage = ctypes.create_string_buffer(16)
        self.gui = Mock()
        self.gui.IsIconic.return_value = False
        self.gui.GetWindowRect.return_value = (-2, 0, 0, 2)
        self.gui.GetWindowDC.return_value = 200
        self.gdi = Mock()
        self.gdi.CreateCompatibleDC.return_value = 300
        self.gdi.SelectObject.return_value = 500
        self.gdi.GdiFlush.return_value = True
        def allocate(source, info, usage, pixels, section, offset):
            header = ctypes.cast(info, ctypes.POINTER(ui.BitmapInfo)).contents.header
            self.assertEqual((header.width, header.height, header.bit_count), (2, -2, 32))
            ctypes.cast(pixels, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.storage)
            return 400
        self.gdi.CreateDIBSection.side_effect = allocate
        self.api = Mock()
        def render(hwnd, dc, flags):
            self.assertEqual((hwnd, dc, flags), (TARGET.hwnd, 300, 2))
            ctypes.memmove(self.storage, self.raw, len(self.raw))
            return True
        self.api.PrintWindow.side_effect = render
        self.enterContext(patch.dict(sys.modules, {"win32gui": self.gui}))
        self.enterContext(patch.object(windows, "validate_target"))
        self.enterContext(patch.object(windows, "user32", return_value=self.api))
        self.enterContext(patch.object(ui, "gdi32", return_value=self.gdi))

    def test_png_captures_selected_hwnd_only_and_has_correct_orientation(self):
        path = self.folder / "screenshot.png"
        ui.capture_target(TARGET, path)
        with Image.open(path) as image:
            self.assertEqual(image.size, (2, 2))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(image.getpixel((0, 1)), (0, 0, 255))
        self.gui.GetWindowDC.assert_called_once_with(TARGET.hwnd)
        self.gui.ReleaseDC.assert_called_once_with(TARGET.hwnd, 200)
        self.gdi.DeleteObject.assert_called_once_with(400)
        self.gdi.DeleteDC.assert_called_once_with(300)
        self.assertFalse((self.folder / "screenshot.tmp.png").exists())

    def test_failed_render_has_no_image_or_fallback_and_releases_resources(self):
        self.api.PrintWindow.side_effect = None
        self.api.PrintWindow.return_value = False
        with self.assertRaisesRegex(RuntimeError, "PrintWindow failed"):
            ui.capture_target(TARGET, self.folder / "screenshot.png")
        self.assertFalse((self.folder / "screenshot.png").exists())
        self.gdi.DeleteObject.assert_called_once_with(400)
        self.gdi.DeleteDC.assert_called_once_with(300)
        self.gui.ReleaseDC.assert_called_once_with(TARGET.hwnd, 200)

    def test_minimized_window_is_not_restored_or_captured(self):
        self.gui.IsIconic.return_value = True
        with self.assertRaisesRegex(RuntimeError, "minimized"):
            ui.capture_target(TARGET, self.folder / "screenshot.png")
        self.gui.GetWindowDC.assert_not_called()
        self.api.PrintWindow.assert_not_called()

    def test_blank_render_is_not_saved_as_success(self):
        self.raw = bytes(16)
        with self.assertRaisesRegex(RuntimeError, "blank/uniform"):
            ui.capture_target(TARGET, self.folder / "screenshot.png")
        self.assertFalse((self.folder / "screenshot.png").exists())

    def test_changed_bounds_discards_image(self):
        self.gui.GetWindowRect.side_effect = [(-2, 0, 0, 2), (-2, 0, 1, 2)]
        with self.assertRaisesRegex(RuntimeError, "bounds changed"):
            ui.capture_target(TARGET, self.folder / "screenshot.png")
        self.assertFalse((self.folder / "screenshot.png").exists())


class StaticSafetyTest(unittest.TestCase):
    def test_runtime_sources_do_not_call_input_or_workflow_changing_apis(self):
        forbidden = {
            "click", "click_input", "double_click", "double_click_input", "right_click_input",
            "type_keys", "send_keys", "press", "write", "set_focus", "invoke", "select",
            "set_edit_text", "set_text", "set_value", "check", "uncheck", "toggle", "expand",
            "collapse", "scroll", "scroll_into_view", "realize", "move_window", "restore",
            "minimize", "maximize", "set_window_text", "draw_outline", "set_capture",
            "SetForegroundWindow", "SetFocus", "SetActiveWindow", "ShowWindow", "SetWindowPos",
            "SendInput", "SendMessage", "PostMessage", "mouse_event", "keybd_event", "SetCursorPos",
            "capture_as_image", "grab", "grabclipboard", "BitBlt", "GetDesktopWindow", "GetDC",
        }
        # File writes are intended; only flag 'write' when not a known report stream.
        for source in (Path(ui.__file__), Path(windows.__file__)):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr == "write" and isinstance(node.func.value, ast.Name) and node.func.value.id in {"stream", "zipped"}:
                    continue
                self.assertNotIn(node.func.attr, forbidden, f"{source.name}:{node.lineno}")
        # The public utility has only the metadata-level pywinauto import.
        self.assertNotIn("Application(", Path(ui.__file__).read_text())
        self.assertNotIn("Desktop(", Path(ui.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
