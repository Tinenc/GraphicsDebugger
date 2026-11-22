#pragma once

#include <QByteArray>
#include <QJsonArray>
#include <QStringList>

// Local configuration only. Network probes and Agent execution stay in AgentHost.
namespace AIConfiguration
{
struct Reply
{
  int exitCode = -1;
  QByteArray output;
  QString diagnostic;
};

QJsonArray providerPresets();
bool handles(const QStringList &arguments);
Reply execute(const QStringList &arguments, const QByteArray &input);
}
