@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /EHsc /W4 /WX d3d12_smoke.cpp /Fe:d3d12_smoke.exe /link d3d12.lib dxgi.lib user32.lib
