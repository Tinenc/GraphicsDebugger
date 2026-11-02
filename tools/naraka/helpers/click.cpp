// Click a point inside a window, given as fractions of the window rect.
//
// Written for the same reason as sendkey.exe: Add-Type and WScript.Shell are blocked
// by policy here, so driving a launcher button from a script needs a real exe.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

static HWND g_hit = NULL;
static const wchar_t *g_needle = NULL;

static BOOL CALLBACK FindProc(HWND h, LPARAM)
{
  wchar_t buf[512];
  if(GetWindowTextW(h, buf, 512) > 0 && wcsstr(buf, g_needle))
  {
    g_hit = h;
    return FALSE;
  }
  return TRUE;
}

int wmain(int argc, wchar_t **argv)
{
  if(argc < 4)
  {
    printf("usage: click.exe <window-title-substring> <x-frac 0..1> <y-frac 0..1>\n");
    return 2;
  }

  g_needle = argv[1];
  double fx = _wtof(argv[2]);
  double fy = _wtof(argv[3]);

  EnumWindows(FindProc, 0);
  if(!g_hit)
  {
    printf("no window matched\n");
    return 1;
  }

  if(IsIconic(g_hit))
    ShowWindow(g_hit, SW_RESTORE);
  SetForegroundWindow(g_hit);
  Sleep(500);

  RECT r;
  GetWindowRect(g_hit, &r);
  int w = r.right - r.left, h = r.bottom - r.top;
  int x = r.left + (int)(w * fx);
  int y = r.top + (int)(h * fy);

  printf("window %dx%d at %d,%d -> click %d,%d\n", w, h, (int)r.left, (int)r.top, x, y);

  SetCursorPos(x, y);
  Sleep(150);

  INPUT in[2] = {};
  in[0].type = INPUT_MOUSE;
  in[0].mi.dwFlags = MOUSEEVENTF_LEFTDOWN;
  in[1].type = INPUT_MOUSE;
  in[1].mi.dwFlags = MOUSEEVENTF_LEFTUP;
  UINT sent = SendInput(2, in, sizeof(INPUT));
  printf("click sent=%u\n", sent);
  return sent == 2 ? 0 : 1;
}
