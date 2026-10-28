@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
cd /d "%~dp0"

echo [1/2] Building dxgi.dll...
cl.exe /nologo /O2 /MD /W3 /LD /D "WIN32" /D "NDEBUG" /D "_WINDOWS" /D "_USRDLL" dxgi_proxy.cpp /Fe"dxgi.dll" /link /DEF:dxgi.def /DLL /MACHINE:X64 user32.lib shell32.lib ole32.lib /SUBSYSTEM:WINDOWS
if %errorlevel% neq 0 (
    echo [ERROR] Failed to build dxgi.dll!
    exit /b 1
)

echo [2/2] Building d3d12.dll...
cl.exe /nologo /O2 /MD /W3 /LD /D "WIN32" /D "NDEBUG" /D "_WINDOWS" /D "_USRDLL" d3d12_proxy.cpp /Fe"d3d12.dll" /link /DEF:d3d12.def /DLL /MACHINE:X64 dxgi.lib user32.lib shell32.lib /SUBSYSTEM:WINDOWS
if %errorlevel% neq 0 (
    echo [ERROR] Failed to build d3d12.dll!
    exit /b 1
)

echo [SUCCESS] Both dxgi.dll and d3d12.dll built successfully.
endlocal
