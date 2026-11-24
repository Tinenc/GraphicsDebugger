/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2016-2026 Baldur Karlsson
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

#include <stdio.h>
#include <stdlib.h>
#include <string>

#if defined(RENDERDOC_PLATFORM_WIN32)
#include <windows.h>
#endif

#include <QApplication>
#include <QCommandLineParser>
#include <QDir>
#include <QFileInfo>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QRegularExpressionMatch>
#include <QStandardPaths>
#include <QSysInfo>
#include "Code/BreadVersion.h"
#include "Code/CaptureContext.h"
#include "Code/QRDUtils.h"
#include "Code/Resources.h"
#include "Code/TranslationManager.h"
#include "Code/pyrenderdoc/PythonContext.h"
#include "Windows/Dialogs/CrashDialog.h"
#include "Windows/Dialogs/UpdateDialog.h"
#include "Windows/MainWindow.h"
#include "version.h"

#if QT_VERSION >= QT_VERSION_CHECK(5, 9, 0)

#include <QOperatingSystemVersion>

QString getOSVersion()
{
  QOperatingSystemVersion ver = QOperatingSystemVersion::current();

  if(ver.type() == QOperatingSystemVersion::Windows && ver.majorVersion() >= 10)
  {
    int major = ver.majorVersion();
    int build = ver.microVersion();
    if(build >= 22000)
      major = 11;

    return QFormatStr("Windows %1 Build num %2").arg(major).arg(build);
  }

  return QSysInfo::prettyProductName();
}

#else

QString getOSVersion()
{
  return QSysInfo::prettyProductName();
}

#endif

#if ENABLE_UNIT_TESTS

#define CATCH_CONFIG_RUNNER
#define CATCH_CONFIG_NOSTDOUT

#include "3rdparty/catch/catch.hpp"

// since we force use of ToStr for everything and don't allow using catch's stringstream (so that
// enums get forwarded to ToStr) we need to implement ToStr for one of Catch's structs.
template <>
rdcstr DoStringise(const Catch::SourceLineInfo &el)
{
  return QFormatStr("%1:%2").arg(QString::fromUtf8(el.file)).arg(el.line);
}

class LogOutputter : public std::stringbuf
{
  FILE *file;

public:
  LogOutputter(FILE *f) : file(f) {}
  void finish()
  {
    std::string msg = this->str();
    RENDERDOC_LogMessage(LogType::Comment, "EXTN", __FILE__, __LINE__, msg.c_str());
    fputs(msg.c_str(), file);
  }
  virtual int sync() override
  {
    rdcstr str = this->str().c_str();
    int idx = str.indexOf('\n');
    if(idx >= 0)
    {
      rdcstr msg = str.substr(0, idx + 1);
      RENDERDOC_LogMessage(LogType::Comment, "EXTN", __FILE__, __LINE__, msg);
      fputs(msg.c_str(), file);
      str = str.substr(idx + 1);
      this->str("");
      this->sputn(str.c_str(), str.size());
    }
    return 0;
  }

  // force a sync on every output
  virtual std::streamsize xsputn(const char *s, std::streamsize n) override
  {
    std::streamsize ret = std::stringbuf::xsputn(s, n);
    sync();
    return ret;
  }
};

std::ostream *catch_stream = NULL;

namespace Catch
{
std::ostream &cout()
{
  return *catch_stream;
}
std::ostream &cerr()
{
  return *catch_stream;
}
std::ostream &clog()
{
  return *catch_stream;
}
}

#endif

#if defined(Q_OS_WIN32)
extern "C" {
_declspec(dllexport) unsigned long NvOptimusEnablement = 0x00000001;
_declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}
#endif

REPLAY_PROGRAM_MARKER()

void sharedLogOutput(QtMsgType type, const QMessageLogContext &context, const QString &msg)
{
  LogType logtype = LogType::Comment;

  switch(type)
  {
    case QtDebugMsg: logtype = LogType::Debug; break;
    case QtInfoMsg: logtype = LogType::Comment; break;
    case QtWarningMsg: logtype = LogType::Warning; break;
    case QtCriticalMsg: logtype = LogType::Error; break;
    case QtFatalMsg: logtype = LogType::Fatal; break;
  }

  RENDERDOC_LogMessage(logtype, "QTRD", context.file ? context.file : rdcstr(), context.line, msg);
}

void hideOption(QCommandLineOption &opt)
{
#if QT_VERSION >= QT_VERSION_CHECK(5, 8, 0)
  opt.setFlags(QCommandLineOption::HiddenFromHelp);
#else
  opt.setHidden(true);
#endif
}

static int PrintBreadVersion()
{
  // i18n-ignore: machine-readable application version and build identifiers.
  std::string output = std::string("RenderDocBread " BREAD_FULL_VERSION_STRING " (") +
                       RENDERDOC_GetCommitHash() + ")\n";
#if defined(DISTRIBUTION_VERSION)
  // i18n-ignore: build distribution metadata in the version diagnostic.
  output += std::string("Packaged for ") + DISTRIBUTION_NAME + " - " + DISTRIBUTION_CONTACT + "\n";
#endif
#if defined(RENDERDOC_PLATFORM_WIN32)
  // The Windows GUI CRT may not bind stdout even when the process inherits a
  // redirected pipe. Write to that inherited handle without opening a console.
  DWORD written = 0;
  HANDLE handle = GetStdHandle(STD_OUTPUT_HANDLE);
  return handle && handle != INVALID_HANDLE_VALUE &&
                 WriteFile(handle, output.data(), DWORD(output.size()), &written, NULL) &&
                 written == output.size()
             ? 0
             : 1;
#else
  return fwrite(output.data(), 1, output.size(), stdout) == output.size() && fflush(stdout) == 0 ? 0
                                                                                                 : 1;
#endif
}

int main(int argc, char *argv[])
{
  // call this as the very first thing - no-op on other platforms, but on linux it means
  // XInitThreads will be called allowing driver access to xlib on multiple threads.
  QCoreApplication::setAttribute(Qt::AA_X11InitThreads);

  qInstallMessageHandler(sharedLogOutput);

  if(BreadRelease::InstallationInProgress())
  {
    qInfo() << "RenderDocBread is applying an update. The installer will restart it.";
    return 0;
  }

  // there seems to be a persistent crash in QWidgetPrivate::subtractOpaqueSiblings where a widget
  // has no parent but is not a window. Try to work around it by setting this env var, as it's only
  // an optimisation
  qputenv("QT_NO_SUBTRACTOPAQUESIBLINGS", lit("1").toUtf8());

  qInfo() << "QRenderDoc initialising.";

  if(IsRunningAsAdmin())
    qInfo() << "Running as administrator";

#if defined(RENDERDOC_PLATFORM_LINUX) && !defined(RENDERDOC_WINDOWING_WAYLAND)
  bool envChanged = false;
  {
    const char *qpa_plat = getenv("QT_QPA_PLATFORM");
    // if not set or empty, force non-wayland to help go through backwards compatibility path on wayland.
    if(!qpa_plat || qpa_plat[0] == 0)
    {
      setenv("QT_QPA_PLATFORM", "xcb", 1);
      envChanged = true;
    }
  }
#endif

  QGuiApplication::setAttribute(Qt::AA_EnableHighDpiScaling);
  QGuiApplication::setAttribute(Qt::AA_UseHighDpiPixmaps);

#if(QT_VERSION >= QT_VERSION_CHECK(5, 14, 0))
  QGuiApplication::setHighDpiScaleFactorRoundingPolicy(
      Qt::HighDpiScaleFactorRoundingPolicy::RoundPreferFloor);
#endif

  QApplication::setApplicationVersion(lit(BREAD_FULL_VERSION_STRING));

// shortcut here so we can run this with a non-GUI application
#if ENABLE_UNIT_TESTS
  if(QString::fromUtf8(argv[1]) == lit("--unittest"))
  {
    char **mod_argv = new char *[argc + 1];
    char **alloc_argv = mod_argv;
    for(int i = 0; i < argc; i++)
      mod_argv[i] = argv[i];
    mod_argv[argc] = 0;

    // pop --unittest
    argc--;
    mod_argv++;

    FILE *test_logOut = NULL;

    if(argc >= 2 && QString::fromUtf8(mod_argv[1]).left(4) == lit("log="))
    {
      test_logOut = fopen(mod_argv[1] + 4, "w");

      // pop
      argc--;
      mod_argv++;
    }

    mod_argv[0] = argv[0];

    if(test_logOut == NULL)
      test_logOut = stdout;

    LogOutputter logbuf(test_logOut);
    std::ostream logstream(&logbuf);

    int ret = 0;

    // catch tests first
    {
      catch_stream = &logstream;

      Catch::Session session;

      session.configData().name = "QRenderDoc";
      session.configData().shouldDebugBreak = Catch::isDebuggerActive();

      ret = session.applyCommandLine(argc, mod_argv);

      if(ret == 0)
      {
        int numFailed = session.run();

        // Note that on unices only the lower 8 bits are usually used, clamping
        // the return value to 255 prevents false negative when some multiple
        // of 256 tests has failed
        if(numFailed != 0)
          ret = (numFailed < 0xff ? numFailed : 0xff);
      }
    }

    GlobalEnvironment env;
    env.enumerateGPUs = false;
    rdcarray<rdcstr> coreargs;
    RENDERDOC_InitialiseReplay(env, coreargs);

    {
      QCoreApplication application(argc, mod_argv);
      PersistentConfig cfg;
      PythonContext::GlobalInit(cfg);

      logstream << "Checking python binding consistency.\n";

      rdcstr errorLog;
      bool errors = false;
      {
        PythonContextHandle py;
        errors = py.ctx().CheckInterfaces(errorLog);
      }

      if(errors)
      {
        logstream << errorLog;
        qCritical() << "Found errors in python bindings. Please fix!\n";
        ret = 1;
      }
      else
      {
        logstream << "Python bindings are consistent.\n";
      }

      PythonContext::GlobalShutdown();
    }

    RENDERDOC_ShutdownReplay();

    logbuf.finish();

    delete[] alloc_argv;

    fclose(test_logOut);
    return ret;
  }
#endif

  QApplication application(argc, argv);

  // Portable releases use a separate UI profile and only the co-located first-party runtime.
  // Existing installations keep their original settings and project identities.
  if(QFileInfo(QDir(QCoreApplication::applicationDirPath()).filePath(
         lit("RenderDocBread.portable"))).isFile())    // i18n-ignore: portable package marker
  {
    QCoreApplication::setApplicationName(lit("RenderDocBread"));    // i18n-ignore: product brand
    if(qEnvironmentVariableIsEmpty("RENDERDOC_CONFIG_DIR"))
    {
      QString localData;
#if defined(RENDERDOC_PLATFORM_WIN32)
      // Preserve Unicode and honor redirected per-process profiles. Qt's known-folder lookup
      // can otherwise resolve the account's original directory instead of LOCALAPPDATA.
      localData = QProcessEnvironment::systemEnvironment().value(lit("LOCALAPPDATA"));    // i18n-ignore: Windows profile environment key
#endif
      if(localData.isEmpty())
        localData = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
      const QString profile = QDir(localData)
                                  .filePath(lit("RenderDocBread/ui"));    // i18n-ignore: isolated portable UI profile path
      // Qt 5's qputenv uses the narrow MSVC CRT environment. UTF-8 bytes are interpreted in
      // the system code page there, corrupting non-ASCII profile paths. The wide CRT setter
      // preserves the native Windows environment and also synchronizes CRT presence checks.
#if defined(RENDERDOC_PLATFORM_WIN32)
      const bool profileSet = _wputenv_s(L"RENDERDOC_CONFIG_DIR", profile.toStdWString().c_str()) == 0;
#else
      const bool profileSet = qputenv("RENDERDOC_CONFIG_DIR", profile.toUtf8());
#endif
      if(!profileSet)
      {
        // i18n-ignore: Startup log diagnostic before translations/UI configuration are loaded.
        qCritical("Cannot set the portable configuration directory; refusing user-directory fallback");
        return 2;
      }
    }
    QCoreApplication::setLibraryPaths(
        {QDir(QCoreApplication::applicationDirPath()).filePath(lit("qtplugins"))});    // i18n-ignore: fixed bundled Qt plugin path
  }
  else if(QCoreApplication::applicationName().compare(lit("RenderDocBread"), Qt::CaseInsensitive) == 0)    // i18n-ignore: executable identity
  {
    // The renamed non-portable executable must keep the existing UI/conversation
    // store. This technical identity is not the displayed application brand.
    QCoreApplication::setApplicationName(lit("qTinecmaTool"));    // i18n-ignore: DF persistent-data and shared-extension path identity
  }

  QCommandLineParser parser;
  parser.setApplicationDescription(QApplication::translate("qrenderdoc", "RenderDocBread graphics debugger"));
  QCommandLineOption helpOption = parser.addHelpOption();
  QCommandLineOption versionOption = parser.addVersionOption();

  QCommandLineOption tempfile(
      lit("tempfile"),
      QApplication::translate(
          "qrenderdoc", "The filename to be opened is a temporary file owned by this instance."));
  parser.addOption(tempfile);

  QCommandLineOption targetcontrol({lit("targetcontrol"), lit("remoteaccess")},
                                   QApplication::translate(
                                       "qrenderdoc",
                                       "A target control connection to open on startup."),
                                   lit("host:port"));
  parser.addOption(targetcontrol);

  QCommandLineOption replayhost(lit("replayhost"),
                                QApplication::translate(
                                    "qrenderdoc", "The replay host to connect to on startup."),
                                lit("host"));
  parser.addOption(replayhost);

  QCommandLineOption python({lit("python"), lit("script"), lit("py")},
                            QApplication::translate(
                                "qrenderdoc", "Run a python script before opening the main UI."),
                            lit("filename.py"));
  parser.addOption(python);

  QCommandLineOption uiscript({lit("ui-python"), lit("ui-script"), lit("ui-py")},
                              QApplication::translate(
                                  "qrenderdoc", "Run a python script after opening the main UI."),
                              lit("filename.py"));
  parser.addOption(uiscript);

  // secret non-described options
  QCommandLineOption installLayer(lit("install_vulkan_layer"), QString(), lit("root_or_not"));
  hideOption(installLayer);
  parser.addOption(installLayer);

  QCommandLineOption updateFailed(lit("updatefailed"), QString(), lit("errormsg"));
  hideOption(updateFailed);
  parser.addOption(updateFailed);

  QCommandLineOption updateDoneAdmin(lit("updatedone_admin"));
  hideOption(updateDoneAdmin);
  parser.addOption(updateDoneAdmin);

  QCommandLineOption updateDone(lit("updatedone"));
  hideOption(updateDone);
  parser.addOption(updateDone);

  QCommandLineOption crashReport(lit("crash"), QString(), lit("reportpath"));
  hideOption(crashReport);
  parser.addOption(crashReport);

  parser.addPositionalArgument(
      lit("filename"), QApplication::translate("qrenderdoc", "The file to open."));

  bool parsedCommands = parser.parse(application.arguments());

  if(!parsedCommands)
    qCritical() << parser.errorText();

  if(parser.isSet(helpOption))
  {
    parser.showHelp();
    return 0;
  }

  if(parser.isSet(versionOption))
  {
    return PrintBreadVersion();
  }

  if(parser.isSet(installLayer))
  {
    qInfo() << "Updating Vulkan layer registration";
    if(parser.value(installLayer) == lit("root"))
      RENDERDOC_UpdateVulkanLayerRegistration(true);
    else
      RENDERDOC_UpdateVulkanLayerRegistration(false);
    return 0;
  }

  bool temp = parser.isSet(tempfile);

  bool updateApplied = false;

  if(parser.isSet(updateFailed))
  {
    RDDialog::critical(
        NULL, QApplication::translate("qrenderdoc", "Error updating"),
        QApplication::translate("qrenderdoc", "Error applying update: %1")
            .arg(parser.value(updateFailed)));
  }

  if(parser.isSet(updateDone))
  {
    qInfo() << "Finishing update as user";
    updateApplied = true;

    // the renderdoccmd updater that runs us is from the old version, so older versions might be
    // running us as admin expecting the version number to be updated.
    // if we're not running as admin, this will immediately exit
    RENDERDOC_UpdateInstalledVersionNumber();
  }

  if(parser.isSet(updateDoneAdmin))
  {
    qInfo() << "Finishing update as admin";
    RENDERDOC_UpdateInstalledVersionNumber();
    return 0;
  }

  QString remoteHost;
  uint remoteIdent = 0;

  if(parser.isSet(targetcontrol))
  {
    QRegularExpression regexp(lit("^([a-zA-Z\\.0-9_-]+)?(:([0-9]+))?$"));

    QRegularExpressionMatch match = regexp.match(parser.value(targetcontrol));

    if(!match.hasMatch())
    {
      qCritical() << "--targetcontrol option must be followed by host:port or host";
      return 1;
    }

    QString host = match.captured(1);

    bool ok = false;
    uint32_t ident = 0;
    if(match.capturedLength(2) > 0)
    {
      ident = match.captured(3).toUInt(&ok);
    }
    else
    {
      // no port specified, find the first open port.
      ident = RENDERDOC_EnumerateRemoteTargets(host, ident);
      ok = (ident != 0);
    }

    if(!ok)
    {
      if(match.capturedLength(2) > 0)
      {
        qCritical() << "--targetcontrol port " << match.captured(3) << "malformed";
      }
      else
      {
        qCritical() << "All ports are busy, cannot find an available port";
      }
      return 1;
    }
    remoteHost = host;
    remoteIdent = ident;
  }

  QString crashReportPath;
  if(parser.isSet(crashReport))
    crashReportPath = parser.value(crashReport);

  QString uiscriptFile;
  if(parser.isSet(uiscript))
    uiscriptFile = parser.value(uiscript);

  QStringList pyscripts = parser.values(python);

  // load the first filename in the positional arguments.
  QStringList remaining = parser.positionalArguments();

  QString filename;
  for(int i = 0; i < remaining.count(); i++)
  {
    const QString &fn = remaining[i];
    QFileInfo checkFile(fn);
    if(checkFile.exists() && checkFile.isFile())
    {
      filename = fn;
      remaining.removeAt(i);
      break;
    }
  }

  RegisterMetatypeConversions();

  {
    PersistentConfig config;

    // ConfigFilePath validates and creates an explicit isolation root before any UI settings are
    // loaded. Do not create the normal AppData directory when this process requested isolation.
    const QString configPath = ConfigFilePath(QString());
    QDir().mkpath(configPath);

    const QString configFilename = ConfigFilePath(lit("UI.config"));
    const bool configLoaded = config.Load(configFilename);

    // Install the persisted language before constructing any capture UI. Keeping the manager in
    // this scope ensures both translators outlive every translated widget and are removed before
    // QApplication is destroyed.
    TranslationManager translationManager(application);
    translationManager.ApplyLanguage(config.UILanguage);

    if(!configLoaded)
    {
      RDDialog::critical(
          NULL, CaptureContext::tr("Error loading config"),
          CaptureContext::tr(
              "Error loading config file\n%1\nA default config is loaded and will be saved out.")
              .arg(configFilename));
    }

    int replayHostIndex = -1;
    if(parser.isSet(replayhost))
    {
      rdcstr replayHost = parser.value(replayhost);
      rdcarray<RemoteHost> hosts = config.GetRemoteHosts();
      for(int i = 0; i < hosts.count(); i++)
      {
        if(hosts[i].Hostname() == replayHost)
        {
          replayHostIndex = i;
          break;
        }
      }
      if(replayHostIndex < 0)
      {
        RDDialog::critical(
            NULL, QApplication::translate("qrenderdoc", "Error loading remote host"),
            QApplication::translate(
                "qrenderdoc",
                "Remote host %1 doesn't exist. Please add it in Remote Host Manager first.")
                .arg(parser.value(replayhost)));
      }
    }

    if(config.Analytics_TotalOptOut)
      Analytics::Disable();
    else
      Analytics::Load();

    bool styleSet = config.SetStyle();

    // unrecognised style, or empty (none set), choose a default
    if(!styleSet)
    {
      config.UIStyle = lit("ultra");    // i18n-ignore: persisted Black theme identifier

      config.SetStyle();
    }

    config.SetupFormatting();

    Resources::Initialise();

    GUIInvoke::init();

    {
      GlobalEnvironment env;
#if defined(RENDERDOC_PLATFORM_LINUX)
      env.xlibDisplay = QX11Info::display();
      if(QGuiApplication::platformName() == lit("wayland"))
      {
        env.waylandDisplay = (wl_display *)AccessWaylandPlatformInterface("display", NULL);

        QString warning = QApplication::translate(
            "qrenderdoc", "Running directly on Wayland is NOT SUPPORTED and is likely to crash, "
                          "hang, or fail to render.");

        qInfo() << "------ !!!! WARNING !!!! ------";
        qInfo() << warning;
        qInfo() << "------ !!!! WARNING !!!! ------";

        RDDialog::critical(
            NULL, QApplication::translate("qrenderdoc", "Wayland Qt platform not supported"),
            warning);
      }
#endif
      rdcarray<rdcstr> coreargs;
      if(!crashReportPath.isEmpty())
        coreargs.push_back("--crash");
      for(const QString &arg : remaining)
        coreargs.push_back(arg);

      // don't enumerate GPUs when reporting a crash, in case enumerating GPUs *causes* the crash.
      if(!crashReportPath.isEmpty())
        env.enumerateGPUs = false;

      RENDERDOC_InitialiseReplay(env, coreargs);
    }

#if defined(RENDERDOC_PLATFORM_LINUX) && !defined(RENDERDOC_WINDOWING_WAYLAND)
    if(envChanged)
      unsetenv("QT_QPA_PLATFORM");
#endif

    if(!crashReportPath.isEmpty())
    {
      QVariantMap json;

      {
        QFile f(crashReportPath);

        if(f.exists() && f.open(QIODevice::ReadOnly | QIODevice::Text))
          json = JSONToVariant(QString::fromUtf8(f.readAll()));
      }

      if(json.contains(lit("report")))
      {
        CrashDialog dialog(config, json);

        RDDialog::show(&dialog);
      }
    }
    else
    {
      PythonContext::GlobalInit(config);

      if(updateApplied)
      {
        config.CheckUpdate_UpdateAvailable = false;
        config.CheckUpdate_UpdateResponse = "";
        config.Save();
      }

      CaptureContext ctx(config);
      if(replayHostIndex >= 0)
      {
        ctx.SetRemoteHost(replayHostIndex);
      }
      Analytics::Prompt(ctx, config);

      ANALYTIC_SET(Metadata.RenderDocVersion, lit(FULL_VERSION_STRING));
#if defined(DISTRIBUTION_VERSION)
      ANALYTIC_SET(Metadata.DistributionVersion, lit(DISTRIBUTION_NAME));
#endif
      ANALYTIC_SET(Metadata.Bitness, ((sizeof(void *) == sizeof(uint64_t)) ? 64 : 32));
      ANALYTIC_SET(Metadata.OSVersion, getOSVersion());

#if RENDERDOC_STABLE_BUILD
      ANALYTIC_SET(Metadata.OfficialBuildRun, true);
#else
      ANALYTIC_SET(Metadata.DevelBuildRun, true);
#endif

      ANALYTIC_SET(Metadata.DaysUsed[QDateTime::currentDateTime().date().day()], true);

      bool pythonExited = false;

      if(!pyscripts.isEmpty())
      {
        PythonContextHandle py;

        ANALYTIC_SET(UIFeatures.PythonInterop, true);

        QObject::connect(&py.ctx(), &PythonContext::exception,
                         [&pythonExited](const QString &, const QString &type, const QString &value,
                                         int, QList<QString> frames) {
                           if(type == lit("SystemExit"))
                           {
                             pythonExited = true;
                             return;
                           }

                           QString exString;

                           if(!frames.isEmpty())
                           {
                             exString += QApplication::translate(
                                 "qrenderdoc", "Traceback (most recent call last):\n");
                             for(const QString &f : frames)
                               exString += QFormatStr("  %1\n").arg(f);
                           }
                           exString += QFormatStr("%1: %2\n").arg(type).arg(value);

                           qCritical("%s", exString.toUtf8().data());
                         });

        QObject::connect(&py.ctx(), &PythonContext::textOutput,
                         [](const QString &, bool isStdError, const QString &output) {
                           if(isStdError)
                             qCritical("%s", output.toUtf8().data());
                           else
                             qInfo("%s", output.toUtf8().data());
                         });

        for(const QString &f : pyscripts)
        {
          QFileInfo checkFile(f);
          if(checkFile.exists() && checkFile.isFile())
          {
            qInfo() << "running" << f;
            py.ctx().executeFile(f);
          }
          else
          {
            qWarning() << "Invalid python script" << f;
          }

          if(pythonExited)
            break;
        }
      }

      if(!pythonExited)
      {
        ctx.Begin(filename, remoteHost, remoteIdent, temp, uiscriptFile);

        while(ctx.isRunning())
        {
          application.processEvents(QEventLoop::WaitForMoreEvents);
          QCoreApplication::sendPostedEvents();
          QCoreApplication::sendPostedEvents(NULL, QEvent::DeferredDelete);
        }
      }

      config.Save();
    }

    RENDERDOC_ShutdownReplay();

    PythonContext::GlobalShutdown();

    Formatter::shutdown();
  }

  return 0;
}
