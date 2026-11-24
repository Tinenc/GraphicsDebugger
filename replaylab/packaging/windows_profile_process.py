"""Run a disposable GUI on a private desktop and own its complete process tree."""
import ctypes
from ctypes import wintypes as wt
import subprocess
import uuid


def run_gui(executable, fixture, environment, timeout=90):
    # Launch suspended, assign an OS job, then resume. Even detached sidecars stay in this
    # job, so closing it cannot leave services writing into an already removed test profile.
    class Startup(ctypes.Structure):
        _fields_ = [('cb', wt.DWORD), ('reserved', wt.LPWSTR), ('desktop', wt.LPWSTR),
                    ('title', wt.LPWSTR), ('x', wt.DWORD), ('y', wt.DWORD),
                    ('sx', wt.DWORD), ('sy', wt.DWORD), ('cx', wt.DWORD), ('cy', wt.DWORD),
                    ('fill', wt.DWORD), ('flags', wt.DWORD), ('show', wt.WORD),
                    ('reservedSize', wt.WORD), ('reserved2', ctypes.c_void_p),
                    ('stdin', wt.HANDLE), ('stdout', wt.HANDLE), ('stderr', wt.HANDLE)]

    class Process(ctypes.Structure):
        _fields_ = [('process', wt.HANDLE), ('thread', wt.HANDLE),
                    ('pid', wt.DWORD), ('tid', wt.DWORD)]

    class Limits(ctypes.Structure):
        _fields_ = [('processTime', ctypes.c_longlong), ('jobTime', ctypes.c_longlong),
                    ('flags', wt.DWORD), ('minWorkingSet', ctypes.c_size_t),
                    ('maxWorkingSet', ctypes.c_size_t), ('activeProcesses', wt.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wt.DWORD), ('scheduling', wt.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', Limits), ('ioCounters', ctypes.c_ulonglong * 6),
                    ('processMemory', ctypes.c_size_t), ('jobMemory', ctypes.c_size_t),
                    ('peakProcessMemory', ctypes.c_size_t), ('peakJobMemory', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Process)]
    kernel.ResumeThread.argtypes = [wt.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    user.CreateDesktopW.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                   wt.DWORD, wt.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = wt.HANDLE
    user.CloseDesktop.argtypes = [wt.HANDLE]
    job, desktop, process = None, None, Process()
    try:
        job = kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        desktop_name = 'BreadProfile' + uuid.uuid4().hex
        desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x01ff, None)
        if not desktop:
            raise ctypes.WinError(ctypes.get_last_error())
        startup = Startup()
        startup.cb, startup.desktop = ctypes.sizeof(startup), desktop_name
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(
            [str(executable), '--ui-script', str(fixture)]))
        block = ctypes.create_unicode_buffer('\0'.join(
            key + '=' + value for key, value in sorted(environment.items())) + '\0\0')
        # CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED
        if not kernel.CreateProcessW(str(executable), command, None, None, False, 0x08000404,
                block, str(executable.parent), ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, process.process):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.ResumeThread(process.thread) == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(process.process, timeout * 1000) != 0:
            raise TimeoutError('Portable GUI did not exit within the profile test deadline')
        code = wt.DWORD()
        if not kernel.GetExitCodeProcess(process.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value
    finally:
        if process.process:
            kernel.TerminateProcess(process.process, 1)  # no-op after normal GUI exit
        if job:
            kernel.CloseHandle(job)
        if process.process:
            kernel.WaitForSingleObject(process.process, 5000)
            kernel.CloseHandle(process.process)
        if process.thread:
            kernel.CloseHandle(process.thread)
        if desktop:
            user.CloseDesktop(desktop)
