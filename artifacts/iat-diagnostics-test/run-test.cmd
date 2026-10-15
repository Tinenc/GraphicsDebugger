@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul
if errorlevel 1 exit /b %errorlevel%
cl /nologo /EHsc /W4 /WX iat_diagnostics_test.cpp /Fe:iat_diagnostics_test.exe
if errorlevel 1 exit /b %errorlevel%
iat_diagnostics_test.exe
