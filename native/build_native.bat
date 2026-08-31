@echo off
setlocal
cd /d "%~dp0\.."
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
  echo Visual Studio Installer was not found.
  exit /b 1
)
set "VSROOT="
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT (
  echo Visual Studio C++ Build Tools were not found.
  exit /b 1
)
call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
if not exist "native\build" mkdir "native\build"
if not exist "bin\runtime" mkdir "bin\runtime"
cl /nologo /O2 /EHsc /W3 /MD /Fo:"native\build\\" /I"native\NVIDIA-DLSS\include" ^
  "native\DLSS5-Feeder\host\dlss5-feed-host64.cpp" ^
  /Fe:"bin\runtime\nvngx.dll" ^
  /link "native\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_d.lib" ^
  version.lib kernel32.lib user32.lib gdi32.lib advapi32.lib ole32.lib
exit /b %errorlevel%
