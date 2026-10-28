call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod"
"C:\Program Files\Microsoft Visual Studio\2022\Community\Msbuild\Current\Bin\MSBuild.exe" renderdoc.sln /p:Configuration=Development /p:Platform=x64 /m
