@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x86 >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /EHsc /MT /W4 /WX runtime-loader.cpp /Fe:runtime-loader.exe
