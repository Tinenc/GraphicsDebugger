// Send a single virtual-key press to a window (or globally).
//
// Exists because Add-Type and WScript.Shell are both blocked by policy on this
// machine, and RenderDoc reads its capture key with GetAsyncKeyState - a real key
// press is the only way to exercise it from a script.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

static HWND g_hit = NULL;
static const wchar_t *g_needle = NULL;

int wmain(int argc, wchar_t **argv)
{
  if(argc < 2)
  {
    printf("usage: sendkey.exe <vk-hex> [window-title-substring]\n");
    printf("       vk: 7B = F12, 2C = PrintScreen\n");
    return 2;
  }

  const wchar_t *arg = argv[1];
  WORD vk = (WORD)wcstoul((arg[0] == L'0' && (arg[1] == L'x' || arg[1] == L'X')) ? arg + 2 : arg,
                          NULL, 16);
  if(vk == 0)
  {
    printf("bad vk\n");
    return 2;
  }

  if(argc >= 3)
  {
    g_needle = argv[2];
    wchar_t buf[512];
    for(HWND cur = GetTopWindow(NULL); cur; cur = GetWindow(cur, GW_HWNDNEXT))
    {
      if(GetWindowTextW(cur, buf, 512) > 0 && wcsstr(buf, g_needle))
      {
        g_hit = cur;
        break;
      }
    }

    if(g_hit)
    {
      SetForegroundWindow(g_hit);
      Sleep(400);
      printf("focused window %p\n", (void *)g_hit);
    }
    else
    {
      printf("no window matched, sending globally\n");
    }
  }

  INPUT in[2] = {};
  in[0].type = INPUT_KEYBOARD;
  in[0].ki.wVk = vk;
  in[1].type = INPUT_KEYBOARD;
  in[1].ki.wVk = vk;
  in[1].ki.dwFlags = KEYEVENTF_KEYUP;

  UINT sent = SendInput(2, in, sizeof(INPUT));
  printf("vk=0x%02X sent=%u\n", vk, sent);
  return sent == 2 ? 0 : 1;
}
