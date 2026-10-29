// ============================================================================

// DXGI Proxy DLL - D3D12 Frame Capture Proxy Layer

// ============================================================================

// Features:

//   - Dual Debugger Support: Microsoft PIX and TinecmaTool (RenderDoc)

//   - Bypasses Ricochet anti-cheat process handle stripping (VirtualAllocEx: 5)

//   - Safe Launcher Gating: skips RenderDoc in cod.exe to avoid watchdog crash

//   - Forwards all DXGI calls to real C:\Windows\System32\dxgi.dll

//   - Dual Trigger: F12 hotkey OR Desktop\trigger_capture file

//   - Outputs timestamped capture files directly to Desktop

// ============================================================================



#define WIN32_LEAN_AND_MEAN

#include <windows.h>

#include <string>

#include <cstdio>

#include <ctime>

#include <shlobj.h>



// ---------------------------------------------------------------------------

// Logging & State

// ---------------------------------------------------------------------------

static FILE* g_logFile = nullptr;

static CRITICAL_SECTION g_logLock;

static char g_desktopDir[MAX_PATH] = "C:\\Users\\Administrator\\Desktop";

static char g_modulePath[MAX_PATH] = { 0 };

static char g_processName[MAX_PATH] = { 0 };

static DWORD g_pid = 0;

static bool g_isCodLauncher = false;

static bool g_isSp26Renderer = false;



enum class CaptureMode {

    PIX,

    RenderDoc,

    Both

};

static CaptureMode g_captureMode = CaptureMode::PIX;



static void LogInit() {

    InitializeCriticalSection(&g_logLock);

    if (SUCCEEDED(SHGetFolderPathA(NULL, CSIDL_DESKTOPDIRECTORY, NULL, 0, g_desktopDir))) {

        // ok

    }

    char path[MAX_PATH];

    sprintf_s(path, "%s\\dxgi_proxy.log", g_desktopDir);

    g_logFile = _fsopen(path, "a", _SH_DENYNO);



    g_pid = GetCurrentProcessId();

    GetModuleFileNameA(NULL, g_modulePath, MAX_PATH);



    // Get lower-case base process name

    const char* lastSlash = strrchr(g_modulePath, '\\');

    const char* baseName = lastSlash ? lastSlash + 1 : g_modulePath;

    strcpy_s(g_processName, baseName);

    for (int i = 0; g_processName[i]; ++i) {

        g_processName[i] = (char)tolower(g_processName[i]);

    }



    g_isSp26Renderer = (strstr(g_processName, "sp26") != nullptr);

    g_isCodLauncher = (!g_isSp26Renderer && strstr(g_processName, "cod.exe") != nullptr);



    if (g_logFile) {

        time_t now = time(nullptr);

        char timeBuf[64];

        ctime_s(timeBuf, sizeof(timeBuf), &now);

        fprintf(g_logFile, "\n=== DXGI Proxy Started [PID %lu: %s] === %s", g_pid, g_modulePath, timeBuf);

        fflush(g_logFile);

    }

}



static void Log(const char* fmt, ...) {

    EnterCriticalSection(&g_logLock);

    if (g_logFile) {

        va_list args;

        va_start(args, fmt);

        fprintf(g_logFile, "[%lu:%s] ", g_pid, g_processName);

        vfprintf(g_logFile, fmt, args);

        va_end(args);

        fprintf(g_logFile, "\n");

        fflush(g_logFile);

    }

    LeaveCriticalSection(&g_logLock);

}



// ---------------------------------------------------------------------------

// Configuration

// ---------------------------------------------------------------------------

static void ReadConfig() {

    char iniPath[MAX_PATH];

    sprintf_s(iniPath, "%s\\dxgi_proxy.ini", g_desktopDir);



    char modeBuf[64] = "";

    GetPrivateProfileStringA("proxy", "mode", "", modeBuf, sizeof(modeBuf), iniPath);

    if (modeBuf[0] == '\0') {

        GetPrivateProfileStringA("Capture", "mode", "", modeBuf, sizeof(modeBuf), iniPath);

    }

    if (modeBuf[0] == '\0') {

        strcpy_s(modeBuf, "renderdoc");

    }



    for (int i = 0; modeBuf[i]; ++i) modeBuf[i] = (char)tolower(modeBuf[i]);



    if (strcmp(modeBuf, "renderdoc") == 0 || strcmp(modeBuf, "tinecmatool") == 0) {

        g_captureMode = CaptureMode::RenderDoc;

    } else if (strcmp(modeBuf, "both") == 0) {

        g_captureMode = CaptureMode::Both;

    } else if (strcmp(modeBuf, "pix") == 0) {

        g_captureMode = CaptureMode::PIX;

    } else {

        g_captureMode = CaptureMode::RenderDoc;

    }



    Log("[CONFIG] Active capture mode: %s (ini=%s)", modeBuf, iniPath);

}



// ---------------------------------------------------------------------------

// Real dxgi.dll handle and function pointers

// ---------------------------------------------------------------------------

static HMODULE g_realDxgi = nullptr;



typedef HRESULT(WINAPI* PFN_CreateDXGIFactory)(REFIID, void**);

typedef HRESULT(WINAPI* PFN_CreateDXGIFactory1)(REFIID, void**);

typedef HRESULT(WINAPI* PFN_CreateDXGIFactory2)(UINT, REFIID, void**);

typedef HRESULT(WINAPI* PFN_DXGIDeclareAdapterRemovalSupport)();

typedef HRESULT(WINAPI* PFN_DXGIGetDebugInterface1)(UINT, REFIID, void**);

typedef HRESULT(WINAPI* PFN_DXGIDisableVBlankVirtualization)();



static PFN_CreateDXGIFactory              g_pfnCreateDXGIFactory = nullptr;

static PFN_CreateDXGIFactory1             g_pfnCreateDXGIFactory1 = nullptr;

static PFN_CreateDXGIFactory2             g_pfnCreateDXGIFactory2 = nullptr;

static PFN_DXGIDeclareAdapterRemovalSupport g_pfnDXGIDeclareAdapterRemovalSupport = nullptr;

static PFN_DXGIGetDebugInterface1         g_pfnDXGIGetDebugInterface1 = nullptr;

static PFN_DXGIDisableVBlankVirtualization g_pfnDXGIDisableVBlankVirtualization = nullptr;



// ---------------------------------------------------------------------------

// PIX GPU Capturer Exports

// ---------------------------------------------------------------------------

static HMODULE g_pixCapturer = nullptr;

static bool g_pixLoaded = false;



typedef void(WINAPI* PFN_CaptureNextFrame)(const wchar_t* fileName, UINT numFrames);

typedef void(WINAPI* PFN_BeginProgrammaticGpuCapture)(const wchar_t* fileName);

typedef void(WINAPI* PFN_EndProgrammaticGpuCapture)();



static PFN_CaptureNextFrame              g_pfnCaptureNextFrame = nullptr;

static PFN_BeginProgrammaticGpuCapture    g_pfnBeginProgrammaticGpuCapture = nullptr;

static PFN_EndProgrammaticGpuCapture      g_pfnEndProgrammaticGpuCapture = nullptr;



static void LoadPixCapturer() {

    const char* pixPaths[] = {

        "C:\\Program Files\\Microsoft PIX Preview\\2407.17-preview\\WinPixGpuCapturer.dll",

        "C:\\Program Files\\Microsoft PIX\\WinPixGpuCapturer.dll",

    };



    for (auto& path : pixPaths) {

        g_pixCapturer = LoadLibraryA(path);

        if (g_pixCapturer) {

            Log("[PIX] Successfully loaded WinPixGpuCapturer.dll: %s", path);

            g_pixLoaded = true;



            g_pfnCaptureNextFrame = (PFN_CaptureNextFrame)GetProcAddress(g_pixCapturer, "CaptureNextFrame");

            g_pfnBeginProgrammaticGpuCapture = (PFN_BeginProgrammaticGpuCapture)GetProcAddress(g_pixCapturer, "BeginProgrammaticGpuCapture");

            g_pfnEndProgrammaticGpuCapture = (PFN_EndProgrammaticGpuCapture)GetProcAddress(g_pixCapturer, "EndProgrammaticGpuCapture");



            Log("[PIX] CaptureNextFrame: %p, Begin: %p, End: %p",

                g_pfnCaptureNextFrame, g_pfnBeginProgrammaticGpuCapture, g_pfnEndProgrammaticGpuCapture);

            break;

        }

    }



    if (!g_pixLoaded) {

        Log("[PIX] WARNING: WinPixGpuCapturer.dll not found in standard paths");

    }

}



// ---------------------------------------------------------------------------

// RenderDoc / TinecmaTool Capturer

// ---------------------------------------------------------------------------

#include <intrin.h>



typedef struct _MY_UNICODE_STRING {

    USHORT Length;

    USHORT MaximumLength;

    PWSTR  Buffer;

} MY_UNICODE_STRING;



typedef struct _MY_PEB_LDR_DATA {

    ULONG Length;

    BOOLEAN Initialized;

    PVOID SsHandle;

    LIST_ENTRY InLoadOrderModuleList;

    LIST_ENTRY InMemoryOrderModuleList;

    LIST_ENTRY InInitializationOrderModuleList;

} MY_PEB_LDR_DATA, *PMY_PEB_LDR_DATA;



typedef struct _MY_LDR_DATA_TABLE_ENTRY {

    LIST_ENTRY InLoadOrderLinks;

    LIST_ENTRY InMemoryOrderLinks;

    LIST_ENTRY InInitializationOrderLinks;

    PVOID DllBase;

    PVOID EntryPoint;

    ULONG SizeOfImage;

    MY_UNICODE_STRING FullDllName;

    MY_UNICODE_STRING BaseDllName;

} MY_LDR_DATA_TABLE_ENTRY, *PMY_LDR_DATA_TABLE_ENTRY;



static bool UnlinkModuleFromPEB(HMODULE hMod) {

    if (!hMod) return false;

    unsigned char* ppeb = (unsigned char*)__readgsqword(0x60);

    if (!ppeb) return false;

    PMY_PEB_LDR_DATA ldr = *(PMY_PEB_LDR_DATA*)(ppeb + 0x18);

    if (!ldr) return false;



    PLIST_ENTRY head = &ldr->InLoadOrderModuleList;

    PLIST_ENTRY curr = head->Flink;

    while (curr != head) {

        PMY_LDR_DATA_TABLE_ENTRY entry = (PMY_LDR_DATA_TABLE_ENTRY)curr;

        if (entry->DllBase == (PVOID)hMod) {

            entry->InLoadOrderLinks.Blink->Flink = entry->InLoadOrderLinks.Flink;

            entry->InLoadOrderLinks.Flink->Blink = entry->InLoadOrderLinks.Blink;

            entry->InMemoryOrderLinks.Blink->Flink = entry->InMemoryOrderLinks.Flink;

            entry->InMemoryOrderLinks.Flink->Blink = entry->InMemoryOrderLinks.Blink;

            entry->InInitializationOrderLinks.Blink->Flink = entry->InInitializationOrderLinks.Flink;

            entry->InInitializationOrderLinks.Flink->Blink = entry->InInitializationOrderLinks.Blink;

            return true;

        }

        curr = curr->Flink;

    }

    return false;

}



static HMODULE g_rdocCapturer = nullptr;

static bool g_rdocLoaded = false;



// RenderDoc official in-app API

#include "C:\\Program Files\\GraphicsDebugger-Steam-Cod\\renderdoc\\api\\app\\renderdoc_app.h"

typedef int(RENDERDOC_CC *pRENDERDOC_GetAPI)(RENDERDOC_Version version, void **outAPIPointers);

static RENDERDOC_API_1_4_1* g_rdocApi = nullptr;



typedef HRESULT(WINAPI* PFN_TINECMATOOL_CreateDXGIFactory)(REFIID, void**);

typedef HRESULT(WINAPI* PFN_TINECMATOOL_CreateDXGIFactory1)(REFIID, void**);

typedef HRESULT(WINAPI* PFN_TINECMATOOL_CreateDXGIFactory2)(UINT, REFIID, void**);



static PFN_TINECMATOOL_CreateDXGIFactory  g_pfnRdocCreateDXGIFactory = nullptr;

static PFN_TINECMATOOL_CreateDXGIFactory1 g_pfnRdocCreateDXGIFactory1 = nullptr;

static PFN_TINECMATOOL_CreateDXGIFactory2 g_pfnRdocCreateDXGIFactory2 = nullptr;



static bool IsTargetRenderer() {

    return (_stricmp(g_processName, "sp26-cod.exe") == 0 ||

            _stricmp(g_processName, "cod.exe") == 0 ||
            _stricmp(g_processName, "modernwarfare.exe") == 0);

}



static void LoadRenderDocCapturer() {

    // Only load TinecmaTool into the actual D3D12 renderer (sp26-cod.exe).

    // Launchers (bootstrapper.exe, cod.exe, crashhandlers) must be completely untouched.

    if (!IsTargetRenderer()) {

        Log("[RenderDoc] SKIP: Process %s is neither sp26-cod.exe nor cod.exe. Forwarding directly to system DXGI.", g_processName);

        return;

    }



    Log("[RenderDoc] Preparing to load TinecmaTool into target renderer: %s (PID: %lu)", g_processName, g_pid);



    char iniPath[MAX_PATH];

    sprintf_s(iniPath, "%s\\dxgi_proxy.ini", g_desktopDir);

    char customRdoc[MAX_PATH] = "";

    GetPrivateProfileStringA("proxy", "rdoc_dll", "", customRdoc, sizeof(customRdoc), iniPath);

    if (customRdoc[0] == '\0') {

        GetPrivateProfileStringA("Capture", "rdoc_dll", "", customRdoc, sizeof(customRdoc), iniPath);

    }

    const char* rdocPaths[4];

    int pathCount = 0;

    if (customRdoc[0] != '\0') {

        rdocPaths[pathCount++] = customRdoc;

    }

    rdocPaths[pathCount++] = "C:\\Program Files\\GraphicsDebugger-Steam-Cod\\x64\\Development\\TinecmaTool.dll";

    rdocPaths[pathCount++] = "C:\\Program Files\\RenderDoc\\renderdoc.dll";

    for (int pIdx = 0; pIdx < pathCount; ++pIdx) {

        const char* path = rdocPaths[pIdx];

        g_rdocCapturer = LoadLibraryA(path);

        if (g_rdocCapturer) {

            Log("[RenderDoc] Successfully loaded %s into %s", path, g_processName);

            g_rdocLoaded = true;



            if (UnlinkModuleFromPEB(g_rdocCapturer)) {

                Log("[RenderDoc] Unlinked %s from PEB LDR lists successfully", path);

            } else {

                Log("[RenderDoc] Note: UnlinkModuleFromPEB returned false");

            }



            pRENDERDOC_GetAPI getApi = (pRENDERDOC_GetAPI)GetProcAddress(g_rdocCapturer, "TINECMATOOL_GetAPI");

            if (!getApi) {

                getApi = (pRENDERDOC_GetAPI)GetProcAddress(g_rdocCapturer, "RENDERDOC_GetAPI");

            }



            if (getApi) {

                int res = getApi(eRENDERDOC_API_Version_1_4_1, (void**)&g_rdocApi);

                Log("[RenderDoc] GetAPI result=%d, ApiPtr=%p, TriggerCapture=%p",

                    res, g_rdocApi, g_rdocApi ? (void*)g_rdocApi->TriggerCapture : nullptr);

                if (g_rdocApi && g_rdocApi->SetCaptureFilePathTemplate) {

                    char cleanName[64];

                    strcpy_s(cleanName, g_processName);

                    char* dot = strchr(cleanName, '.');

                    if (dot) *dot = '\0';



                    char capTemplate[MAX_PATH];

                    sprintf_s(capTemplate, "%s\\COD_RDC_%s", g_desktopDir, cleanName);

                    g_rdocApi->SetCaptureFilePathTemplate(capTemplate);

                    Log("[RenderDoc] SetCaptureFilePathTemplate to: %s", capTemplate);



                    typedef void(__cdecl* PFN_INTERNAL_SetDebugLogFile)(const char*);

                    PFN_INTERNAL_SetDebugLogFile setLog = (PFN_INTERNAL_SetDebugLogFile)GetProcAddress(g_rdocCapturer, "INTERNAL_SetDebugLogFile");

                    if (setLog) {

                        char rdcLog[MAX_PATH];

                        sprintf_s(rdcLog, "%s\\rdoc_debug_%s.log", g_desktopDir, cleanName);

                        setLog(rdcLog);

                        Log("[RenderDoc] Configured internal debug log: %s", rdcLog);

                    }

                }



                g_pfnRdocCreateDXGIFactory = (PFN_TINECMATOOL_CreateDXGIFactory)GetProcAddress(g_rdocCapturer, "TINECMATOOL_CreateDXGIFactory");

                g_pfnRdocCreateDXGIFactory1 = (PFN_TINECMATOOL_CreateDXGIFactory1)GetProcAddress(g_rdocCapturer, "TINECMATOOL_CreateDXGIFactory1");

                g_pfnRdocCreateDXGIFactory2 = (PFN_TINECMATOOL_CreateDXGIFactory2)GetProcAddress(g_rdocCapturer, "TINECMATOOL_CreateDXGIFactory2");

                Log("[RenderDoc:Bridge] Factory exports: Create=%p, Create1=%p, Create2=%p",

                    g_pfnRdocCreateDXGIFactory, g_pfnRdocCreateDXGIFactory1, g_pfnRdocCreateDXGIFactory2);

            } else {

                Log("[RenderDoc] Warning: GetAPI export not found in %s", path);

            }

            break;

        }

    }



    if (!g_rdocLoaded) {

        Log("[RenderDoc] WARNING: TinecmaTool.dll / renderdoc.dll not found");

    }

}



// ---------------------------------------------------------------------------

// Load real system dxgi.dll

// ---------------------------------------------------------------------------

static bool LoadRealDxgi() {

    char sysDir[MAX_PATH];

    GetSystemDirectoryA(sysDir, MAX_PATH);

    std::string realPath = std::string(sysDir) + "\\dxgi.dll";



    g_realDxgi = LoadLibraryA(realPath.c_str());

    if (!g_realDxgi) {

        Log("[ERROR] Failed to load system dxgi.dll: %s (error: %lu)", realPath.c_str(), GetLastError());

        return false;

    }



    Log("[DXGI] Loaded system dxgi.dll: %s", realPath.c_str());



    g_pfnCreateDXGIFactory = (PFN_CreateDXGIFactory)GetProcAddress(g_realDxgi, "CreateDXGIFactory");

    g_pfnCreateDXGIFactory1 = (PFN_CreateDXGIFactory1)GetProcAddress(g_realDxgi, "CreateDXGIFactory1");

    g_pfnCreateDXGIFactory2 = (PFN_CreateDXGIFactory2)GetProcAddress(g_realDxgi, "CreateDXGIFactory2");

    g_pfnDXGIDeclareAdapterRemovalSupport = (PFN_DXGIDeclareAdapterRemovalSupport)GetProcAddress(g_realDxgi, "DXGIDeclareAdapterRemovalSupport");

    g_pfnDXGIGetDebugInterface1 = (PFN_DXGIGetDebugInterface1)GetProcAddress(g_realDxgi, "DXGIGetDebugInterface1");

    g_pfnDXGIDisableVBlankVirtualization = (PFN_DXGIDisableVBlankVirtualization)GetProcAddress(g_realDxgi, "DXGIDisableVBlankVirtualization");



    return true;

}



// ---------------------------------------------------------------------------

// Hotkey & Trigger Thread

// ---------------------------------------------------------------------------

static volatile bool g_capturing = false;

static volatile bool g_threadRunning = true;



static DWORD WINAPI TriggerMonitorThread(LPVOID) {

    char cleanName[64];

    strcpy_s(cleanName, g_processName);

    char* dot = strchr(cleanName, '.');

    if (dot) *dot = '\0';



    Log("[Trigger] Monitor thread started for %s. F12 or Desktop\\trigger_capture will trigger capture.", cleanName);



    char genericTrigger[MAX_PATH];

    sprintf_s(genericTrigger, "%s\\trigger_capture", g_desktopDir);



    char specificTrigger[MAX_PATH];

    sprintf_s(specificTrigger, "%s\\trigger_capture_%s", g_desktopDir, cleanName);



    FILETIME lastGenericTime = { 0, 0 };
    WIN32_FILE_ATTRIBUTE_DATA fileDataInit;
    if (GetFileAttributesExA(genericTrigger, GetFileExInfoStandard, &fileDataInit)) {
        lastGenericTime = fileDataInit.ftLastWriteTime;
    }



    while (g_threadRunning) {

        bool f12Pressed = (GetAsyncKeyState(VK_F12) & 1) != 0;

        bool fileTrigger = false;



        if (GetFileAttributesA(specificTrigger) != INVALID_FILE_ATTRIBUTES) {

            fileTrigger = true;

            DeleteFileA(specificTrigger);

            Log("[Trigger] Process-specific file trigger detected: %s", specificTrigger);

        }



        WIN32_FILE_ATTRIBUTE_DATA fileData;

        if (GetFileAttributesExA(genericTrigger, GetFileExInfoStandard, &fileData)) {

            if (CompareFileTime(&fileData.ftLastWriteTime, &lastGenericTime) > 0) {

                lastGenericTime = fileData.ftLastWriteTime;
                DeleteFileA(genericTrigger);

                fileTrigger = true;

                Log("[Trigger] Generic file trigger detected for %s (time: %lu)", cleanName, fileData.ftLastWriteTime.dwLowDateTime);

            }

        }



        if ((f12Pressed || fileTrigger) && !g_capturing) {

            g_capturing = true;



            SYSTEMTIME st;

            GetLocalTime(&st);



            // 1. PIX Capture

            if (g_pixLoaded && (g_captureMode == CaptureMode::PIX || g_captureMode == CaptureMode::Both)) {

                wchar_t pixPath[MAX_PATH];

                swprintf_s(pixPath, MAX_PATH, L"%hs\\COD_PIX_%hs_%04d%02d%02d_%02d%02d%02d.wpix",

                    g_desktopDir, cleanName, st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);



                Log("[Capture:PIX] Starting capture to: %ls", pixPath);

                if (g_pfnCaptureNextFrame) {

                    g_pfnCaptureNextFrame(pixPath, 1);

                    Log("[Capture:PIX] CaptureNextFrame(1) issued successfully!");

                } else if (g_pfnBeginProgrammaticGpuCapture && g_pfnEndProgrammaticGpuCapture) {

                    g_pfnBeginProgrammaticGpuCapture(pixPath);

                    Sleep(50);

                    g_pfnEndProgrammaticGpuCapture();

                    Log("[Capture:PIX] Begin/EndProgrammaticGpuCapture issued successfully!");

                }

            }



            // 2. RenderDoc Capture

            if (g_rdocLoaded && (g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both)) {

                Log("[Capture:RenderDoc] [%s] Triggering frame capture via in-app API...", cleanName);

                if (g_rdocApi && g_rdocApi->TriggerCapture) {

                    uint32_t prevCount = g_rdocApi->GetNumCaptures ? g_rdocApi->GetNumCaptures() : 0;

                    g_rdocApi->TriggerCapture();

                    Log("[Capture:RenderDoc] [%s] TriggerCapture() called successfully! (prevCaptures=%u)", cleanName, prevCount);



                    if (g_rdocApi->GetNumCaptures && g_rdocApi->GetCapture) {

                        for (int w = 0; w < 400; ++w) {

                            Sleep(500);

                            if (g_rdocApi->GetNumCaptures() > prevCount) {

                                char capFile[MAX_PATH] = { 0 };

                                uint32_t pathLen = MAX_PATH;

                                uint64_t timestamp = 0;

                                g_rdocApi->GetCapture(prevCount, capFile, &pathLen, &timestamp);

                                Log("[Capture:RenderDoc] [%s] >>> FRAME CAPTURE COMPLETE! File: %s (idx: %u)",

                                    cleanName, capFile, prevCount);



                                char doneFile[MAX_PATH];

                                sprintf_s(doneFile, "%s\\capture_completed.txt", g_desktopDir);

                                FILE* fp = fopen(doneFile, "w");

                                if (fp) {

                                    fprintf(fp, "%s\n", capFile);

                                    fclose(fp);

                                }

                                break;

                            }

                        }

                    }

                } else {

                    Log("[Capture:RenderDoc] API pointer or TriggerCapture is null");

                }

            }



            // Debounce

            Sleep(2000);

            g_capturing = false;

        }



        Sleep(100);

    }

    return 0;

}



// ---------------------------------------------------------------------------

// Exported Functions - Forwarded to system dxgi.dll

// ---------------------------------------------------------------------------

extern "C" {



__declspec(dllexport) HRESULT WINAPI CreateDXGIFactory(REFIID riid, void** ppFactory) {

    if ((g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both) && g_pfnRdocCreateDXGIFactory) {

        Log("[Bridge:RenderDoc] Routing CreateDXGIFactory to TinecmaTool...");

        HRESULT hr = g_pfnRdocCreateDXGIFactory(riid, ppFactory);

        Log("[Bridge:RenderDoc] CreateDXGIFactory returned hr=0x%08X (ppFactory=%p)", hr, ppFactory ? *ppFactory : nullptr);

        return hr;

    }

    HRESULT hr = g_pfnCreateDXGIFactory ? g_pfnCreateDXGIFactory(riid, ppFactory) : E_FAIL;

    Log("CreateDXGIFactory returned hr=0x%08X", hr);

    return hr;

}



__declspec(dllexport) HRESULT WINAPI CreateDXGIFactory1(REFIID riid, void** ppFactory) {

    if ((g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both) && g_pfnRdocCreateDXGIFactory1) {

        Log("[Bridge:RenderDoc] Routing CreateDXGIFactory1 to TinecmaTool...");

        HRESULT hr = g_pfnRdocCreateDXGIFactory1(riid, ppFactory);

        Log("[Bridge:RenderDoc] CreateDXGIFactory1 returned hr=0x%08X (ppFactory=%p)", hr, ppFactory ? *ppFactory : nullptr);

        return hr;

    }

    HRESULT hr = g_pfnCreateDXGIFactory1 ? g_pfnCreateDXGIFactory1(riid, ppFactory) : E_FAIL;

    Log("CreateDXGIFactory1 returned hr=0x%08X", hr);

    return hr;

}



__declspec(dllexport) HRESULT WINAPI CreateDXGIFactory2(UINT flags, REFIID riid, void** ppFactory) {

    if ((g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both) && g_pfnRdocCreateDXGIFactory2) {

        Log("[Bridge:RenderDoc] Routing CreateDXGIFactory2(flags=0x%08X) to TinecmaTool...", flags);

        HRESULT hr = g_pfnRdocCreateDXGIFactory2(flags, riid, ppFactory);

        Log("[Bridge:RenderDoc] CreateDXGIFactory2 returned hr=0x%08X (ppFactory=%p)", hr, ppFactory ? *ppFactory : nullptr);

        return hr;

    }

    HRESULT hr = g_pfnCreateDXGIFactory2 ? g_pfnCreateDXGIFactory2(flags, riid, ppFactory) : E_FAIL;

    Log("CreateDXGIFactory2(flags=0x%08X) returned hr=0x%08X", flags, hr);

    return hr;

}



__declspec(dllexport) HRESULT WINAPI DXGIDeclareAdapterRemovalSupport() {

    return g_pfnDXGIDeclareAdapterRemovalSupport ? g_pfnDXGIDeclareAdapterRemovalSupport() : E_FAIL;

}



__declspec(dllexport) HRESULT WINAPI DXGIGetDebugInterface1(UINT flags, REFIID riid, void** ppDebug) {

    return g_pfnDXGIGetDebugInterface1 ? g_pfnDXGIGetDebugInterface1(flags, riid, ppDebug) : E_FAIL;

}



__declspec(dllexport) HRESULT WINAPI DXGIDisableVBlankVirtualization() {

    return g_pfnDXGIDisableVBlankVirtualization ? g_pfnDXGIDisableVBlankVirtualization() : E_FAIL;

}



} // extern "C"



// ---------------------------------------------------------------------------

// DllMain Entry Point

// ---------------------------------------------------------------------------

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {

    switch (fdwReason) {

    case DLL_PROCESS_ATTACH:

        DisableThreadLibraryCalls(hinstDLL);

        LogInit();

        ReadConfig();

        Log("=== Initializing DXGI Proxy Layer ===");



        // Step 1: Pre-load Capturer before any D3D12/DXGI device is created

        if (g_captureMode == CaptureMode::PIX || g_captureMode == CaptureMode::Both) {

            LoadPixCapturer();

        }

        if (g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both) {

            LoadRenderDocCapturer();

        }



        // Step 2: Load system dxgi.dll

        if (!LoadRealDxgi()) {

            Log("[FATAL] Cannot load system dxgi.dll");

            return FALSE;

        }



        // Step 3: Spawn background trigger monitor thread (only for sp26-cod.exe)

        if (IsTargetRenderer()) {

            CreateThread(nullptr, 0, TriggerMonitorThread, nullptr, 0, nullptr);

        }



        Log("=== DXGI Proxy Init Complete ===");

        break;



    case DLL_PROCESS_DETACH:

        g_threadRunning = false;

        if (g_logFile) {

            Log("=== DXGI Proxy Unloading ===");

            fclose(g_logFile);

            g_logFile = nullptr;

        }

        DeleteCriticalSection(&g_logLock);

        if (g_realDxgi) FreeLibrary(g_realDxgi);

        if (g_pixCapturer) FreeLibrary(g_pixCapturer);

        if (g_rdocCapturer) FreeLibrary(g_rdocCapturer);

        break;

    }

    return TRUE;

}

