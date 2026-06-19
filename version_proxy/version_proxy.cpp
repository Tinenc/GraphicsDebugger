#define WIN32_LEAN_AND_MEAN
#include <windows.h>

static HMODULE g_realDll = NULL;

typedef BOOL  (WINAPI *pGetFileVersionInfoA)(LPCSTR, DWORD, DWORD, LPVOID);
typedef DWORD (WINAPI *pGetFileVersionInfoByHandle)(DWORD, HANDLE, DWORD, LPVOID);
typedef BOOL  (WINAPI *pGetFileVersionInfoExA)(DWORD, LPCSTR, DWORD, DWORD, LPVOID);
typedef BOOL  (WINAPI *pGetFileVersionInfoExW)(DWORD, LPCWSTR, DWORD, DWORD, LPVOID);
typedef DWORD (WINAPI *pGetFileVersionInfoSizeA)(LPCSTR, LPDWORD);
typedef DWORD (WINAPI *pGetFileVersionInfoSizeExA)(DWORD, LPCSTR, LPDWORD);
typedef DWORD (WINAPI *pGetFileVersionInfoSizeExW)(DWORD, LPCWSTR, LPDWORD);
typedef DWORD (WINAPI *pGetFileVersionInfoSizeW)(LPCWSTR, LPDWORD);
typedef BOOL  (WINAPI *pGetFileVersionInfoW)(LPCWSTR, DWORD, DWORD, LPVOID);
typedef DWORD (WINAPI *pVerFindFileA)(DWORD, LPSTR, LPSTR, LPSTR, LPSTR, PUINT, LPSTR, PUINT);
typedef DWORD (WINAPI *pVerFindFileW)(DWORD, LPWSTR, LPWSTR, LPWSTR, LPWSTR, PUINT, LPWSTR, PUINT);
typedef DWORD (WINAPI *pVerInstallFileA)(DWORD, LPSTR, LPSTR, LPSTR, LPSTR, LPSTR, LPSTR, PUINT);
typedef DWORD (WINAPI *pVerInstallFileW)(DWORD, LPWSTR, LPWSTR, LPWSTR, LPWSTR, LPWSTR, LPWSTR, PUINT);
typedef DWORD (WINAPI *pVerLanguageNameA)(DWORD, LPSTR, DWORD);
typedef DWORD (WINAPI *pVerLanguageNameW)(DWORD, LPWSTR, DWORD);
typedef BOOL  (WINAPI *pVerQueryValueA)(LPCVOID, LPCSTR, LPVOID *, PUINT);
typedef BOOL  (WINAPI *pVerQueryValueW)(LPCVOID, LPCWSTR, LPVOID *, PUINT);

static pGetFileVersionInfoA        fpGetFileVersionInfoA;
static pGetFileVersionInfoByHandle fpGetFileVersionInfoByHandle;
static pGetFileVersionInfoExA      fpGetFileVersionInfoExA;
static pGetFileVersionInfoExW      fpGetFileVersionInfoExW;
static pGetFileVersionInfoSizeA    fpGetFileVersionInfoSizeA;
static pGetFileVersionInfoSizeExA  fpGetFileVersionInfoSizeExA;
static pGetFileVersionInfoSizeExW  fpGetFileVersionInfoSizeExW;
static pGetFileVersionInfoSizeW    fpGetFileVersionInfoSizeW;
static pGetFileVersionInfoW        fpGetFileVersionInfoW;
static pVerFindFileA               fpVerFindFileA;
static pVerFindFileW               fpVerFindFileW;
static pVerInstallFileA            fpVerInstallFileA;
static pVerInstallFileW            fpVerInstallFileW;
static pVerLanguageNameA           fpVerLanguageNameA;
static pVerLanguageNameW           fpVerLanguageNameW;
static pVerQueryValueA             fpVerQueryValueA;
static pVerQueryValueW             fpVerQueryValueW;

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID)
{
    if(reason == DLL_PROCESS_ATTACH)
    {
        wchar_t buf[MAX_PATH];
        GetSystemDirectoryW(buf, MAX_PATH);
        wcscat_s(buf, MAX_PATH, L"\\version.dll");
        g_realDll = LoadLibraryW(buf);
        if(!g_realDll)
            return FALSE;

#define LOAD(name) fp##name = (p##name)GetProcAddress(g_realDll, #name)
        LOAD(GetFileVersionInfoA);
        LOAD(GetFileVersionInfoByHandle);
        LOAD(GetFileVersionInfoExA);
        LOAD(GetFileVersionInfoExW);
        LOAD(GetFileVersionInfoSizeA);
        LOAD(GetFileVersionInfoSizeExA);
        LOAD(GetFileVersionInfoSizeExW);
        LOAD(GetFileVersionInfoSizeW);
        LOAD(GetFileVersionInfoW);
        LOAD(VerFindFileA);
        LOAD(VerFindFileW);
        LOAD(VerInstallFileA);
        LOAD(VerInstallFileW);
        LOAD(VerLanguageNameA);
        LOAD(VerLanguageNameW);
        LOAD(VerQueryValueA);
        LOAD(VerQueryValueW);
#undef LOAD

        // Load TinecmaTools from its installation directory
        LoadLibraryW(L"C:\\Program Files\\GraphicsDebugger\\x64\\Development\\TinecmaTools.dll");
    }
    return TRUE;
}

// All stub functions are prefixed with vp_ to avoid conflict with dllimport
// declarations in winver.h. The .def file re-exports them under the real names.

extern "C" {

BOOL WINAPI vp_GetFileVersionInfoA(LPCSTR a, DWORD b, DWORD c, LPVOID d)
{
    return fpGetFileVersionInfoA(a, b, c, d);
}

DWORD WINAPI vp_GetFileVersionInfoByHandle(DWORD a, HANDLE b, DWORD c, LPVOID d)
{
    return fpGetFileVersionInfoByHandle(a, b, c, d);
}

BOOL WINAPI vp_GetFileVersionInfoExA(DWORD a, LPCSTR b, DWORD c, DWORD d, LPVOID e)
{
    return fpGetFileVersionInfoExA(a, b, c, d, e);
}

BOOL WINAPI vp_GetFileVersionInfoExW(DWORD a, LPCWSTR b, DWORD c, DWORD d, LPVOID e)
{
    return fpGetFileVersionInfoExW(a, b, c, d, e);
}

DWORD WINAPI vp_GetFileVersionInfoSizeA(LPCSTR a, LPDWORD b)
{
    return fpGetFileVersionInfoSizeA(a, b);
}

DWORD WINAPI vp_GetFileVersionInfoSizeExA(DWORD a, LPCSTR b, LPDWORD c)
{
    return fpGetFileVersionInfoSizeExA(a, b, c);
}

DWORD WINAPI vp_GetFileVersionInfoSizeExW(DWORD a, LPCWSTR b, LPDWORD c)
{
    return fpGetFileVersionInfoSizeExW(a, b, c);
}

DWORD WINAPI vp_GetFileVersionInfoSizeW(LPCWSTR a, LPDWORD b)
{
    return fpGetFileVersionInfoSizeW(a, b);
}

BOOL WINAPI vp_GetFileVersionInfoW(LPCWSTR a, DWORD b, DWORD c, LPVOID d)
{
    return fpGetFileVersionInfoW(a, b, c, d);
}

DWORD WINAPI vp_VerFindFileA(DWORD a, LPSTR b, LPSTR c, LPSTR d, LPSTR e, PUINT f, LPSTR g, PUINT h)
{
    return fpVerFindFileA(a, b, c, d, e, f, g, h);
}

DWORD WINAPI vp_VerFindFileW(DWORD a, LPWSTR b, LPWSTR c, LPWSTR d, LPWSTR e, PUINT f, LPWSTR g, PUINT h)
{
    return fpVerFindFileW(a, b, c, d, e, f, g, h);
}

DWORD WINAPI vp_VerInstallFileA(DWORD a, LPSTR b, LPSTR c, LPSTR d, LPSTR e, LPSTR f, LPSTR g, PUINT h)
{
    return fpVerInstallFileA(a, b, c, d, e, f, g, h);
}

DWORD WINAPI vp_VerInstallFileW(DWORD a, LPWSTR b, LPWSTR c, LPWSTR d, LPWSTR e, LPWSTR f, LPWSTR g, PUINT h)
{
    return fpVerInstallFileW(a, b, c, d, e, f, g, h);
}

DWORD WINAPI vp_VerLanguageNameA(DWORD a, LPSTR b, DWORD c)
{
    return fpVerLanguageNameA(a, b, c);
}

DWORD WINAPI vp_VerLanguageNameW(DWORD a, LPWSTR b, DWORD c)
{
    return fpVerLanguageNameW(a, b, c);
}

BOOL WINAPI vp_VerQueryValueA(LPCVOID a, LPCSTR b, LPVOID *c, PUINT d)
{
    return fpVerQueryValueA(a, b, c, d);
}

BOOL WINAPI vp_VerQueryValueW(LPCVOID a, LPCWSTR b, LPVOID *c, PUINT d)
{
    return fpVerQueryValueW(a, b, c, d);
}

}  // extern "C"
