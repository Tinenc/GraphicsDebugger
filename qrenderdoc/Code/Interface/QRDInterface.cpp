/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2017-2026 Baldur Karlsson
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#include "QRDInterface.h"
#include <cstdlib>
#include <QDebug>
#include <QDir>
#include <QFileInfo>
#include <QProcessEnvironment>
#include <QStandardPaths>
#include "Code/QRDUtils.h"

QVariant EnvModToVariant(const EnvironmentModification &env)
{
  QVariantMap ret;
  ret[lit("variable")] = env.name;
  ret[lit("value")] = env.value;
  ret[lit("type")] = ToQStr(env.mod);
  ret[lit("separator")] = ToQStr(env.sep);
  return ret;
}

EnvironmentModification EnvModFromVariant(const QVariant &v)
{
  QVariantMap data = v.toMap();

  EnvironmentModification ret;

  ret.name = data[lit("variable")].toString().toUtf8().data();
  ret.value = data[lit("value")].toString().toUtf8().data();

  QString t = data[lit("type")].toString();

  if(t == ToQStr(EnvMod::Append))
    ret.mod = EnvMod::Append;
  else if(t == ToQStr(EnvMod::Prepend))
    ret.mod = EnvMod::Prepend;
  else
    ret.mod = EnvMod::Set;

  QString s = data[lit("separator")].toString();

  if(s == ToQStr(EnvSep::SemiColon))
    ret.sep = EnvSep::SemiColon;
  else if(s == ToQStr(EnvSep::Colon))
    ret.sep = EnvSep::Colon;
  else if(s == ToQStr(EnvSep::Platform))
    ret.sep = EnvSep::Platform;
  else
    ret.sep = EnvSep::NoSep;

  return ret;
}

CaptureSettings::CaptureSettings()
{
  inject = false;
  autoStart = false;
  queuedFrameCap = 0;
  numQueuedFrames = 0;
}

CaptureSettings::operator QVariant() const
{
  QVariantMap ret;

  ret[lit("inject")] = inject;
  ret[lit("autoStart")] = autoStart;

  ret[lit("executable")] = executable;
  ret[lit("workingDir")] = workingDir;
  ret[lit("commandLine")] = commandLine;

  QVariantList env;
  for(int i = 0; i < environment.count(); i++)
    env.push_back(EnvModToVariant(environment[i]));
  ret[lit("environment")] = env;

  QVariantMap opts;
  opts[lit("allowVSync")] = options.allowVSync;
  opts[lit("allowFullscreen")] = options.allowFullscreen;
  opts[lit("apiValidation")] = options.apiValidation;
  opts[lit("captureCallstacks")] = options.captureCallstacks;
  opts[lit("captureCallstacksOnlyDraws")] = options.captureCallstacksOnlyActions;
  opts[lit("delayForDebugger")] = options.delayForDebugger;
  opts[lit("verifyBufferAccess")] = options.verifyBufferAccess;
  opts[lit("hookIntoChildren")] = options.hookIntoChildren;
  opts[lit("refAllResources")] = options.refAllResources;
  opts[lit("captureAllCmdLists")] = options.captureAllCmdLists;
  opts[lit("debugOutputMute")] = options.debugOutputMute;
  opts[lit("softMemoryLimit")] = options.softMemoryLimit;
  ret[lit("options")] = opts;

  ret[lit("queuedFrameCap")] = queuedFrameCap;
  ret[lit("numQueuedFrames")] = numQueuedFrames;
  ret[lit("monitorScriptPath")] = monitorScriptPath;

  return ret;
}

CaptureSettings::CaptureSettings(const QVariant &v)
{
  QVariantMap data = v.toMap();

  inject = data[lit("inject")].toBool();
  autoStart = data[lit("autoStart")].toBool();

  executable = data[lit("executable")].toString();
  workingDir = data[lit("workingDir")].toString();
  commandLine = data[lit("commandLine")].toString();

  QVariantList env = data[lit("environment")].toList();
  for(int i = 0; i < env.size(); i++)
  {
    EnvironmentModification e = EnvModFromVariant(env[i]);
    environment.push_back(e);
  }

  QVariantMap opts = data[lit("options")].toMap();

  options.allowVSync = opts[lit("allowVSync")].toBool();
  options.allowFullscreen = opts[lit("allowFullscreen")].toBool();
  options.apiValidation = opts[lit("apiValidation")].toBool();
  options.captureCallstacks = opts[lit("captureCallstacks")].toBool();
  options.captureCallstacksOnlyActions = opts[lit("captureCallstacksOnlyDraws")].toBool();
  options.delayForDebugger = opts[lit("delayForDebugger")].toUInt();
  // old name for verifyBufferAccess was verifyMapWrites, so use that as a fallback
  if(opts.contains(lit("verifyBufferAccess")))
    options.verifyBufferAccess = opts[lit("verifyBufferAccess")].toBool();
  else
    options.verifyBufferAccess = opts[lit("verifyMapWrites")].toBool();
  options.hookIntoChildren = opts[lit("hookIntoChildren")].toBool();
  options.refAllResources = opts[lit("refAllResources")].toBool();
  options.captureAllCmdLists = opts[lit("captureAllCmdLists")].toBool();
  options.debugOutputMute = opts[lit("debugOutputMute")].toBool();
  options.softMemoryLimit = opts[lit("softMemoryLimit")].toUInt();

  if(data.contains(lit("queuedFrameCap")))
    queuedFrameCap = data[lit("queuedFrameCap")].toUInt();
  else
    queuedFrameCap = 0;
  if(data.contains(lit("numQueuedFrames")))
    numQueuedFrames = data[lit("numQueuedFrames")].toUInt();
  else
    numQueuedFrames = 0;

  if(data.contains(lit("monitorScriptPath")))
    monitorScriptPath = data[lit("monitorScriptPath")].toString();
}

rdcstr ConfigFilePath(const rdcstr &filename)
{
  // QProcessEnvironment preserves the native Unicode environment on Windows. qgetenv's local
  // encoding conversion must not redirect a non-ASCII isolation directory to a different path.
  // i18n-ignore: Process-owner environment key, not user-visible UI text.
  QString isolated = QProcessEnvironment::systemEnvironment().value(lit("RENDERDOC_CONFIG_DIR"));
  if(!isolated.isEmpty())
  {
    QString leaf = filename;
#if defined(Q_OS_WIN)
    isolated.replace(QLatin1Char('\\'), QLatin1Char('/'));
    leaf.replace(QLatin1Char('\\'), QLatin1Char('/'));
    const QChar driveLetter = isolated[0].toLower();
    const bool drive = isolated.size() >= 3 && driveLetter >= QLatin1Char('a') &&
                       driveLetter <= QLatin1Char('z') && isolated[1] == QLatin1Char(':') &&
                       isolated[2] == QLatin1Char('/');
    const int share = isolated.indexOf(QLatin1Char('/'), 2);
    const bool unc = isolated.size() > 4 && isolated[0] == QLatin1Char('/') &&
                     isolated[1] == QLatin1Char('/') && isolated[2] != QLatin1Char('/') &&
                     isolated[2] != QLatin1Char('?') && isolated[2] != QLatin1Char('.') &&
                     share > 2 && share + 1 < isolated.size() &&
                     isolated[share + 1] != QLatin1Char('/');
    bool valid = drive || unc;
#else
    bool valid = isolated.startsWith(QLatin1Char('/'));
#endif
    // Only this function's root-directory callers may pass an empty leaf. All actual files and
    // subdirectories must remain relative to the explicitly supplied root, before normalization.
    valid = valid && !leaf.startsWith(QLatin1Char('/')) && !leaf.contains(QLatin1Char(':'));
    for(const QString &component : isolated.split(QLatin1Char('/')))
    {
      valid = valid && component != QLatin1String(".") && component != QLatin1String("..");
#if defined(Q_OS_WIN)
      // Win32 strips trailing dots/spaces; do not allow normalization to introduce traversal.
      valid = valid && !component.endsWith(QLatin1Char('.')) &&
              !component.endsWith(QLatin1Char(' '));
#endif
    }
    if(!leaf.isEmpty())
      for(const QString &component : leaf.split(QLatin1Char('/')))
      {
        valid = valid && !component.isEmpty() && component != QLatin1String(".") &&
                component != QLatin1String("..");
#if defined(Q_OS_WIN)
        valid = valid && !component.endsWith(QLatin1Char('.')) &&
                !component.endsWith(QLatin1Char(' '));
#endif
      }
    for(QChar c : isolated + leaf)
      valid = valid && c.unicode() >= 32 && c != QLatin1Char('"') && c != QLatin1Char('*') &&
              c != QLatin1Char('?') && c != QLatin1Char('<') && c != QLatin1Char('>') &&
              c != QLatin1Char('|');

    if(!valid)
    {
      // i18n-ignore: Startup log diagnostic for an invalid process-owner isolation request.
      qCritical("RENDERDOC_CONFIG_DIR requires an absolute directory and a contained config path; "
                "refusing user-directory fallback");
      std::exit(2);
    }

    isolated = QDir::cleanPath(isolated);
    if(!QDir().mkpath(isolated) || !QFileInfo(isolated).isDir())
    {
      // i18n-ignore: Startup log diagnostic; no translated UI has been initialized on this path.
      qCritical("Cannot create RENDERDOC_CONFIG_DIR; refusing user-directory fallback");
      std::exit(2);
    }

    return QDir::cleanPath(QDir(isolated).absoluteFilePath(leaf));
  }

  QString path = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);

  QDir dir(path);
  if(!dir.exists())
    dir.mkdir(lit("."));

  return QDir::cleanPath(dir.absoluteFilePath(filename));
}
