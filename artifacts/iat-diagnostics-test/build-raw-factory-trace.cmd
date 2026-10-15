@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvarsall.bat" x64 -vcvars_ver=14.44 >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /EHsc /W4 /WX d3d12_raw_factory_trace.cpp /Fe:d3d12_raw_factory_trace.exe /link d3d12-ordinal.lib delayimp.lib dxgi.lib user32.lib /DELAYLOAD:d3d12.dll
