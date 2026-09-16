@echo off
REM ============================================================
REM  Open the rebuilt 李白谪仙 scene with BlenderMCP auto-connected.
REM
REM  Double-click this file. Nothing else to click: the scene has
REM  blendermcp_auto_start_server=True baked in and the addon is
REM  enabled in user prefs, so port 9876 comes up on open.
REM  (The addon does NOT open the port during register() -- that
REM  scene flag is what actually starts the listener.)
REM
REM  Launch it yourself rather than letting the agent spawn it: a
REM  GUI process started from inside the agent sandbox gets reaped
REM  when the command returns, which kills the MCP server with it.
REM
REM  Blender 5.2.0 LTS is required -- the BlenderMCP addon only
REM  supports 5.0+. Do NOT point this back at the 4.5 install.
REM ============================================================

set BLENDER=E:\blender-5.2.0-windows-x64\blender.exe
set BLENDER_FALLBACK=E:\blender-4.5.5-windows-x64\blender.exe
set SCENE=E:\GST\libai_scene\libai_scene_rebuilt.blend
set BOOT=E:\GST\libai_scene\autostart_mcp.py

if not exist "%BLENDER%" (
  echo [!] Blender 5.2 not found at %BLENDER%
  if exist "%BLENDER_FALLBACK%" (
    echo     A 4.5 install exists at %BLENDER_FALLBACK%
    echo     but BlenderMCP needs 5.0+ -- install Blender 5.2 instead.
  )
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
echo Blender: %BLENDER%
echo BlenderMCP listens on localhost:9876 (auto-start baked into the scene)
start "" "%BLENDER%" "%SCENE%" --python "%BOOT%"
