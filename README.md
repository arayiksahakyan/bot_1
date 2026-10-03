# SPIMEX read-only diagnostic

A portable Windows GUI built from Python 3.12, pywinauto, Tkinter and PyInstaller.
It reads the selected application's UI and contains no trading automation.

## Client instructions

You only need **Start_Diagnostic.exe**. Nothing to install, configure or type.
No Python, pip, command prompt or PowerShell is needed.

1. Save `Start_Diagnostic.exe` in your Documents folder.
2. Open SPIMEX and navigate to the screen you want inspected.
3. Double-click `Start_Diagnostic.exe`. If a list appears, choose your SPIMEX
   window and press **Start**.
4. Wait for completion. Send **spimex_diagnostic.zip**, located beside
   `Start_Diagnostic.exe`.

The program shows **Collecting diagnostic information...** while it works. On a
complete collection it shows:

```text
Diagnostics completed successfully.
Please send the file:
spimex_diagnostic.zip
```

If SPIMEX is not detected, it shows:

```text
SPIMEX window was not found.
Please open SPIMEX, navigate to the required screen,
and run this program again.
```

If some information is unavailable, the program says so and still saves the
available reports in the ZIP. If it reports an error, try again or send
`diagnostic_error.log` to your support contact. An error message does not mean
that an older ZIP beside the program was updated.

## Developer: build on Windows

**The Windows EXE must be built on Windows.** The spec deliberately refuses a
Linux/macOS build; renaming a Linux executable to `.exe` is not supported.

On a **Windows x64 build computer**, install Python **3.12 x64**, including Tcl/Tk
and the Python launcher, then copy this repository and double-click
**build_windows.bat**. The build computer needs package-download access. It does
not need SPIMEX. The script:

1. Creates `.build-venv` and installs `diagnostic/requirements-build.txt`.
2. Checks dependencies and runs every existing and new mocked test.
3. Builds the checked-in `Start_Diagnostic.spec` with PyInstaller.
4. Verifies that the output is a Windows x64 PE executable with the GUI subsystem.

Output: **dist/Start_Diagnostic.exe**. Give the client this single file after
Windows verification. The client does not need the repository, Python, a virtual
environment, DLL folders, batch files, configuration files, or internet access.

Equivalent developer commands (Windows only):

```bat
py -3.12 -m venv .build-venv
.build-venv\Scripts\python.exe -m pip install --only-binary=:all: -r diagnostic\requirements-build.txt
.build-venv\Scripts\python.exe -m unittest discover -s tests -v
.build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean Start_Diagnostic.spec
```

The spec implements **--onefile --noconsole** with a single `EXE` and
`console=False`. Those switches cannot also be passed when building an existing
spec. It bundles Tcl/Tk, the runtime dependencies, package metadata, and generated
`comtypes.gen` UI Automation wrappers. It requests no administrator privileges.
It disables PyInstaller's default traceback dialog; application exceptions are
handled by the GUI and written to `diagnostic_error.log`.

For developer-only source execution, after installing the dependencies:

```bat
.build-venv\Scripts\pythonw.exe diagnostic\start_diagnostic.py
```

The original console tools and `diagnostic/run_diagnostic.bat` are retained for
developers. Their timestamped output under `diagnostic/results/` is unchanged.
They are not the client entry point.

## Behavior and output

Window detection uses case-insensitive English/Russian SPIMEX identifiers in
window titles, process names and class names, plus trading-window title phrases.
Known editor, shell and diagnostic windows are excluded. Browser windows require
both a SPIMEX name and a trading phrase to avoid ordinary SPIMEX website tabs.
This is a heuristic: the actual site's window names must be checked during native
verification. Matching rules live in `diagnostic/client.py`; the client has no
configuration to edit. No matching window means no UI inspection or capture.

Exactly one match starts automatically. Multiple matches show **Select the SPIMEX
window**, a read-only list, and a single **Start** button. No candidate is chosen
by default in that case. All selection occurs in the diagnostic's own GUI.

The original collection engine is reused with the selected HWND and inventory.
Its hidden worker processes re-enter the EXE through a private worker route;
they never start another GUI, interpreter console, or shell. The main GUI stays
responsive and waits for collection to finish before closing. No explicit focus
or activation request is made to SPIMEX or the diagnostic window.

The ZIP contains:

- `windows.txt`: visible window titles, process names, PIDs, classes and HWNDs.
- `ui_tree.txt`: selected UIA subtree, with control type, name/title, automation
  ID, class, bounds and enabled state for each exposed control.
- `screenshot.png`: selected-window image, when supported.
- `system_info.txt`: Windows/Python versions, screen resolution, monitor scaling,
  DPI, dependency versions and selected-window details.
- `diagnostic_log.txt` and, if present, `diagnostic_error.log`.

Every GUI run uses a fresh temporary directory beside the executable. The finished
ZIP is checked and then atomically placed at `spimex_diagnostic.zip` in the **EXE's
own directory**, regardless of the working directory or PyInstaller extraction
location. A completed/partial new report replaces the old ZIP; failed collection
does not label the old ZIP as new. Temporary loose reports are cleaned up.
Technical errors are also retained beside the EXE in `diagnostic_error.log`.
If that directory is unwritable, the GUI asks the client to move the EXE to
Documents; technical logging falls back to `%LOCALAPPDATA%\SPIMEX Diagnostic`.

## Read-only boundary

No SPIMEX controls are clicked, typed into, invoked, selected, expanded, scrolled,
focused, restored or modified. No orders are submitted or selected. Only window
metadata, UI Automation metadata and image rendering are requested. DPI awareness
is changed only for the diagnostic's process. Window/process identity is checked
before inspection and again during collection. There is no upload or trading API.

The screenshot uses Windows `PrintWindow` for **only the selected HWND**, drawing
into a private bitmap. There is no desktop capture or desktop-crop fallback.
Minimized, protected or custom GPU-rendered windows can refuse capture or return
blank images. The report records this; the application is never focused or
restored to improve an image. A non-uniform image can still have missing regions
and needs visual verification on the authorized computer.

UIA exposes only the controls supplied by the target provider. Virtualized items
are not realized. Unreadable properties/branches and collection limits are marked
incomplete. Limits remain 120 seconds for the tree, 30 seconds for the screenshot,
50,000 controls and depth 100. Timeouts stop only the utility's own worker.

The GUI and reports display:

```text
READ-ONLY DIAGNOSTIC - NO ACTIONS WERE PERFORMED
```

## Validation status

**TESTED ON LINUX / Python 3.12.8:** 44 mocked tests passed, including all 22
original tests. New coverage includes automatic selection, multiple matches,
no-window handling, successful ZIP creation beside the EXE, partial results,
friendly errors, file logging, no-console streams, and frozen worker routing.
The tests do not interact with any real application.

```text
python3.12 -m unittest discover -s tests -v
```

Linux tests require Python 3.12 and Pillow. The real Windows libraries are mocked.

**REQUIRES WINDOWS VERIFICATION:** no Windows executable has been built or run
in this Linux workspace. Build on Windows, then test the resulting EXE on a clean
Windows 10/11 x64 computer **without Python installed**. Verify there is no console
or extra worker GUI, test zero/one/multiple candidates, confirm ZIP placement
after moving the EXE, Unicode/spaced paths, error-log behavior and a second run.
On the authorized SPIMEX computer, confirm real title detection, tree/image
content, DPI/scaling and unchanged orders/entered values throughout collection.

References: [PyInstaller build usage](https://pyinstaller.org/en/stable/usage.html),
[frozen runtime paths](https://pyinstaller.org/en/stable/runtime-information.html),
[windowed streams and workers](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html),
[pywinauto UIAElementInfo](https://pywinauto.readthedocs.io/en/latest/code/pywinauto.uia_element_info.html),
[Microsoft PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow).
