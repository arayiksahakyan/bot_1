@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "BUILD_EXIT=1"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
if not "%OS%"=="Windows_NT" goto not_windows
pushd "%~dp0"
if errorlevel 1 goto finish

echo Building Start_Diagnostic.exe on Windows with Python 3.12 x64.
if exist ".build-venv\Scripts\python.exe" goto check_python
py -3.12 -c "import sys, struct; sys.exit(sys.platform != 'win32' or sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8)" >nul 2>&1
if errorlevel 1 goto missing_python
py -3.12 -m venv ".build-venv"
if errorlevel 1 goto failure

:check_python
".build-venv\Scripts\python.exe" -c "import sys, struct; sys.exit(sys.platform != 'win32' or sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8)"
if errorlevel 1 goto missing_python
".build-venv\Scripts\python.exe" -m pip install --only-binary=:all: -r "diagnostic\requirements-build.txt"
if errorlevel 1 goto failure
".build-venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failure
".build-venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto failure

rem The checked-in spec sets --onefile and --noconsole (console=False).
rem PyInstaller does not accept those CLI switches together with a spec file.
".build-venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean "Start_Diagnostic.spec"
if errorlevel 1 goto failure
if not exist "dist\Start_Diagnostic.exe" goto failure
".build-venv\Scripts\python.exe" -c "import pefile; p=pefile.PE('dist/Start_Diagnostic.exe'); assert p.FILE_HEADER.Machine == 0x8664; assert p.OPTIONAL_HEADER.Subsystem == 2; print('Verified: x64 Windows GUI executable, no console subsystem.')"
if errorlevel 1 goto failure
echo.
echo Build completed: %CD%\dist\Start_Diagnostic.exe
echo Test on a Windows PC without Python before handing it to the client.
echo Give the client ONLY Start_Diagnostic.exe. No Python or setup is needed.
set "BUILD_EXIT=0"
goto done

:missing_python
echo Install Windows Python 3.12 x64 with Tcl/Tk and the Python launcher.
echo If .build-venv uses a different Python, rename that folder before rebuilding.
goto done

:failure
echo Build failed. Do not distribute an older EXE left in dist.
goto done

:not_windows
echo A Windows executable must be built on Windows. This build cannot cross-compile.
goto finish

:done
popd
:finish
pause
exit /b %BUILD_EXIT%
