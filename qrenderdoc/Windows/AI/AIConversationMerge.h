#pragma once

#include <QCryptographicHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QHash>
#include <QVector>
#include <algorithm>

namespace AIConversationMerge
{
// i18n-ignore: Persisted JSON field/schema identifiers; no displayed text.
inline qint64 timestamp(const QJsonObject &row, const char *field)
{
  const QJsonValue value = row.value(QString::fromLatin1(field));
  return value.isString() ? value.toString().toLongLong() : qint64(value.toDouble());
}

inline QString identity(const QJsonObject &row)
{
  QString id = row.value(QStringLiteral("id")).toString();
  return id.isEmpty() ? QString::fromLatin1(QCryptographicHash::hash(
      QJsonDocument(row).toJson(QJsonDocument::Compact), QCryptographicHash::Sha256).toHex()) : id;
}

inline QJsonArray messages(const QJsonArray &older, const QJsonArray &newer)
{
  QVector<QJsonObject> rows;
  QHash<QString, int> positions;
  for(const QJsonArray &input : {older, newer})
    for(const QJsonValue &value : input)
    {
      if(!value.isObject())
        continue;
      const QJsonObject row = value.toObject();
      const QString id = identity(row);
      if(positions.contains(id))
        rows[positions.value(id)] = row;
      else
      {
        positions.insert(id, rows.size());
        rows.push_back(row);
      }
    }
  std::stable_sort(rows.begin(), rows.end(), [](const QJsonObject &a, const QJsonObject &b) {
    return timestamp(a, "timestampUnixMs") < timestamp(b, "timestampUnixMs");
  });
  QJsonArray result;
  for(const QJsonObject &row : rows)
    result.append(row);
  return result;
}

inline bool known(const QJsonObject &root)
{
  const QString schema = root.value(QStringLiteral("schema")).toString();
  return schema == QStringLiteral("replaylab-ai-conversations/v2") ||
         schema == QStringLiteral("replaylab-ai-conversations/v3") ||
         schema == QStringLiteral("replaylab-ai-conversations/v4") ||
         schema == QStringLiteral("replaylab-ai-conversations/v5");
}

inline QJsonObject merge(const QJsonObject &primary, const QJsonObject &legacy)
{
  QJsonObject result = primary;
  result[QStringLiteral("schema")] = QStringLiteral("replaylab-ai-conversations/v5");
  QVector<QJsonObject> rows;
  QHash<QString, int> positions;
  for(const QJsonArray &input : {legacy.value(QStringLiteral("conversations")).toArray(),
                                primary.value(QStringLiteral("conversations")).toArray()})
    for(const QJsonValue &value : input)
    {
      if(!value.isObject())
        continue;
      QJsonObject row = value.toObject();
      const QString id = identity(row);
      if(positions.contains(id))
      {
        const int index = positions.value(id);
        const QJsonObject before = rows[index];
        const bool latest = timestamp(row, "updatedUnixMs") >= timestamp(before, "updatedUnixMs");
        QJsonObject chosen = latest ? row : before;
        chosen[QStringLiteral("messages")] = messages(
            (latest ? before : row).value(QStringLiteral("messages")).toArray(),
            (latest ? row : before).value(QStringLiteral("messages")).toArray());
        rows[index] = chosen;
      }
      else
      {
        positions.insert(id, rows.size());
        rows.push_back(row);
      }
    }
  QJsonArray conversations;
  QString active = primary.value(QStringLiteral("activeConversationId")).toString();
  bool activeHasMessages = false;
  for(const QJsonObject &row : rows)
  {
    conversations.append(row);
    if(identity(row) == active)
      activeHasMessages = !row.value(QStringLiteral("messages")).toArray().isEmpty();
  }
  if(!activeHasMessages)
  {
    const QString previous = legacy.value(QStringLiteral("activeConversationId")).toString();
    if(positions.contains(previous))
      active = previous;
  }
  result[QStringLiteral("conversations")] = conversations;
  result[QStringLiteral("activeConversationId")] = active;
  // Recover accepted queued prompts too; application hydration marks them interrupted.
  result[QStringLiteral("queuedMessages")] = messages(
      legacy.value(QStringLiteral("queuedMessages")).toArray(),
      primary.value(QStringLiteral("queuedMessages")).toArray());
  return result;
}

// Merge edits from one application's last loaded/saved view into the newest
// disk document. Untouched stale rows cannot overwrite or resurrect a row
// removed by another instance. Explicit local deletions affect only rows this
// application actually knew; unrelated new conversations remain intact.
inline QJsonObject concurrent(const QJsonObject &local, const QJsonObject &baseline,
                              const QJsonObject &disk)
{
  QJsonObject result = local;
  for(const char *field : {"conversations", "queuedMessages"})
  {
    const QString key = QString::fromLatin1(field);
    QHash<QString, QJsonObject> before, edited;
    QVector<QJsonObject> rows;
    for(const QJsonValue &v : baseline.value(key).toArray())
      if(v.isObject()) before.insert(identity(v.toObject()), v.toObject());
    for(const QJsonValue &v : local.value(key).toArray())
      if(v.isObject()) edited.insert(identity(v.toObject()), v.toObject());
    for(const QJsonValue &v : disk.value(key).toArray())
      if(v.isObject() && !(before.contains(identity(v.toObject())) &&
                          !edited.contains(identity(v.toObject()))))
        rows.append(v.toObject());
    for(const QJsonValue &v : local.value(key).toArray())
    {
      if(!v.isObject()) continue;
      QJsonObject row = v.toObject();
      const QString id = identity(row);
      if(before.contains(id) && before.value(id) == row) continue;
      int found = -1;
      for(int i=0; i<rows.size(); ++i) if(identity(rows[i]) == id) { found=i; break; }
      if(found < 0) rows.append(row);
      else
      {
        if(key == QStringLiteral("conversations"))
          row[QStringLiteral("messages")] = messages(
              rows[found].value(QStringLiteral("messages")).toArray(),
              row.value(QStringLiteral("messages")).toArray());
        rows[found] = row;
      }
    }
    QJsonArray merged;
    for(const QJsonObject &row : rows) merged.append(row);
    result[key] = merged;
  }
  return result;
}
};
