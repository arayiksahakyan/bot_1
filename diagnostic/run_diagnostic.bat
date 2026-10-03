@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8:backslashreplace"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "DIAGNOSTIC_EXIT=1"
echo READ-ONLY DIAGNOSTIC - NO ACTIONS WERE PERFORMED
echo.
echo This utility reads window information and saves a selected-window image.
echo Enter all responses in THIS console window.
echo.
pushd "%~dp0"
if errorlevel 1 goto folder_error

if exist ".venv\Scripts\python.exe" goto check_venv
py -3.12 -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto try_python
py -3.12 -m venv ".venv"
if errorlevel 1 goto setup_error
goto check_venv

:try_python
python -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto missing_python
python -m venv ".venv"
if errorlevel 1 goto setup_error

:check_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto invalid_venv
echo Checking diagnostic dependencies...
if exist "wheels\*.whl" goto offline_install
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r "requirements.txt"
if errorlevel 1 goto setup_error
goto run

:offline_install
".venv\Scripts\python.exe" -m pip install --no-index --find-links="wheels" --only-binary=:all: -r "requirements.txt"
if errorlevel 1 goto setup_error

:run
echo.
".venv\Scripts\python.exe" -u "inspect_ui.py"
set "DIAGNOSTIC_EXIT=%ERRORLEVEL%"
echo.
if "%DIAGNOSTIC_EXIT%"=="0" echo Diagnostic reports completed. The output paths are printed above.
if "%DIAGNOSTIC_EXIT%"=="2" echo The diagnostic was incomplete or cancelled. Review the saved reports.
if "%DIAGNOSTIC_EXIT%"=="1" echo The diagnostic failed. Review the error printed above.
goto done

:missing_python
echo Python 3.12 was not found. Ask your IT contact to install Python 3.12
echo with the Python launcher, then double-click this file again.
goto done

:invalid_venv
echo The local .venv is not a working Python 3.12 environment.
echo Ask your IT contact to rename the diagnostic .venv folder and retry.
goto done

:setup_error
echo Setup failed. Review the error above or ask your IT contact for help.
echo First-time setup needs package access or an IT-prepared wheels folder.
goto done

:folder_error
echo Cannot access the diagnostic folder. Extract it to a writable local folder.
goto finish

:done
popd
:finish
echo.
echo READ-ONLY DIAGNOSTIC - NO ACTIONS WERE PERFORMED
pause
exit /b %DIAGNOSTIC_EXIT%
