@echo off
setlocal
rem Bread AI: fetch/build the four bundled unpackers into replaylab\third_party\unpackers
rem and mirror them into the deployed application. Runs without prompts; add -Ask to confirm each step.
rem Reading an already-unpacked tree needs no AES key and no profile: Bread AI generates
rem the uex profile from the project's asset root. This only fetches the programs.
rem   Fetch-Unpackers.cmd [-PaksDir "D:\Games\...\Content\Paks"] [-AssetStudioTag v0.19.0] [-GitHubProxyPrefix https://ghfast.top/] [-Ask]
rem   Fetch-Unpackers.cmd -UexSource D:\Study\GitHub\uex          (build uex from your own checkout)
rem   Fetch-Unpackers.cmd -UexExe D:\Study\GitHub\uex\publish\uex.exe   (bundle a uex you already built)
rem   Fetch-Unpackers.cmd -ProfileOnly -PaksDir "D:\Games\...\Content\Paks"   (OPTIONAL pak mode; unpacked trees need no profile)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Fetch-Unpackers.ps1" %*
endlocal
