call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
cl.exe /nologo /O2 scripts\resolve.cpp /link dbghelp.lib /out:scripts\resolve.exe
scripts\resolve.exe
