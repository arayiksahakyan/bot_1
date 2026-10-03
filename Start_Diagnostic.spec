# Build ON WINDOWS with Python 3.12 x64: py -3.12 -m PyInstaller Start_Diagnostic.spec
# One EXE (no COLLECT step); console=False is PyInstaller's --noconsole option.
from pathlib import Path
import sys

if sys.platform != "win32":
    raise SystemExit("Start_Diagnostic.exe must be built on Windows, not cross-compiled.")
if sys.version_info[:2] != (3, 12):
    raise SystemExit("Build with Python 3.12.")

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
import comtypes.client

# Generate and bundle the dynamic UI Automation COM wrappers on the build PC.
# This does not enumerate or interact with application windows.
comtypes.client.GetModule("UIAutomationCore.dll")
root = Path(SPECPATH)
metadata = []
for package in ("pywinauto", "pywin32", "comtypes", "psutil", "Pillow", "six"):
    metadata += copy_metadata(package)
metadata += collect_data_files("comtypes.gen", include_py_files=True)
hidden = collect_submodules("comtypes.gen") + [
    "comtypes.gen.UIAutomationClient", "comtypes.stream", "comtypes.persist",
    "comtypes.typeinfo", "comtypes.automation", "pythoncom", "pywintypes",
    "win32api", "win32gui", "win32process", "PIL.PngImagePlugin",
]

analysis = Analysis(
    [str(root / "diagnostic" / "start_diagnostic.py")],
    pathex=[str(root / "diagnostic")],
    binaries=[], datas=metadata, hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest"], noarchive=False,
)
archive = PYZ(analysis.pure)
exe = EXE(
    archive, analysis.scripts, analysis.binaries, analysis.datas, [],
    name="Start_Diagnostic", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=True,
    uac_admin=False, uac_uiaccess=False,
)
