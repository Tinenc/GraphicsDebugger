"""Scoped native project permission repair; never an arbitrary AI replay RPC.

The application remains asInvoker. Only a fixed repair operation may ask UAC,
and it grants the invoking user's SID access, even with another admin login.
"""
import base64
import ctypes
import json
import os
import re
import subprocess


def encoded_repair(project_root, workspace_id, user_sid, inspect_only=False):
    root = os.path.abspath(project_root)
    if not os.path.isabs(project_root) or '\0' in project_root or len(root) > 24000:
        raise ValueError('project-permission-invalid-root')
    drive, tail = os.path.splitdrive(root)
    if not tail.strip('/\\') or not re.match(r'^[A-Za-z0-9_.-]{1,128}$', workspace_id):
        raise ValueError('project-permission-invalid-identity')
    if not re.match(r'^S-1-\d+(?:-\d+)+$', user_sid):
        raise ValueError('project-permission-invalid-user')
    payload = base64.b64encode(json.dumps({'projectRoot': root, 'workspaceId': workspace_id,
        'userSid': user_sid, 'inspectOnly': bool(inspect_only)}).encode('utf-8')).decode('ascii')
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'project_permissions.ps1')
    with open(source, 'r', encoding='utf-8-sig') as stream:
        script = stream.read()
    command = "$BreadRepairRequest = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')) | ConvertFrom-Json\n" % payload
    return base64.b64encode((command + script).encode('utf-16-le')).decode('ascii')


def invoking_user_sid():
    from ctypes import wintypes as wt
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_uint, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wt.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wt.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try: return sid_text.value
        finally: kernel.LocalFree(sid_text)
    finally: kernel.CloseHandle(token)


def repair(project_root, workspace_id, automatic=False):
    if os.name != 'nt':
        raise RuntimeError('project-permission-windows-only')
    from ctypes import wintypes as wt
    class ExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wt.DWORD), ('fMask', wt.ULONG), ('hwnd', wt.HWND),
            ('verb', wt.LPCWSTR), ('file', wt.LPCWSTR), ('parameters', wt.LPCWSTR),
            ('directory', wt.LPCWSTR), ('show', ctypes.c_int), ('instance', wt.HINSTANCE),
            ('idList', ctypes.c_void_p), ('className', wt.LPCWSTR), ('classKey', wt.HKEY),
            ('hotkey', wt.DWORD), ('icon', wt.HANDLE), ('process', wt.HANDLE)]
    command = encoded_repair(project_root, workspace_id, invoking_user_sid())
    program = os.path.join(os.environ['SystemRoot'], 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    if automatic:
        # First use the current token. Owned/read-only copied project metadata
        # often needs no elevation. Only the fixed helper's permission exit may
        # request UAC; invalid projects, links and other I/O failures must not.
        result = subprocess.run([program, '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'Bypass', '-EncodedCommand', command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode == 0:
            return {'status': 'repaired', 'elevated': False}
        if result.returncode != 21:
            raise RuntimeError('project-permission-repair-failed: exit %d' % result.returncode)
    info = ExecuteInfo(); info.cbSize = ctypes.sizeof(info); info.fMask = 0x40 | 0x100
    info.verb = 'runas'; info.file = program
    info.parameters = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + command
    info.show = 0
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
    ole = ctypes.OleDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
    initialized = False
    try:
        ole.CoInitializeEx(None, 2)
        initialized = True
        launched = shell.ShellExecuteExW(ctypes.byref(info))
        launch_error = ctypes.get_last_error()
    finally:
        if initialized: ole.CoUninitialize()
    if not launched:
        error = launch_error
        if error == 1223: return {'status': 'cancelled'}
        raise ctypes.WinError(error)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    try:
        if kernel.WaitForSingleObject(info.process, 120000) != 0:
            return {'status': 'pending'}
        code = wt.DWORD()
        if not kernel.GetExitCodeProcess(info.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value: raise RuntimeError('project-permission-repair-failed: exit %d' % code.value)
        return {'status': 'repaired'}
    finally: kernel.CloseHandle(info.process)
