// ============================================================================
// D3D12 Proxy DLL - D3D12 Device Creation Bridge
// ============================================================================
// Pairs with dxgi_proxy.dll:
// Routes D3D12CreateDevice (Ordinal 101) directly to TinecmaTool (RenderDoc),
// ensuring the created ID3D12Device is wrapped and matched with WrappedIDXGIFactory.
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi.h>
#include <cstdio>
#include <ctime>
#include <share.h>
#include <shlobj.h>

static FILE* g_logFile = nullptr;
static CRITICAL_SECTION g_logLock;
static char g_desktopDir[MAX_PATH] = "C:\\Users\\Administrator\\Desktop";
static char g_modulePath[MAX_PATH] = { 0 };
static char g_processName[MAX_PATH] = { 0 };
static DWORD g_pid = 0;
static char g_rdocPath[MAX_PATH] = "C:\\Program Files\\GraphicsDebugger-Steam-Cod\\x64\\Development\\TinecmaTool.dll";

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

    const char* lastSlash = strrchr(g_modulePath, '\\');
    const char* baseName = lastSlash ? lastSlash + 1 : g_modulePath;
    strcpy_s(g_processName, baseName);
    for (int i = 0; g_processName[i]; ++i) {
        g_processName[i] = (char)tolower(g_processName[i]);
    }
}

static void Log(const char* fmt, ...) {
    EnterCriticalSection(&g_logLock);
    if (g_logFile) {
        va_list args;
        va_start(args, fmt);
        fprintf(g_logFile, "[%lu:%s][D3D12] ", g_pid, g_processName);
        vfprintf(g_logFile, fmt, args);
        va_end(args);
        fprintf(g_logFile, "\n");
        fflush(g_logFile);
    }
    LeaveCriticalSection(&g_logLock);
}

static void ReadConfig() {
    char iniPath[MAX_PATH];
    sprintf_s(iniPath, "%s\\dxgi_proxy.ini", g_desktopDir);

    char modeBuf[64] = "";
    // First try [proxy], then fallback to [Capture]
    GetPrivateProfileStringA("proxy", "mode", "", modeBuf, sizeof(modeBuf), iniPath);
    if (modeBuf[0] == '\0') {
        GetPrivateProfileStringA("Capture", "mode", "pix", modeBuf, sizeof(modeBuf), iniPath);
    }
    for (int i = 0; modeBuf[i]; ++i) modeBuf[i] = (char)tolower(modeBuf[i]);

    if (strcmp(modeBuf, "renderdoc") == 0 || strcmp(modeBuf, "tinecmatool") == 0) {
        g_captureMode = CaptureMode::RenderDoc;
    } else if (strcmp(modeBuf, "both") == 0) {
        g_captureMode = CaptureMode::Both;
    } else {
        g_captureMode = CaptureMode::PIX;
    }

    char rdocBuf[MAX_PATH] = "";
    GetPrivateProfileStringA("proxy", "rdoc_dll", "", rdocBuf, sizeof(rdocBuf), iniPath);
    if (rdocBuf[0] == '\0') {
        GetPrivateProfileStringA("Capture", "rdoc_dll", "", rdocBuf, sizeof(rdocBuf), iniPath);
    }
    if (rdocBuf[0] != '\0') {
        strcpy_s(g_rdocPath, rdocBuf);
    }
}

static bool IsTargetRenderer() {
    return (_stricmp(g_processName, "sp26-cod.exe") == 0 ||
            _stricmp(g_processName, "cod.exe") == 0 ||
            _stricmp(g_processName, "modernwarfare.exe") == 0);
}

// ---------------------------------------------------------------------------
// Real System d3d12.dll
// ---------------------------------------------------------------------------
static HMODULE g_realD3D12 = nullptr;

typedef HRESULT(WINAPI* PFN_D3D12CreateDevice)(IUnknown*, D3D_FEATURE_LEVEL, REFIID, void**);
typedef HRESULT(WINAPI* PFN_D3D12GetDebugInterface)(REFIID, void**);
typedef HRESULT(WINAPI* PFN_D3D12GetInterface)(REFCLSID, REFIID, void**);
typedef HRESULT(WINAPI* PFN_D3D12SerializeRootSignature)(const D3D12_ROOT_SIGNATURE_DESC*, D3D_ROOT_SIGNATURE_VERSION, ID3DBlob**, ID3DBlob**);
typedef HRESULT(WINAPI* PFN_D3D12SerializeVersionedRootSignature)(const D3D12_VERSIONED_ROOT_SIGNATURE_DESC*, ID3DBlob**, ID3DBlob**);
typedef HRESULT(WINAPI* PFN_D3D12EnableExperimentalFeatures)(UINT, const IID*, void*, UINT*);

static PFN_D3D12CreateDevice                   g_realD3D12CreateDevice = nullptr;
static PFN_D3D12GetDebugInterface              g_realD3D12GetDebugInterface = nullptr;
static PFN_D3D12GetInterface                   g_realD3D12GetInterface = nullptr;
static PFN_D3D12SerializeRootSignature         g_realD3D12SerializeRootSignature = nullptr;
static PFN_D3D12SerializeVersionedRootSignature g_realD3D12SerializeVersionedRootSignature = nullptr;
static PFN_D3D12EnableExperimentalFeatures     g_realD3D12EnableExperimentalFeatures = nullptr;

// Generic pointers for other ordinals
static FARPROC g_procTable[128] = { 0 };

// TinecmaTool D3D12 export
typedef HRESULT(WINAPI* PFN_TINECMATOOL_D3D12CreateDevice)(IUnknown*, D3D_FEATURE_LEVEL, REFIID, void**);
static PFN_TINECMATOOL_D3D12CreateDevice g_pfnRdocD3D12CreateDevice = nullptr;

static bool LoadRealD3D12() {
    char sysDir[MAX_PATH];
    GetSystemDirectoryA(sysDir, MAX_PATH);
    char realPath[MAX_PATH];
    sprintf_s(realPath, "%s\\d3d12.dll", sysDir);

    g_realD3D12 = LoadLibraryA(realPath);
    if (!g_realD3D12) {
        Log("[ERROR] Failed to load system d3d12.dll from %s", realPath);
        return false;
    }
    Log("Loaded system d3d12.dll: %s", realPath);

    g_realD3D12CreateDevice = (PFN_D3D12CreateDevice)GetProcAddress(g_realD3D12, "D3D12CreateDevice");
    g_realD3D12GetDebugInterface = (PFN_D3D12GetDebugInterface)GetProcAddress(g_realD3D12, "D3D12GetDebugInterface");
    g_realD3D12GetInterface = (PFN_D3D12GetInterface)GetProcAddress(g_realD3D12, "D3D12GetInterface");
    g_realD3D12SerializeRootSignature = (PFN_D3D12SerializeRootSignature)GetProcAddress(g_realD3D12, "D3D12SerializeRootSignature");
    g_realD3D12SerializeVersionedRootSignature = (PFN_D3D12SerializeVersionedRootSignature)GetProcAddress(g_realD3D12, "D3D12SerializeVersionedRootSignature");
    g_realD3D12EnableExperimentalFeatures = (PFN_D3D12EnableExperimentalFeatures)GetProcAddress(g_realD3D12, "D3D12EnableExperimentalFeatures");

    // Populate ordinals 99-117
    for (int ord = 99; ord <= 117; ++ord) {
        g_procTable[ord] = GetProcAddress(g_realD3D12, MAKEINTRESOURCEA(ord));
    }

    return true;
}

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

static void LoadTinecmaToolD3D12() {
    if (!IsTargetRenderer()) return;

    HMODULE hTool = GetModuleHandleA("TinecmaTool.dll");
    if (!hTool) {
        hTool = LoadLibraryA(g_rdocPath);
    }
    if (hTool) {
        UnlinkModuleFromPEB(hTool);
        g_pfnRdocD3D12CreateDevice = (PFN_TINECMATOOL_D3D12CreateDevice)GetProcAddress(hTool, "TINECMATOOL_D3D12CreateDevice");
        Log("Resolved TINECMATOOL_D3D12CreateDevice: %p (from %s)", g_pfnRdocD3D12CreateDevice, g_rdocPath);
    } else {
        Log("[WARN] TinecmaTool.dll not found for D3D12 bridge at %s", g_rdocPath);
    }
}

// ---------------------------------------------------------------------------
// Export Implementations
// ---------------------------------------------------------------------------
static thread_local bool t_inD3D12Create = false;

extern "C" {

HRESULT WINAPI D3D12CreateDevice(IUnknown* pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid, void** ppDevice) {
    if (t_inD3D12Create) {
        // TinecmaTool re-entered D3D12CreateDevice via GetProcAddress: provide real implementation!
        Log("[Bridge:D3D12] Callback from TinecmaTool detected -> Forwarding to real system D3D12CreateDevice");
        return g_realD3D12CreateDevice ? g_realD3D12CreateDevice(pAdapter, MinimumFeatureLevel, riid, ppDevice) : E_FAIL;
    }

    if ((g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both) && g_pfnRdocD3D12CreateDevice) {
        Log("[Bridge:RenderDoc] Routing D3D12CreateDevice to TinecmaTool (FeatureLevel 0x%X)...", MinimumFeatureLevel);
        t_inD3D12Create = true;
        HRESULT hr = g_pfnRdocD3D12CreateDevice(pAdapter, MinimumFeatureLevel, riid, ppDevice);
        t_inD3D12Create = false;
        Log("[Bridge:RenderDoc] D3D12CreateDevice returned hr=0x%08X (ppDevice=%p)", hr, ppDevice ? *ppDevice : nullptr);
        return hr;
    }
    if (g_realD3D12CreateDevice) {
        return g_realD3D12CreateDevice(pAdapter, MinimumFeatureLevel, riid, ppDevice);
    }
    return E_FAIL;
}

HRESULT WINAPI D3D12GetDebugInterface(REFIID riid, void** ppvDebug) {
    if (g_realD3D12GetDebugInterface) return g_realD3D12GetDebugInterface(riid, ppvDebug);
    return E_FAIL;
}

HRESULT WINAPI D3D12GetInterface(REFCLSID rclsid, REFIID riid, void** ppvDebug) {
    if (g_realD3D12GetInterface) return g_realD3D12GetInterface(rclsid, riid, ppvDebug);
    return E_FAIL;
}

HRESULT WINAPI D3D12SerializeRootSignature(const D3D12_ROOT_SIGNATURE_DESC* pRootSignature, D3D_ROOT_SIGNATURE_VERSION Version, ID3DBlob** ppBlob, ID3DBlob** ppErrorBlob) {
    if (g_realD3D12SerializeRootSignature) return g_realD3D12SerializeRootSignature(pRootSignature, Version, ppBlob, ppErrorBlob);
    return E_FAIL;
}

HRESULT WINAPI D3D12SerializeVersionedRootSignature(const D3D12_VERSIONED_ROOT_SIGNATURE_DESC* pRootSignature, ID3DBlob** ppBlob, ID3DBlob** ppErrorBlob) {
    if (g_realD3D12SerializeVersionedRootSignature) return g_realD3D12SerializeVersionedRootSignature(pRootSignature, ppBlob, ppErrorBlob);
    return E_FAIL;
}

HRESULT WINAPI D3D12EnableExperimentalFeatures(UINT NumFeatures, const IID* pIIDs, void* pConfigurationStructs, UINT* pConfigurationStructSizes) {
    if (g_realD3D12EnableExperimentalFeatures) return g_realD3D12EnableExperimentalFeatures(NumFeatures, pIIDs, pConfigurationStructs, pConfigurationStructSizes);
    return E_FAIL;
}

// Generic forwarder for other ordinals
#define FORWARD_ORDINAL(ord) \
void* WINAPI Ordinal##ord(void* a1, void* a2, void* a3, void* a4, void* a5, void* a6) { \
    typedef void* (WINAPI* PFN_Generic)(void*, void*, void*, void*, void*, void*); \
    PFN_Generic fn = (PFN_Generic)g_procTable[ord]; \
    return fn ? fn(a1, a2, a3, a4, a5, a6) : nullptr; \
}

FORWARD_ORDINAL(99)
FORWARD_ORDINAL(100) // SetAppCompatStringPointer
FORWARD_ORDINAL(103) // D3D12CoreCreateLayeredDevice
FORWARD_ORDINAL(104) // D3D12CoreGetLayeredDeviceSize
FORWARD_ORDINAL(105) // D3D12CoreRegisterLayers
FORWARD_ORDINAL(106) // D3D12CreateRootSignatureDeserializer
FORWARD_ORDINAL(107) // D3D12CreateVersionedRootSignatureDeserializer
FORWARD_ORDINAL(108) // D3D12DeviceRemovedExtendedData
FORWARD_ORDINAL(111) // D3D12PIXEventsReplaceBlock
FORWARD_ORDINAL(112) // D3D12PIXGetThreadInfo
FORWARD_ORDINAL(113) // D3D12PIXNotifyWakeFromFenceSignal
FORWARD_ORDINAL(114) // D3D12PIXReportCounter
FORWARD_ORDINAL(117) // GetBehaviorValue

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
        Log("=== Initializing D3D12 Proxy Layer ===");

        if (!LoadRealD3D12()) {
            Log("[FATAL] Cannot load system d3d12.dll");
            return FALSE;
        }

        if (g_captureMode == CaptureMode::RenderDoc || g_captureMode == CaptureMode::Both) {
            LoadTinecmaToolD3D12();
        }

        Log("=== D3D12 Proxy Init Complete ===");
        break;

    case DLL_PROCESS_DETACH:
        if (g_logFile) {
            fclose(g_logFile);
            g_logFile = nullptr;
        }
        DeleteCriticalSection(&g_logLock);
        if (g_realD3D12) FreeLibrary(g_realD3D12);
        break;
    }
    return TRUE;
}
