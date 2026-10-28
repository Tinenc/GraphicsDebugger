@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
cd /d "%~dp0"
cl.exe /nologo /O2 /MD /W3 /LD /D "WIN32" /D "NDEBUG" /D "_WINDOWS" /D "_USRDLL" dxgi_proxy.cpp /Fe"dxgi.dll" /link /DEF:dxgi.def /DLL /MACHINE:X64 user32.lib shell32.lib ole32.lib /SUBSYSTEM:WINDOWS
