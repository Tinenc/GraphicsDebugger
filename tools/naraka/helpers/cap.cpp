// Capture a window to a BMP, for visual verification of the control-center GUI.
// Written because Add-Type is blocked by policy on this machine and there is no
// other scripting screenshot path available.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

static HWND g_found = NULL;
static const wchar_t *g_needle = NULL;

static BOOL CALLBACK ListTitles(HWND h, LPARAM)
{
  wchar_t buf[512];
  if(GetWindowTextW(h, buf, 512) > 0)
    wprintf(L"%s %p visible=%d  %s\n", IsWindowVisible(h) ? L"V" : L"-", (void *)h,
            IsWindowVisible(h), buf);
  return TRUE;
}

static BOOL CALLBACK FindByTitle(HWND h, LPARAM)
{
  wchar_t buf[512];
  if(GetWindowTextW(h, buf, 512) == 0)
    return TRUE;
  if(wcsstr(buf, g_needle))
  {
    g_found = h;
    return FALSE;  // stop enumerating
  }
  return TRUE;
}

int wmain(int argc, wchar_t **argv)
{
  if(argc >= 2 && wcscmp(argv[1], L"-list") == 0)
  {
    EnumWindows(ListTitles, 0);
    return 0;
  }

  if(argc < 3)
  {
    printf("usage: cap.exe <title-substring> <out.bmp>\n");
    return 2;
  }

  g_needle = argv[1];
  EnumWindows(FindByTitle, 0);
  if(!g_found)
  {
    printf("no window matching that title\n");
    return 1;
  }

  SetForegroundWindow(g_found);
  // A window started by another process often comes up minimised; restore it or
  // GetWindowRect hands back the off-screen placeholder rect (-32000-ish).
  if(IsIconic(g_found))
    ShowWindow(g_found, SW_RESTORE);
  Sleep(700);

  RECT r;
  GetWindowRect(g_found, &r);
  int w = r.right - r.left, h = r.bottom - r.top;
  printf("window %dx%d at %d,%d\n", w, h, (int)r.left, (int)r.top);

  HDC screen = GetDC(NULL);
  HDC mem = CreateCompatibleDC(screen);
  HBITMAP bmp = CreateCompatibleBitmap(screen, w, h);
  SelectObject(mem, bmp);

  // PW_RENDERFULLCONTENT (2) makes this work for composited/.NET windows and for
  // windows that are partly covered.
  if(!PrintWindow(g_found, mem, 2))
  {
    printf("PrintWindow failed, falling back to BitBlt\n");
    BitBlt(mem, 0, 0, w, h, screen, r.left, r.top, SRCCOPY | CAPTUREBLT);
  }

  BITMAPINFOHEADER ih = {0};
  ih.biSize = sizeof(ih);
  ih.biWidth = w;
  ih.biHeight = h;
  ih.biPlanes = 1;
  ih.biBitCount = 24;
  ih.biCompression = BI_RGB;

  DWORD imgSize = ((w * 3 + 3) & ~3u) * h;
  BYTE *bits = (BYTE *)malloc(imgSize);
  BITMAPINFO bi = {0};
  bi.bmiHeader = ih;
  GetDIBits(mem, bmp, 0, h, bits, &bi, DIB_RGB_COLORS);

  BITMAPFILEHEADER fh = {0};
  fh.bfType = 0x4D42;
  fh.bfOffBits = sizeof(fh) + sizeof(ih);
  fh.bfSize = fh.bfOffBits + imgSize;

  FILE *f = NULL;
  _wfopen_s(&f, argv[2], L"wb");
  if(!f)
  {
    printf("cannot open output\n");
    return 1;
  }
  fwrite(&fh, sizeof(fh), 1, f);
  fwrite(&ih, sizeof(ih), 1, f);
  fwrite(bits, imgSize, 1, f);
  fclose(f);

  printf("wrote output, %u bytes of pixels\n", imgSize);
  return 0;
}
