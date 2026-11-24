#include <QCoreApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <iostream>
#include "Windows/AI/AIConfiguration.h"

int main(int argc, char **argv)
{
  QCoreApplication application(argc, argv);
  std::string line;
  while(std::getline(std::cin, line))
  {
    const QJsonObject input = QJsonDocument::fromJson(QByteArray::fromStdString(line)).object();
    QJsonObject output;
    if(input.contains(QStringLiteral("catalog")))
      output[QStringLiteral("catalog")] = AIConfiguration::providerPresets();
    else
    {
      QStringList arguments;
      for(const QJsonValue &arg : input.value(QStringLiteral("args")).toArray())
        arguments << arg.toString();
      const AIConfiguration::Reply reply = AIConfiguration::execute(
          arguments, input.value(QStringLiteral("input")).toString().toUtf8());
      output[QStringLiteral("handled")] = AIConfiguration::handles(arguments);
      output[QStringLiteral("exitCode")] = reply.exitCode;
      output[QStringLiteral("output")] = QString::fromUtf8(reply.output);
      output[QStringLiteral("diagnostic")] = reply.diagnostic;
    }
    std::cout << QJsonDocument(output).toJson(QJsonDocument::Compact).constData() << std::endl;
  }
}
