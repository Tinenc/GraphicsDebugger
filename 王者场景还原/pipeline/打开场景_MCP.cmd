@echo off
REM ============================================================
REM  Open the rebuilt 李白谪仙 scene with BlenderMCP auto-connected.
REM
REM  Double-click this file. The BlenderMCP addon auto-starts its
REM  server on localhost:9876 during registration, so the agent can
REM  drive the scene as soon as the window is up.
REM
REM  Launch it yourself rather than letting the agent spawn it: a
REM  GUI process started from inside the agent sandbox gets reaped
REM  when the command returns, which kills the MCP server with it.
REM ============================================================

set BLENDER=E:\blender-4.5.5-windows-x64\blender.exe
set SCENE=E:\GST\libai_scene\libai_scene_rebuilt.blend
set BOOT=E:\GST\libai_scene\autostart_mcp.py

if not exist "%BLENDER%" (
  echo [!] Blender not found at %BLENDER%
  pause
  exit /b 1
)
if not exist "%SCENE%" (
  echo [!] Scene not found at %SCENE%
  echo     Run blender_rebuild.py first.
  pause
  exit /b 1
)

echo Opening %SCENE%
echo BlenderMCP will listen on localhost:9876
start "" "%BLENDER%" "%SCENE%" --python "%BOOT%"
