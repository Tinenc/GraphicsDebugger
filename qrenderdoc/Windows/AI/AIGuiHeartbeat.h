#pragma once

#include <QtGlobal>

// GUI-thread-only interval sampling. Normal ticks do no I/O; a caller emits a diagnostic only
// after an active AI interval stalled, including a task which finished before the next tick.
class AIGuiHeartbeat
{
public:
  qint64 sample(qint64 monotonicMs, bool active)
  {
    const qint64 gap = qMax<qint64>(0, monotonicMs - m_LastTickMs);
    const bool relevant = active || m_PreviouslyActive;
    m_LastTickMs = monotonicMs;
    m_PreviouslyActive = active;
    if(!relevant || gap < 1500 || monotonicMs - m_LastReportMs < 10000)
      return 0;
    m_LastReportMs = monotonicMs;
    return gap;
  }

private:
  qint64 m_LastTickMs = 0;
  qint64 m_LastReportMs = -10000;
  bool m_PreviouslyActive = false;
};
