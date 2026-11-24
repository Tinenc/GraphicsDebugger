#pragma once

#include <QProcess>

// Background compiler and language-server children must never create a console.
// This leaves redirected streams intact and does not hide user-launched apps.
inline void ConfigureBackgroundProcess(QProcess *process)
{
#if defined(Q_OS_WIN)
  process->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *args) {
    args->flags |= 0x08000000UL; // CREATE_NO_WINDOW
  });
#else
  Q_UNUSED(process);
#endif
}
