@echo off
setlocal

set SCRIPT=%~dp0dead_reckoning.py

set PY="C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
if exist %PY% goto :launch

set PY="%LOCALAPPDATA%\Programs\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
if exist %PY% goto :launch

set PY=pythonw.exe
where pythonw.exe >nul 2>&1
if %errorlevel%==0 goto :launch

echo Could not find a Python interpreter.
echo Edit this file and set PY to the full path of python.exe.
pause
exit /b 1

:launch
start "" %PY% "%SCRIPT%" %*
endlocal
