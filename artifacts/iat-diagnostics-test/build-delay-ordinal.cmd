@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul
if errorlevel 1 exit /b %errorlevel%
lib /nologo /def:d3d12-ordinal.def /machine:x64 /out:d3d12-ordinal.lib
if errorlevel 1 exit /b %errorlevel%
cl /nologo /EHsc /W4 /WX d3d12_smoke.cpp /Fe:d3d12_delay_ordinal.exe /link d3d12-ordinal.lib delayimp.lib dxgi.lib user32.lib /DELAYLOAD:d3d12.dll
