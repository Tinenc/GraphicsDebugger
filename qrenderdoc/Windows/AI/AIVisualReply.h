#pragma once

#include <QCoreApplication>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QUrl>
#include <QWidget>
#include <functional>
#include <memory>

struct AIVisualReplyLayoutState;

// A native, offline visual answer. Only a hash-addressed Host manifest and its
// verified PNG/geometry blobs are loaded; model HTML/URLs never enter a renderer.
class AIVisualReply : public QWidget
{
  Q_DECLARE_TR_FUNCTIONS(AIVisualReply)
public:
  using Navigate = std::function<void(const QUrl &, const QJsonObject &)>;
  AIVisualReply(const QString &identity, Navigate navigate, QWidget *parent = NULL);
  static bool validIdentity(const QString &identity);
  static QString storageRoot();
  static QImage verifiedImage(const QJsonObject &asset);
  static QStringList visibleIdentities(const QJsonArray &references);
  static QJsonObject navigationTarget(const QString &identity);
  static QUrl resolveLegacyTextureLink(const QUrl &url, const QString &identity,
                                       QJsonObject &capture);
  static int inlineHeight(const QString &identity);

private:
  AIVisualReply(const QString &identity, Navigate navigate, QWidget *parent,
                std::shared_ptr<AIVisualReplyLayoutState> layouts);
  void setComparisonMode(int mode);
  int m_ComparisonMode = 2;
};
