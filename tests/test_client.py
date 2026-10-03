"""Client GUI/workflow tests. Windows applications and native APIs are mocked."""

import ast
from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
from queue import Queue
import runpy
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from diagnostic import client, inspect_ui, inspect_windows, start_diagnostic

TARGET = inspect_windows.WindowRecord(501, "SPIMEX - Orders", "terminal.exe", 777, "TradingWindow", 1.0)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.enterContext(redirect_stdout(io.StringIO()))

    def app(self):
        """Exercise the real GUI handlers with widgets replaced by inert mocks."""
        app = start_diagnostic.DiagnosticApp.__new__(start_diagnostic.DiagnosticApp)
        app.root = Mock()
        app.directory, app.dpi_status = self.directory, "test DPI"
        app.busy, app.detection, app.results = False, None, Queue()
        for field in ("message", "progress", "choices", "start_button", "close_button"):
            setattr(app, field, Mock())
        return app

    def test_one_matching_window_is_selected_automatically_without_start(self):
        app = self.app()
        detection = client.Detection([TARGET], [TARGET])
        app.collect = Mock()
        app.detected(detection)
        self.assertEqual(detection.automatic_target, TARGET)
        app.collect.assert_called_once_with(TARGET)
        app.choices.pack.assert_not_called()
        app.start_button.pack.assert_not_called()

    def test_multiple_windows_wait_for_explicit_gui_selection(self):
        other = replace(TARGET, hwnd=502, title="SPIMEX - Trades")
        detection = client.Detection([TARGET, other], [TARGET, other])
        app = self.app()
        app.collect = Mock()
        app.detected(detection)
        self.assertIsNone(detection.automatic_target)
        app.message.set.assert_called_once_with("Select the SPIMEX window")
        app.collect.assert_not_called()
        app.choices.current.return_value = -1
        app.start_chosen()
        app.collect.assert_not_called()
        app.choices.current.return_value = 1
        app.choice_changed()
        app.start_button.configure.assert_called_once_with(state="normal")
        app.start_chosen()
        app.collect.assert_called_once_with(other)
        with self.assertRaises(ValueError):
            detection.target_at(-1)
        with self.assertRaises(ValueError):
            detection.target_at(2)

    def test_no_window_shows_exact_friendly_message_without_collection(self):
        app = self.app()
        app.collect = Mock()
        app.detected(client.Detection([], []))
        app.message.set.assert_called_once_with(
            "SPIMEX window was not found.\nPlease open SPIMEX, navigate to the required screen,\nand run this program again."
        )
        app.collect.assert_not_called()

    def test_detection_matches_english_russian_process_and_class_names(self):
        samples = [
            TARGET, replace(TARGET, hwnd=502, title="СПбМТСБ — Заявки"),
            replace(TARGET, hwnd=503, title="СПИМЕКС"),
            replace(TARGET, hwnd=504, title="Торговая система"),
            replace(TARGET, hwnd=505, title="Trading terminal"),
            replace(TARGET, hwnd=506, title="Orders", process_name="SpimexClient.exe"),
            replace(TARGET, hwnd=507, title="Orders", class_name="SPIMEXMainWindow"),
        ]
        with patch.object(client.os, "getpid", return_value=123):
            self.assertEqual(client.likely_trading_windows(samples), samples)

    def test_detection_excludes_own_window_duplicates_and_obvious_document_windows(self):
        samples = [
            replace(TARGET, pid=123),
            replace(TARGET, process_name="Start_Diagnostic.exe"),
            replace(TARGET, process_name="code.exe"),
            replace(TARGET, title="SPIMEX website", process_name="chrome.exe"),
            replace(TARGET, title="Documents", class_name="CabinetWClass", process_name="explorer.exe"),
            replace(TARGET, title="Untitled", class_name="OtherClass"),
            TARGET, TARGET,
        ]
        with patch.object(client.os, "getpid", return_value=123):
            self.assertEqual(client.likely_trading_windows(samples), [TARGET])
            browser_terminal = replace(TARGET, title="SPIMEX trading terminal", process_name="msedge.exe")
            self.assertEqual(client.likely_trading_windows([browser_terminal]), [browser_terminal])

    def test_detect_preserves_the_full_inventory_for_windows_report(self):
        unrelated = replace(TARGET, hwnd=600, title="Mail", class_name="MailWindow")
        with patch.object(inspect_windows, "require_runtime"), patch.object(inspect_windows, "enumerate_windows", return_value=[TARGET, unrelated]):
            found = client.detect_windows()
        self.assertEqual(found.inventory, [TARGET, unrelated])
        self.assertEqual(found.candidates, [TARGET])

    def mock_collection_apis(self, screenshot=True):
        self.enterContext(patch.object(inspect_windows, "validate_target"))
        self.enterContext(patch.object(inspect_windows, "choose_window", side_effect=AssertionError("Must not request console input")))
        self.enterContext(patch.object(inspect_windows, "enumerate_windows", side_effect=AssertionError("Use the GUI snapshot")))
        self.enterContext(patch.object(inspect_ui, "append_target_info", return_value=True))
        def system(path, dpi):
            path.write_text("Windows test fixture")
            return True
        self.enterContext(patch.object(inspect_ui, "save_system_info", side_effect=system))
        def worker(kind, run_dir, timeout):
            if kind == "tree":
                (run_dir / "ui_tree.txt").write_text("COMPLETE: test control")
                return True
            if screenshot:
                (run_dir / "screenshot.png").write_bytes(b"mock image")
                return True
            inspect_ui.record_error(run_dir, "Screenshot provider unsupported")
            return False
        self.enterContext(patch.object(inspect_ui, "run_worker", side_effect=worker))

    def test_success_creates_valid_zip_beside_exe_and_uses_exact_success_message(self):
        self.mock_collection_apis()
        old_zip = self.directory / client.ZIP_NAME
        old_zip.write_bytes(b"old report")
        result = client.collect_for_client(TARGET, [TARGET], self.directory, "test")
        self.assertEqual(result.state, "success")
        self.assertEqual(result.message, "Diagnostics completed successfully.\nPlease send the file:\nspimex_diagnostic.zip")
        self.assertEqual(result.archive, old_zip)
        with ZipFile(result.archive) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("screenshot.png", archive.namelist())
            self.assertIn(b"SPIMEX", archive.read("windows.txt"))
            self.assertNotIn("target.json", archive.namelist())
        self.assertEqual(list(self.directory.iterdir()), [old_zip])

    def test_unsupported_screenshot_still_packages_reports_and_error_log_without_false_success(self):
        self.mock_collection_apis(screenshot=False)
        result = client.collect_for_client(TARGET, [TARGET], self.directory, "test")
        self.assertEqual(result.state, "partial")
        self.assertEqual(result.message, client.PARTIAL)
        with ZipFile(result.archive) as archive:
            self.assertIn("ui_tree.txt", archive.namelist())
            self.assertNotIn("screenshot.png", archive.namelist())
            self.assertIn("diagnostic_error.log", archive.namelist())
        self.assertIn("provider unsupported", (self.directory / client.ERROR_LOG).read_text())

    def test_exception_details_go_to_log_and_never_to_user(self):
        with patch.object(inspect_ui, "collect_diagnostic", side_effect=RuntimeError("secret technical COM detail")):
            result = client.collect_for_client(TARGET, [TARGET], self.directory, "test")
        self.assertEqual(result.state, "error")
        self.assertEqual(result.message, client.FAILED)
        self.assertIsNone(result.archive)
        self.assertNotIn("secret technical", result.message)
        technical = (self.directory / client.ERROR_LOG).read_text()
        self.assertIn("secret technical COM detail", technical)
        self.assertIn("Traceback", technical)

    def test_detection_access_error_does_not_claim_the_output_folder_is_unwritable(self):
        outcome = client.friendly_failure(self.directory, PermissionError("process query denied"))
        self.assertEqual(outcome.message, client.FAILED)
        self.assertNotEqual(outcome.message, client.SAVE_FAILED)

    def test_write_error_is_friendly_and_preserves_previous_zip(self):
        old_zip = self.directory / client.ZIP_NAME
        old_zip.write_bytes(b"previous ZIP")
        with patch.object(client.tempfile, "TemporaryDirectory", side_effect=PermissionError("write denied")):
            result = client.collect_for_client(TARGET, [TARGET], self.directory, "test")
        self.assertEqual(result.message, client.SAVE_FAILED)
        self.assertIsNone(result.archive)
        self.assertEqual(old_zip.read_bytes(), b"previous ZIP")
        self.assertNotIn("PermissionError", result.message)

    def test_missing_reports_never_replace_previous_archive_or_report_success(self):
        old_zip = self.directory / client.ZIP_NAME
        old_zip.write_bytes(b"previous ZIP")
        def broken(run_dir, *args, **kwargs):
            with ZipFile(run_dir / client.ZIP_NAME, "w") as archive:
                archive.writestr("diagnostic_log.txt", "incomplete")
            return 0
        with patch.object(inspect_ui, "collect_diagnostic", side_effect=broken):
            result = client.collect_for_client(TARGET, [TARGET], self.directory, "test")
        self.assertEqual(result.state, "error")
        self.assertEqual(old_zip.read_bytes(), b"previous ZIP")

    def test_frozen_output_path_uses_executable_parent_not_temp_extraction_directory(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(self.directory / "Start_Diagnostic.exe")),
            patch.object(sys, "_MEIPASS", str(self.directory / "temporary-unpack"), create=True),
        ):
            self.assertEqual(client.application_directory(), self.directory)

    def test_frozen_workers_use_exe_without_python_or_script_arguments(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "Start_Diagnostic.exe"):
            command = inspect_ui.worker_command("tree", self.directory)
        self.assertEqual(command, ["Start_Diagnostic.exe", "--diagnostic-worker", "tree", "--run-dir", str(self.directory)])

    def test_worker_dispatch_never_constructs_gui(self):
        with patch.object(inspect_ui, "worker_main", return_value=0) as worker, patch.object(start_diagnostic, "run_gui") as gui:
            status = start_diagnostic.dispatch(["--diagnostic-worker", "tree", "--run-dir", str(self.directory)], self.directory)
        self.assertEqual(status, 0)
        worker.assert_called_once_with("tree", self.directory)
        gui.assert_not_called()

    def test_no_console_streams_are_supported_and_restored(self):
        def fake_dispatch(args, directory):
            self.assertIsNotNone(sys.stdin)
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            print("READ-ONLY test", flush=True)
            return 0
        with (
            patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), patch.object(sys, "stderr", None),
            patch.object(start_diagnostic, "dispatch", side_effect=fake_dispatch),
        ):
            self.assertEqual(start_diagnostic.main([]), 0)
            self.assertIsNone(sys.stdout)
            self.assertIsNone(sys.stderr)

    def test_unhandled_startup_error_is_logged_and_shown_without_traceback(self):
        with (
            patch.object(client, "application_directory", return_value=self.directory),
            patch.object(start_diagnostic, "dispatch", side_effect=RuntimeError("technical startup failure")),
            patch.object(start_diagnostic, "show_startup_error") as dialog,
        ):
            self.assertEqual(start_diagnostic.main([]), 1)
        dialog.assert_called_once_with(client.FAILED)
        self.assertIn("technical startup failure", (self.directory / client.ERROR_LOG).read_text())

    def test_callback_error_uses_friendly_message_and_file(self):
        app = self.app()
        app.callback_error(RuntimeError, RuntimeError("technical callback failure"), None)
        app.message.set.assert_called_once_with(client.FAILED)
        self.assertIn("technical callback failure", (self.directory / client.ERROR_LOG).read_text())

    def test_progress_and_close_do_not_abandon_running_workers(self):
        app = self.app()
        app.busy = True
        app.close()
        app.root.destroy.assert_not_called()
        self.assertIn(client.PROGRESS, app.message.set.call_args.args[0])
        app.busy = False
        app.close()
        app.root.destroy.assert_called_once_with()

    def test_background_result_is_delivered_only_by_main_loop_poll(self):
        app = self.app()
        completed = Mock()
        value = client.Outcome("success", client.SUCCESS)
        def thread(**kwargs):
            return SimpleNamespace(start=lambda: kwargs["target"]())
        with patch.object(start_diagnostic, "Thread", side_effect=thread):
            app.background(lambda: value, completed)
        completed.assert_not_called()
        self.assertTrue(app.busy)
        app.message.set.assert_called_once_with(client.PROGRESS)
        app.poll()
        self.assertFalse(app.busy)
        completed.assert_called_once_with(value)

    def test_spec_refuses_non_windows_build_before_loading_pyinstaller(self):
        spec = Path(__file__).resolve().parents[1] / "Start_Diagnostic.spec"
        with patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(SystemExit, "must be built on Windows"):
                runpy.run_path(str(spec))

    def test_client_layer_has_no_trading_action_or_focus_apis(self):
        forbidden = {
            "click", "click_input", "type_keys", "set_focus", "invoke", "select",
            "set_text", "set_value", "set_edit_text", "set_window_text", "scroll",
            "SetForegroundWindow", "SetFocus", "SendInput", "PostMessage", "SendMessage",
            "focus_force", "focus_set", "lift", "grab_set", "capture_as_image", "grab",
        }
        for module in (client, start_diagnostic):
            tree = ast.parse(Path(module.__file__).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, forbidden)


if __name__ == "__main__":
    unittest.main()
