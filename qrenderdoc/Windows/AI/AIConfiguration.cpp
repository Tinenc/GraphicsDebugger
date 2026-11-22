#include "AIConfiguration.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHostAddress>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QSaveFile>
#include <QUrl>
#include <QUuid>
#include <algorithm>
#include <cmath>
#include <cstring>

#ifdef Q_OS_WIN
#include <windows.h>

#include <wincred.h>
#pragma comment(lib, "advapi32.lib")
#else
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#endif

#include "AIProviderCatalog.inc"

namespace
{
// i18n-ignore: The following literals are CLI/JSON identifiers and validation field names.
const QJsonObject &catalog()
{
  static const QJsonObject value = QJsonDocument::fromJson(AIProviderCatalogJSON).object();
  return value;
}

QString configurationPath()
{
  const QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
  QString root = environment.value(QStringLiteral("APPDATA"));
  if(root.isEmpty())
    root = environment.value(QStringLiteral("LOCALAPPDATA"));
  if(root.isEmpty())
    root = QDir::home().filePath(QStringLiteral(".config"));
  return QDir(root).filePath(QStringLiteral("ReplayLab/ai/profiles.json"));
}

QString invalidSettings(const QString &field)
{
  return QCoreApplication::translate("AIConfiguration", "Invalid AI connection settings: %1.").arg(field);
}

bool validId(const QString &id)
{
  static const QRegularExpression expression(QStringLiteral("^[a-z0-9][a-z0-9._-]{0,63}$"));
  return expression.match(id).hasMatch();
}

QJsonObject presetFor(const QString &id)
{
  for(const QJsonValue &value : AIConfiguration::providerPresets())
    if(value.toObject().value(QStringLiteral("id")).toString() == id)
      return value.toObject();
  return QJsonObject();
}

QJsonObject emptyProfile(const QString &id)
{
  QJsonObject row;
  for(const char *key : {"preset", "model", "default_model", "title", "base_url", "api_key_env"})
    row[QString::fromLatin1(key)] = QString();
  row[QStringLiteral("id")] = id;
  for(const char *key : {"models", "command", "agent_env"})
    row[QString::fromLatin1(key)] = QJsonArray();
  row[QStringLiteral("options")] = QJsonObject();
  row[QStringLiteral("model_context_windows")] = QJsonObject();
  row[QStringLiteral("probed_capabilities")] = QJsonValue(QJsonValue::Null);
  return row;
}

bool cleanLine(const QString &value, int limit)
{
  return value.size() <= limit && !value.contains(QChar(0)) && !value.contains(QLatin1Char('\r')) &&
         !value.contains(QLatin1Char('\n'));
}

bool loopback(const QString &host)
{
  QString name = host.toLower();
  while(name.endsWith(QLatin1Char('.')))
    name.chop(1);
  return name == QStringLiteral("localhost") || name.endsWith(QStringLiteral(".localhost")) ||
         QHostAddress(name).isLoopback();
}

QString endpointFor(const QJsonObject &row, const QJsonObject &preset)
{
  QString endpoint = row.value(QStringLiteral("base_url")).toString();
  if(endpoint.isEmpty())
    endpoint = preset.value(QStringLiteral("baseUrl")).toString();
  while(endpoint.endsWith(QLatin1Char('/')))
    endpoint.chop(1);
  const QString protocol = preset.value(QStringLiteral("protocol")).toString();
  if(protocol == QStringLiteral("openai-chat") || protocol == QStringLiteral("openai-responses"))
    for(const char *suffix : {"/chat/completions", "/responses", "/models"})
      if(endpoint.endsWith(QString::fromLatin1(suffix), Qt::CaseInsensitive))
      {
        endpoint.chop(int(strlen(suffix)));
        while(endpoint.endsWith(QLatin1Char('/')))
          endpoint.chop(1);
        break;
      }
  return endpoint;
}

QString validate(QJsonObject &row)
{
  const QString id = row.value(QStringLiteral("id")).toString();
  const QJsonObject preset = presetFor(row.value(QStringLiteral("preset")).toString());
  if(!validId(id) || preset.isEmpty())
    return invalidSettings(QStringLiteral("id/preset"));
  for(const char *field : {"model", "default_model", "title", "base_url", "api_key_env"})
    if(row.contains(QString::fromLatin1(field)) && !row.value(QString::fromLatin1(field)).isString())
      return invalidSettings(QString::fromLatin1(field));
  if(!cleanLine(row.value(QStringLiteral("title")).toString(), 128))
    return invalidSettings(QStringLiteral("title"));
  QJsonArray models;
  const QJsonValue modelsValue = row.value(QStringLiteral("models"));
  if(!modelsValue.isUndefined() && !modelsValue.isArray())
    return invalidSettings(QStringLiteral("models"));
  if(modelsValue.toArray().size() > 200)
    return invalidSettings(QStringLiteral("models"));
  for(const QJsonValue &model : modelsValue.toArray())
  {
    const QString name = model.toString().trimmed();
    if(!model.isString() || name.isEmpty() || !cleanLine(name, 512))
      return invalidSettings(QStringLiteral("models"));
    if(!models.contains(name))
      models.append(name);
  }
  QString model = row.value(QStringLiteral("model")).toString().trimmed();
  QString defaultModel = row.value(QStringLiteral("default_model")).toString().trimmed();
  if(models.isEmpty() && !model.isEmpty())
    models.append(model);
  if(defaultModel.isEmpty())
    defaultModel = model.isEmpty() ? (models.isEmpty() ? QString() : models[0].toString()) : model;
  if(model.isEmpty())
    model = defaultModel;
  if(!cleanLine(model, 512) || !cleanLine(defaultModel, 512) ||
     (!defaultModel.isEmpty() && !models.contains(defaultModel)) ||
     (!model.isEmpty() && !models.isEmpty() && !models.contains(model)))
    return invalidSettings(QStringLiteral("default_model/models"));
  row[QStringLiteral("models")] = models;
  row[QStringLiteral("model")] = model;
  row[QStringLiteral("default_model")] = defaultModel;
  const bool api = preset.value(QStringLiteral("kind")).toString() == QStringLiteral("model");
  if(api && model.isEmpty())
    return invalidSettings(QStringLiteral("model"));
  QString keyEnv = row.value(QStringLiteral("api_key_env")).toString();
  if(keyEnv.isEmpty())
    keyEnv = preset.value(QStringLiteral("apiKeyEnv")).toString();
  static const QRegularExpression environment(QStringLiteral("^[A-Za-z_][A-Za-z0-9_]{0,127}$"));
  if((!keyEnv.isEmpty() &&
      (!environment.match(keyEnv).hasMatch() ||
       catalog().value(QStringLiteral("reservedCredentialEnv")).toArray().contains(keyEnv.toUpper()))) ||
     (api && preset.value(QStringLiteral("auth")).toString() == QStringLiteral("api-key") &&
      keyEnv.isEmpty()))
    return invalidSettings(QStringLiteral("api_key_env"));
  const QString endpoint = endpointFor(row, preset);
  if(!cleanLine(endpoint, 4096))
    return invalidSettings(QStringLiteral("base_url"));
  if(!endpoint.isEmpty())
  {
    const QUrl url(endpoint, QUrl::StrictMode);
    if(!url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() || url.hasQuery() ||
       url.hasFragment() ||
       (url.scheme() != QStringLiteral("https") &&
        (url.scheme() != QStringLiteral("http") || !loopback(url.host()))))
      return invalidSettings(QStringLiteral("base_url"));
  }
  if(!row.value(QStringLiteral("options")).isObject())
    return invalidSettings(QStringLiteral("options"));
  if(preset.value(QStringLiteral("protocol")).toString() == QStringLiteral("azure-openai"))
  {
    const QUrl url(endpoint);
    const bool v1 = url.host().endsWith(QStringLiteral(".models.ai.azure.com"), Qt::CaseInsensitive) ||
                    url.path().endsWith(QStringLiteral("/openai/v1"), Qt::CaseInsensitive);
    const QString version = row.value(QStringLiteral("options"))
                                .toObject()
                                .value(QStringLiteral("api_version"))
                                .toString()
                                .trimmed();
    if(endpoint.isEmpty() || v1 == !version.isEmpty())
      return invalidSettings(QStringLiteral("api_version"));
  }
  if(!row.value(QStringLiteral("command")).isArray())
    return invalidSettings(QStringLiteral("command"));
  QJsonArray command = row.value(QStringLiteral("command")).toArray();
  if(command.isEmpty())
    command = preset.value(QStringLiteral("command")).toArray();
  const QJsonArray trusted = preset.value(QStringLiteral("command")).toArray();
  if(!api && command.isEmpty())
    return invalidSettings(QStringLiteral("command"));
  static const QRegularExpression secret(
      QStringLiteral("(?:^|[-_.])(?:api[-_]?key|auth|authentication|authorization|bearer|cookie|"
                     "cookies|credential|credentials|header|headers|key|password|passwd|pat|secret|"
                     "token)(?:[-_.](?:value|raw|literal))?$"),
      QRegularExpression::CaseInsensitiveOption);
  for(int i = 0; i < command.size(); ++i)
  {
    const QString arg = command[i].toString();
    QString flag = arg.section(QRegularExpression(QStringLiteral("[=:]")), 0, 0);
    flag.replace(QRegularExpression(QStringLiteral("([a-z0-9])([A-Z])")),
                 QStringLiteral("\\1-\\2"));
    flag.replace(QRegularExpression(QStringLiteral("([A-Z])([A-Z][a-z])")),
                 QStringLiteral("\\1-\\2"));
    if(!command[i].isString() || arg.contains(QChar(0)) ||
       (arg.isEmpty() && (i == 0 || command != trusted)))
      return invalidSettings(QStringLiteral("command"));
    if(arg.startsWith(QLatin1Char('-')) || arg.contains(QLatin1Char('=')) ||
       (arg.startsWith(QLatin1Char('/')) && arg.contains(QLatin1Char(':'))))
      if(arg == QStringLiteral("-H") || secret.match(flag).hasMatch())
        return invalidSettings(QStringLiteral("command/credential"));
    if(arg.startsWith(QStringLiteral("Bearer "), Qt::CaseInsensitive) ||
       arg.startsWith(QStringLiteral("Authorization:"), Qt::CaseInsensitive) ||
       arg.startsWith(QStringLiteral("Cookie:"), Qt::CaseInsensitive))
      return invalidSettings(QStringLiteral("command/credential"));
  }
  return QString();
}

QJsonObject wireProfile(QJsonObject row, const QString &active)
{
  const QJsonObject preset = presetFor(row.value(QStringLiteral("preset")).toString());
  QJsonObject wire = preset;
  for(const char *field : {"id", "title", "model", "models", "options"})
    wire[QString::fromLatin1(field)] = row.value(QString::fromLatin1(field));
  if(wire.value(QStringLiteral("title")).toString().isEmpty())
    wire[QStringLiteral("title")] = row.value(QStringLiteral("id"));
  wire[QStringLiteral("preset")] = row.value(QStringLiteral("preset"));
  wire[QStringLiteral("active")] = row.value(QStringLiteral("id")).toString() == active;
  wire[QStringLiteral("defaultModel")] = row.value(QStringLiteral("default_model"));
  wire[QStringLiteral("modelContextWindows")] = row.value(QStringLiteral("model_context_windows"));
  wire[QStringLiteral("baseUrl")] = row.value(QStringLiteral("base_url"));
  wire[QStringLiteral("endpoint")] = endpointFor(row, preset);
  if(!row.value(QStringLiteral("api_key_env")).toString().isEmpty())
    wire[QStringLiteral("apiKeyEnv")] = row.value(QStringLiteral("api_key_env"));
  if(!row.value(QStringLiteral("command")).toArray().isEmpty())
    wire[QStringLiteral("command")] = row.value(QStringLiteral("command"));
  wire[QStringLiteral("agentEnv")] = row.value(QStringLiteral("agent_env"));
  wire[QStringLiteral("capabilities")] = row.value(QStringLiteral("probed_capabilities")).isNull()
                                             ? preset.value(QStringLiteral("declaredCapabilities"))
                                             : row.value(QStringLiteral("probed_capabilities"));
  // These are the schemas understood by this co-built runtime, not a runtime health probe.
  wire[QStringLiteral("runInputSchemas")] = catalog().value(QStringLiteral("runInputSchemas"));
  return wire;
}

struct Transaction
{
#ifdef Q_OS_WIN
  HANDLE handle = INVALID_HANDLE_VALUE;
  OVERLAPPED overlap = {};
  bool lock(const QString &path)
  {
    handle = CreateFileW((LPCWSTR)path.utf16(), GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_ALWAYS,
                         FILE_ATTRIBUTE_NORMAL, NULL);
    return handle != INVALID_HANDLE_VALUE &&
           LockFileEx(handle, LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &overlap);
  }
  ~Transaction()
  {
    if(handle != INVALID_HANDLE_VALUE)
      CloseHandle(handle);
  }
#else
  int handle = -1;
  bool lock(const QString &path)
  {
    handle = open(QFile::encodeName(path).constData(), O_CREAT | O_RDWR, 0600);
    return handle >= 0 && flock(handle, LOCK_EX | LOCK_NB) == 0;
  }
  ~Transaction()
  {
    if(handle >= 0)
      close(handle);
  }
#endif
};

bool load(QJsonObject &document, QString &error)
{
  QFile file(configurationPath());
  document = {{QStringLiteral("schema"), QStringLiteral("replaylab-ai-profiles/v2")},
              {QStringLiteral("profiles"), QJsonArray()},
              {QStringLiteral("activeProfile"), QString()}};
  if(!file.exists())
    return true;
  if(!file.open(QIODevice::ReadOnly) || file.size() > 16 * 1024 * 1024)
  {
    error = QCoreApplication::translate(
        "AIConfiguration",
        "The AI configuration file could not be read. Your existing file has been preserved.");
    return false;
  }
  const QJsonDocument parsed = QJsonDocument::fromJson(file.readAll());
  const QJsonObject value = parsed.object();
  const QString schema = value.value(QStringLiteral("schema")).toString();
  if(!parsed.isObject() ||
     (schema != QStringLiteral("replaylab-ai-profiles/v2") &&
      schema != QStringLiteral("replaylab-ai-profiles/v1")) ||
     !value.value(QStringLiteral("profiles")).isArray())
  {
    error =
        QCoreApplication::translate("AIConfiguration",
                                    "The AI configuration file is invalid or uses an unsupported "
                                    "format. Your existing file has been preserved.");
    return false;
  }
  QStringList ids;
  for(const QJsonValue &entry : value.value(QStringLiteral("profiles")).toArray())
  {
    const QString id = entry.toObject().value(QStringLiteral("id")).toString();
    if(!entry.isObject() || !validId(id) || ids.contains(id))
    {
      error = invalidSettings(QStringLiteral("profiles/id"));
      return false;
    }
    ids.append(id);
  }
  const QString active = value.value(QStringLiteral("activeProfile")).toString();
  if(!active.isEmpty() && !ids.contains(active))
  {
    error = invalidSettings(QStringLiteral("activeProfile"));
    return false;
  }
  document = value;
  return true;
}

bool credential(const QString &verb, const QString &id, const QByteArray &input,
                QJsonObject &output, QString &error)
{
  output = {{QStringLiteral("profile"), id},
            {QStringLiteral("supported"), false},
            {QStringLiteral("stored"), false},
            {QStringLiteral("backend"), QStringLiteral("unavailable")}};
#ifdef Q_OS_WIN
  const QString target = QStringLiteral("ReplayLab/AI/API/v1/") + id;
  output[QStringLiteral("supported")] = true;
  output[QStringLiteral("backend")] = QStringLiteral("windows-credential-manager");
  bool ok = false;
  if(verb == QStringLiteral("status"))
  {
    PCREDENTIALW value = NULL;
    ok = CredReadW((LPCWSTR)target.utf16(), CRED_TYPE_GENERIC, 0, &value);
    if(ok)
    {
      output[QStringLiteral("stored")] = value->CredentialBlobSize > 0;
      CredFree(value);
    }
    else if(GetLastError() == ERROR_NOT_FOUND)
      ok = true;
  }
  else if(verb == QStringLiteral("delete"))
  {
    ok = CredDeleteW((LPCWSTR)target.utf16(), CRED_TYPE_GENERIC, 0);
    output[QStringLiteral("deleted")] = ok;
    if(!ok && GetLastError() == ERROR_NOT_FOUND)
      ok = true;
  }
  else if(verb == QStringLiteral("store"))
  {
    QString key = QString::fromUtf8(input).trimmed();
    if(key.startsWith(QStringLiteral("Bearer"), Qt::CaseInsensitive) && key.size() > 6 &&
       key[6].isSpace())
      key = key.mid(6).trimmed();
    if(key.isEmpty() || key.compare(QStringLiteral("Bearer"), Qt::CaseInsensitive) == 0 ||
       !cleanLine(key, 2560) || QString::fromUtf8(input).toUtf8() != input)
    {
      error = invalidSettings(QStringLiteral("api-key"));
      return false;
    }
    QByteArray bytes = key.toUtf8();
    if(bytes.size() > 2560)
    {
      error = invalidSettings(QStringLiteral("api-key"));
      return false;
    }
    CREDENTIALW value = {};
    value.Type = CRED_TYPE_GENERIC;
    value.TargetName = (LPWSTR)target.utf16();
    value.Persist = CRED_PERSIST_LOCAL_MACHINE;
    value.CredentialBlobSize = DWORD(bytes.size());
    value.CredentialBlob = (LPBYTE)bytes.data();
    ok = CredWriteW(&value, 0);
    bytes.fill(0);
    output[QStringLiteral("stored")] = ok;
  }
  if(ok)
    return true;
  error = QCoreApplication::translate(
              "AIConfiguration",
              "Windows Credential Manager could not be updated or read (Windows error %1).")
              .arg(qulonglong(GetLastError()));
#else
  if(verb != QStringLiteral("store"))
    return true;
  error = QCoreApplication::translate(
      "AIConfiguration", "Secure credential storage is unavailable. Use a session-only key.");
#endif
  return false;
}

bool ordinaryTree(const QString &path)
{
  const QFileInfo info(path);
#ifdef Q_OS_WIN
  const DWORD attributes = GetFileAttributesW((LPCWSTR)path.utf16());
  if(attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_REPARSE_POINT))
    return false;
#endif
  if(info.isSymLink())
    return false;
  if(info.isDir())
    for(const QFileInfo &child : QDir(path).entryInfoList(QDir::AllEntries | QDir::Hidden |
                                                          QDir::System | QDir::NoDotAndDotDot))
      if(!ordinaryTree(child.absoluteFilePath()))
        return false;
  return true;
}

bool clearProviderState(const QJsonObject &profile, QString &error)
{
  const QString id = profile.value(QStringLiteral("id")).toString();
  const QString preset = profile.value(QStringLiteral("preset")).toString();
  const QString directory =
      catalog().value(QStringLiteral("vendorStateDirectories")).toObject().value(preset).toString();
  if(directory.isEmpty())
    return true;
  const QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
  QString local = environment.value(QStringLiteral("LOCALAPPDATA"));
  if(local.isEmpty())
    local = environment.value(QStringLiteral("XDG_DATA_HOME"));
  if(local.isEmpty())
    local = QDir::home().filePath(QStringLiteral(".local/share"));
  QStringList targets = {
      QDir(local).filePath(QStringLiteral("ReplayLab/") + directory + QLatin1Char('/') + id)};
  if(preset == QStringLiteral("codex"))
    targets.append(QDir(local).filePath(QStringLiteral("RenderDocBread/provider-state/codex/") + id));
  const QString leasePath = QFileInfo(configurationPath()).absolutePath() +
                            QStringLiteral("/provider-use/") + id + QStringLiteral(".lock");
  QDir().mkpath(QFileInfo(leasePath).absolutePath());
  bool available = true;
#ifdef Q_OS_WIN
  HANDLE lease = CreateFileW((LPCWSTR)leasePath.utf16(), GENERIC_READ | GENERIC_WRITE, 0, NULL,
                             OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
  available = lease != INVALID_HANDLE_VALUE;
#else
  Transaction lease;
  available = lease.lock(leasePath);
#endif
  if(available)
    for(const QString &target : targets)
    {
      // Reject redirection in every managed parent and child before touching this exact id.
      QString parent = target;
      while(parent.size() > QDir::cleanPath(local).size())
      {
        const QFileInfo info(parent);
#ifdef Q_OS_WIN
        const DWORD attrs = GetFileAttributesW((LPCWSTR)parent.utf16());
        if(attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
          available = false;
#endif
        if(info.isSymLink())
          available = false;
        parent = info.absolutePath();
      }
      if(!available || !ordinaryTree(target))
      {
        available = false;
        break;
      }
      if(!QFileInfo::exists(target))
        continue;
      if(!QFileInfo(target).isDir())
      {
        available = false;
        break;
      }
      const QString retired = QFileInfo(target).absolutePath() + QStringLiteral("/.delete-") + id +
                              QLatin1Char('-') + QUuid::createUuid().toString();
      if(!QDir().rename(target, retired))
      {
        available = false;
        break;
      }
      if(!QDir(retired).removeRecursively())
      {
        QDir().rename(retired, target);
        available = false;
        break;
      }
    }
#ifdef Q_OS_WIN
  if(lease != INVALID_HANDLE_VALUE)
    CloseHandle(lease);
#endif
  if(!available)
    error = QCoreApplication::translate("AIConfiguration",
                                        "The provider's local state is in use or cannot be "
                                        "removed. Close its active tasks and retry.");
  return available;
}
}

QJsonArray AIConfiguration::providerPresets()
{
  return catalog().value(QStringLiteral("presets")).toArray();
}

bool AIConfiguration::handles(const QStringList &arguments)
{
  if(arguments.size() < 2 || arguments[0] != QStringLiteral("profile"))
    return false;
  return QStringList({QStringLiteral("list"), QStringLiteral("add"), QStringLiteral("update"),
                      QStringLiteral("select"), QStringLiteral("remove"),
                      QStringLiteral("credential")})
      .contains(arguments[1]);
}

AIConfiguration::Reply AIConfiguration::execute(const QStringList &arguments, const QByteArray &input)
{
  Reply reply;
  const QString verb = arguments.value(1);
  const bool writing = verb == QStringLiteral("add") || verb == QStringLiteral("update") ||
                       verb == QStringLiteral("select") || verb == QStringLiteral("remove");
  Transaction transaction;
  if(writing)
  {
    if(!QDir().mkpath(QFileInfo(configurationPath()).absolutePath()) ||
       !transaction.lock(configurationPath() + QStringLiteral(".update.lock")))
    {
      reply.diagnostic =
          QCoreApplication::translate("AIConfiguration",
                                      "The AI configuration file is being updated or cannot be "
                                      "written. Retry saving the connection.");
      return reply;
    }
  }
  QJsonObject document;
  if(!load(document, reply.diagnostic))
    return reply;
  QJsonArray rows = document.value(QStringLiteral("profiles")).toArray();
  QString active = document.value(QStringLiteral("activeProfile")).toString();
  QJsonDocument result;
  if(verb == QStringLiteral("list"))
  {
    QJsonArray wire;
    for(const QJsonValue &entry : rows)
    {
      QJsonObject row = emptyProfile(entry.toObject().value(QStringLiteral("id")).toString());
      const QJsonObject existing = entry.toObject();
      for(auto it = existing.begin(); it != existing.end(); ++it)
        row[it.key()] = it.value();
      const QString error = validate(row);
      if(!error.isEmpty())
      {
        reply.diagnostic = error;
        return reply;
      }
      wire.append(wireProfile(row, active));
    }
    result = QJsonDocument(wire);
  }
  else if(verb == QStringLiteral("credential"))
  {
    const QString operation = arguments.value(2);
    const QString id = arguments.value(arguments.indexOf(QStringLiteral("--profile")) + 1);
    QJsonObject profile;
    for(const QJsonValue &entry : rows)
      if(entry.toObject().value(QStringLiteral("id")).toString() == id)
        profile = entry.toObject();
    const QJsonObject preset = presetFor(profile.value(QStringLiteral("preset")).toString());
    if(!validId(id) || profile.isEmpty() ||
       (operation == QStringLiteral("store") &&
        (preset.value(QStringLiteral("kind")).toString() != QStringLiteral("model") ||
         preset.value(QStringLiteral("auth")).toString() != QStringLiteral("api-key"))))
    {
      reply.diagnostic = invalidSettings(QStringLiteral("credential/profile"));
      return reply;
    }
    QJsonObject output;
    if(!credential(operation, id, input, output, reply.diagnostic))
      return reply;
    result = QJsonDocument(output);
  }
  else
  {
    const QString id = arguments.value(2);
    int index = -1;
    for(int i = 0; i < rows.size(); ++i)
      if(rows[i].toObject().value(QStringLiteral("id")).toString() == id)
        index = i;
    if(!validId(id) || (verb == QStringLiteral("add") ? index >= 0 : index < 0))
    {
      reply.diagnostic = invalidSettings(QStringLiteral("profile/id"));
      return reply;
    }
    QJsonObject row = emptyProfile(id);
    if(index >= 0)
    {
      const QJsonObject existing = rows[index].toObject();
      for(auto it = existing.begin(); it != existing.end(); ++it)
        row[it.key()] = it.value();
    }
    if(verb == QStringLiteral("remove"))
    {
      rows.removeAt(index);
      if(active == id)
        active =
            rows.isEmpty() ? QString() : rows[0].toObject().value(QStringLiteral("id")).toString();
      // Best-effort state cleanup cannot prevent removal of the connection metadata.
      QString cleanupWarning;
      clearProviderState(row, cleanupWarning);
    }
    else if(verb == QStringLiteral("select"))
      active = id;
    else
    {
      const QJsonObject previous = row;
      if(arguments.contains(QStringLiteral("--replace-provider")))
      {
        row = emptyProfile(id);
        row[QStringLiteral("title")] = previous.value(QStringLiteral("title"));
      }
      QJsonArray models, capabilities;
      // Qt 5.9 can invalidate a nested object's binary offsets when the owning row is edited.
      // Give options independent storage before changing the row and reinserting it.
      QJsonObject options =
          QJsonDocument::fromJson(QJsonDocument(row.value(QStringLiteral("options")).toObject())
                                      .toJson(QJsonDocument::Compact))
              .object();
      for(int i = 3; i < arguments.size(); ++i)
      {
        const QString flag = arguments[i];
        if(flag == QStringLiteral("--json") || flag == QStringLiteral("--replace-provider"))
          continue;
        if(flag == QStringLiteral("--select"))
        {
          active = id;
          continue;
        }
        if(i + 1 >= arguments.size())
        {
          reply.diagnostic = invalidSettings(flag);
          return reply;
        }
        const QString value = arguments[++i];
        if(flag == QStringLiteral("--allowed-model"))
        {
          if(!models.contains(value))
            models.append(value);
        }
        else if(flag == QStringLiteral("--capability"))
          capabilities.append(value);
        else if(flag == QStringLiteral("--command-json"))
        {
          const QJsonDocument command = QJsonDocument::fromJson(value.toUtf8());
          if(!command.isArray())
          {
            reply.diagnostic = invalidSettings(QStringLiteral("command"));
            return reply;
          }
          row[QStringLiteral("command")] = command.array();
        }
        else if(flag == QStringLiteral("--option"))
        {
          const QString key = value.section(QLatin1Char('='), 0, 0);
          const QString setting = value.mid(key.size() + 1);
          if(!QStringList({QStringLiteral("api_version"), QStringLiteral("project"),
                           QStringLiteral("location"), QStringLiteral("subscription_auth_flow"),
                           QStringLiteral("allow_unsandboxed_agent")})
                  .contains(key))
          {
            reply.diagnostic = invalidSettings(QStringLiteral("options"));
            return reply;
          }
          options[key] = key == QStringLiteral("allow_unsandboxed_agent")
                             ? QJsonValue(setting == QStringLiteral("true"))
                             : QJsonValue(setting);
        }
        else
        {
          const QMap<QString, QString> fields = {
              {QStringLiteral("--preset"), QStringLiteral("preset")},
              {QStringLiteral("--title"), QStringLiteral("title")},
              {QStringLiteral("--base-url"), QStringLiteral("base_url")},
              {QStringLiteral("--api-key-env"), QStringLiteral("api_key_env")},
              {QStringLiteral("--default-model"), QStringLiteral("default_model")}};
          if(!fields.contains(flag))
          {
            reply.diagnostic = invalidSettings(flag);
            return reply;
          }
          row[fields.value(flag)] = value;
          if(flag == QStringLiteral("--default-model"))
            row[QStringLiteral("model")] = value;
        }
      }
      // An empty model selection on a subscription deliberately clears the prior selection.
      row[QStringLiteral("models")] = models;
      row[QStringLiteral("options")] = options;
      if(!capabilities.isEmpty())
        row[QStringLiteral("probed_capabilities")] = capabilities;
      reply.diagnostic = validate(row);
      if(!reply.diagnostic.isEmpty())
        return reply;
      if(previous.value(QStringLiteral("preset")) != row.value(QStringLiteral("preset")) && index >= 0)
      {
        if(!clearProviderState(previous, reply.diagnostic))
          return reply;
        QJsonObject ignored;
        if(!credential(QStringLiteral("delete"), id, QByteArray(), ignored, reply.diagnostic))
          return reply;
      }
      if(index >= 0)
        rows[index] = row;
      else
        rows.append(row);
      if(active.isEmpty())
        active = id;
    }
    document[QStringLiteral("schema")] = QStringLiteral("replaylab-ai-profiles/v2");
    document[QStringLiteral("profiles")] = rows;
    document[QStringLiteral("activeProfile")] = active;
    QSaveFile file(configurationPath());
    const QByteArray bytes = QJsonDocument(document).toJson();
    if(!file.open(QIODevice::WriteOnly) || file.write(bytes) != bytes.size() || !file.commit())
    {
      reply.diagnostic = QCoreApplication::translate(
          "AIConfiguration",
          "The AI configuration could not be saved. Your existing file has been preserved.");
      return reply;
    }
    result = QJsonDocument(wireProfile(row, active));
  }
  reply.output = result.toJson(QJsonDocument::Compact);
  reply.exitCode = 0;
  return reply;
}
