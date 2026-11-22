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

#include "CaptureContext.h"
#include <QApplication>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QDirIterator>
#include <QElapsedTimer>
#include <QEvent>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QMenu>
#include <QMessageBox>
#include <QMetaObject>
#include <QProgressDialog>
#include <QRegularExpression>
#include <QSet>
#include <QTimer>
#include <memory>
#include "Code/BreadVersion.h"
#include "Code/Resources.h"
#include "Code/ReplayLab/ReplayLabIntegration.h"
#include "Code/CodeEditorService.h"
#include "Code/pyrenderdoc/PythonContext.h"
#include "Widgets/AnnotationDisplay.h"
#include "Windows/APIInspector.h"
#include "Windows/AI/AIAssistant.h"
#include "Windows/ApiMonitorWindow.h"
#include "Windows/BufferViewer.h"
#include "Windows/BookmarksView.h"
#include "Windows/CommentView.h"
#include "Windows/DebugMessageView.h"
#include "Windows/DescriptorViewer.h"
#include "Widgets/ShaderCodeEditor.h"
#include "Windows/Dialogs/CaptureDialog.h"
#include "Windows/Dialogs/CrashDialog.h"
#include "Windows/Dialogs/LiveCapture.h"
#include "Windows/Dialogs/SettingsDialog.h"
#include "Windows/EventBrowser.h"
#include "Windows/LogView.h"
#include "Windows/MainWindow.h"
#include "Windows/PerformanceCounterViewer.h"
#include "Windows/PipelineState/PipelineStateViewer.h"
#include "Windows/PixelHistoryView.h"
#include "Windows/PythonShell.h"
#include "Windows/ResourceInspector.h"
#include "Windows/ShaderMessageViewer.h"
#include "Windows/ShaderViewer.h"
#include "Windows/StatisticsViewer.h"
#include "Windows/TextureViewer.h"
#include "Windows/TimelineBar.h"
#include "MiniQtHelper.h"
#include "QRDUtils.h"
#include "RGPInterop.h"
#include "version.h"

#include "pipestate.inl"

CaptureContext::PublishedPipelineState::PublishedPipelineState(IReplayController *controller)
{
  shaderMutationGeneration = controller->GetShaderReplacementGeneration(0);
  // Read and copy only on the replay thread, after SavePipelineState has finished. The driver is
  // free to rebuild its arrays and bindings during the next replay without changing this value.
  if(const D3D11Pipe::State *state = controller->GetD3D11PipelineState())
    d3d11.reset(new D3D11Pipe::State(*state));
  if(const D3D12Pipe::State *state = controller->GetD3D12PipelineState())
    d3d12.reset(new D3D12Pipe::State(*state));
  if(const GLPipe::State *state = controller->GetGLPipelineState())
    gl.reset(new GLPipe::State(*state));
  if(const VKPipe::State *state = controller->GetVulkanPipelineState())
    vulkan.reset(new VKPipe::State(*state));

  // API state copy constructors retain raw reflection pointers. The replay thread may free their
  // shader while the GUI still displays this publication, so detach all of them before returning.
  // Shared reflection (including D3D11's input-layout bytecode) is copied only once per snapshot.
  auto ownReflection = [this](const ShaderReflection *reflection) -> const ShaderReflection * {
    if(!reflection)
      return NULL;
    auto it = reflections.find(reflection);
    if(it == reflections.end())
      it = reflections.emplace(reflection,
                               std::unique_ptr<ShaderReflection>(new ShaderReflection(*reflection)))
               .first;
    return it->second.get();
  };

  if(d3d11)
  {
    d3d11->inputAssembly.bytecode = ownReflection(d3d11->inputAssembly.bytecode);
    for(D3D11Pipe::Shader *shader : {&d3d11->vertexShader, &d3d11->hullShader,
                                   &d3d11->domainShader, &d3d11->geometryShader,
                                   &d3d11->pixelShader, &d3d11->computeShader})
      shader->reflection = ownReflection(shader->reflection);
  }
  if(d3d12)
    for(D3D12Pipe::Shader *shader : {&d3d12->vertexShader, &d3d12->hullShader,
                                   &d3d12->domainShader, &d3d12->geometryShader,
                                   &d3d12->pixelShader, &d3d12->computeShader,
                                   &d3d12->ampShader, &d3d12->meshShader})
      shader->reflection = ownReflection(shader->reflection);
  if(gl)
    for(GLPipe::Shader *shader : {&gl->vertexShader, &gl->tessControlShader, &gl->tessEvalShader,
                                &gl->geometryShader, &gl->fragmentShader, &gl->computeShader})
      shader->reflection = ownReflection(shader->reflection);
  if(vulkan)
    for(VKPipe::Shader *shader : {&vulkan->vertexShader, &vulkan->tessControlShader,
                                &vulkan->tessEvalShader, &vulkan->geometryShader,
                                &vulkan->fragmentShader, &vulkan->computeShader,
                                &vulkan->taskShader, &vulkan->meshShader})
      shader->reflection = ownReflection(shader->reflection);

  pipeline.CopyStateAndRebind(controller->GetPipelineState(), d3d11.get(), d3d12.get(), gl.get(),
                              vulkan.get());
}

void CaptureContext::PublishPipelineState(std::shared_ptr<const PublishedPipelineState> state)
{
  // Publishing is a GUI operation. The EID and these pointers are updated in the same callback,
  // so all viewers continue to see the previous completed event while the next replay is running.
  m_PublishedPipelineState = std::move(state);
  m_CurD3D11PipelineState = m_PublishedPipelineState ? m_PublishedPipelineState->d3d11.get() : NULL;
  m_CurD3D12PipelineState = m_PublishedPipelineState ? m_PublishedPipelineState->d3d12.get() : NULL;
  m_CurGLPipelineState = m_PublishedPipelineState ? m_PublishedPipelineState->gl.get() : NULL;
  m_CurVulkanPipelineState = m_PublishedPipelineState ? m_PublishedPipelineState->vulkan.get() : NULL;
  m_CurPipelineState = m_PublishedPipelineState ? &m_PublishedPipelineState->pipeline
                                              : &m_DummyPipelineState;

  // i18n-ignore: diagnostic QObject property for validating publication boundaries, never shown.
  m_MainWindow->setProperty("pipelinePublicationSerial",
                           m_PublishedPipelineState
                               ? QVariant::fromValue<qulonglong>(++m_PipelinePublicationSerial)
                               : QVariant());
}

CaptureContext::CaptureContext(PersistentConfig &cfg) : m_Config(cfg)
{
  RENDERDOC_PROFILEFUNCTION();

  m_CaptureLoaded = false;
  m_LoadInProgress = false;

  RENDERDOC_RegisterMemoryRegion(this, sizeof(CaptureContext));

  memset(&m_APIProps, 0, sizeof(m_APIProps));

  m_CurD3D11PipelineState = NULL;
  m_CurD3D12PipelineState = NULL;
  m_CurGLPipelineState = NULL;
  m_CurVulkanPipelineState = NULL;
  m_CurPipelineState = &m_DummyPipelineState;

  m_Actions = &m_EmptyActions;

  m_StructuredFile = &m_DummySDFile;

  m_QtHelper = new MiniQtHelper(*this);

  qApp->setApplicationVersion(QString::fromLatin1(BREAD_FULL_VERSION_STRING));

  PythonContext::setCtxGlobal(*this);

  m_Icon = new QIcon();
  m_Icon->addFile(QStringLiteral(":/logo.svg"), QSize(), QIcon::Normal, QIcon::Off);

  m_MainWindow = new MainWindow(*this);

  m_MainWindow->LoadInitialLayout();

  m_Replay.SetFatalErrorCallback([this]() {
    GUIInvoke::call(m_MainWindow, [this]() {
      ResultDetails err = GetFatalError();

      // if we encountered a fatal error during shutdown just ignore it, there's not much point in
      // alerting the user and the recorded error will be gone already
      if(!m_Replay.IsRunning())
        return;

      RefreshUIStatus({}, true, true);

      QString title;
      QString text;

      if(err.code == ResultCode::RemoteServerConnectionLost)
      {
        QString serverName = Replay().CurrentRemote().Name();

        title = tr("Connection lost to %1").arg(serverName);
        text =
            tr("%1.\n\n"
               "This is most commonly caused by a crash, but that can't be verified and it may be "
               "a connection issue.")
                .arg(err.Message());
      }
      else if(err.code == ResultCode::DeviceLost)
      {
        title = tr("Device Lost error");
        text = tr("%1.\n\n"
                  "This may be due to an application bug, a RenderDoc bug, or insufficient "
                  "resources on the system to analyse the capture.\n\n"
                  "It is recommended that you run your application with API validation enabled, as "
                  "API usage errors can cause this kind of problem.")
                   .arg(err.Message());
      }
      else if(err.code == ResultCode::OutOfMemory)
      {
        title = tr("Out of memory");
        text =
            tr("%1.\n\n"
               "This is most likely caused by "
               "insufficient resources on the system to analyse the capture, which is possible if "
               "there is memory pressure from other applications or if the capture is heavyweight.")
                .arg(err.Message());
      }
      else
      {
        title = tr("Unrecoverable error");
        text = tr("While analysing the capture we encountered an unrecoverable error:\n\n%1")
                   .arg(err.Message());
      }

      text +=
          tr("\n\n"
             "This error is not recoverable and the analysis cannot continue. The UI will be "
             "non-functional until the capture is closed and reloaded.\n\n");

      bool add_report = false;

      if(m_MainWindow->ErrorReportsAllowed())
      {
        if(CrashDialog::HasCaptureReady(m_Config))
        {
          text +=
              tr("If you think this may be a RenderDoc bug please click the button below to report "
                 "it, "
                 "but note that this will require you to upload the capture for reproduction as "
                 "otherwise it is impossible to tell what the problem may be.");

          add_report = true;
        }
        else if(CrashDialog::CaptureTooLarge(m_Config))
        {
          text = tr("<html>%1<br><br>"
                    "Your capture is too large to upload as a crash report so this can't be "
                    "automatically reported. "
                    "Please email me at <a "
                    "href=\"mailto:baldurk@baldurk.org?subject=RenderDoc%20Unrecoverable%20error\">"
                    "baldurk@baldurk.org</a> with information and I can help investigate.</html>")
                     .arg(text);
        }
        else
        {
          text += tr("The capture must be saved locally if you want to report this as a bug. ");

          if(Replay().CurrentRemote().IsConnected())
          {
            text +=
                tr("Before closing the capture you can save it to disk and manually report a bug. "
                   "Please include the capture, or else it will be impossible to tell what the "
                   "problem may be.");
          }
          else
          {
            text += tr("You will need to reconnect to the remote server to save the capture.");
          }
        }
      }

      QMessageBox mb(QMessageBox::Critical, title, text.trimmed(), QMessageBox::Ok, m_MainWindow);
      mb.setDefaultButton(QMessageBox::NoButton);
      QPushButton *report = NULL;
      if(add_report)
        report = mb.addButton(tr("Report bug"), QMessageBox::AcceptRole);
      RDDialog::show(&mb);

      if(mb.clickedButton() == (QAbstractButton *)report)
      {
        m_MainWindow->sendErrorReport(true);
      }
    });
  });

  {
    QDir dir(ConfigFilePath("extensions"));

    if(!dir.exists())
      dir.mkpath(dir.absolutePath());
  }

  // ReplayLab is a first-party qrenderdoc integration. It is loaded from the fixed application
  // tree before optional user extensions, with no manifest scan, enable flag, or AppData fallback.
  m_ReplayLabIntegration = new ReplayLabIntegration(*this);
  const QString replayLabError = m_ReplayLabIntegration->Initialize();
  if(!replayLabError.isEmpty())
    qWarning() << tr("Built-in Bread integration is unavailable:") << replayLabError;

  m_ExtensionWatcher = new QFileSystemWatcher(GetMainWindow()->Widget());

  QObject::connect(m_ExtensionWatcher, &QFileSystemWatcher::directoryChanged,
                   [this](const QString &path) { ExtensionTouched(path); });
  QObject::connect(m_ExtensionWatcher, &QFileSystemWatcher::fileChanged,
                   [this](const QString &path) { ExtensionTouched(path); });

  rdcarray<ExtensionMetadata> exts = CaptureContext::GetInstalledExtensions();

  {
    QDir sentinelSearch(ConfigFilePath(QString()));

    bool questioned = false;

    // i18n-ignore: Filesystem pattern or sentinel filename for Python extensions.
    for(QString child : sentinelSearch.entryList(QStringList() << lit("python_load*.sentinel"),
                                                 QDir::Files | QDir::NoDotAndDotDot))
    {
      QFileInfo finfo(ConfigFilePath(child));

      // to avoid this being racey, we only care if we see a sentinel that's over 10 seconds old.
      // This means if two instances are starting up and both enter this section the second one
      // won't detect the temporary sentinel of the first as a crash
      if(finfo.exists() && finfo.lastModified().secsTo(QDateTime::currentDateTime()) > 10)
      {
        QFile f(ConfigFilePath(child));

        if(questioned)
        {
          f.remove();
          continue;
        }

        if(f.open(QIODevice::ReadOnly | QIODevice::Text))
        {
          QString extNames = QString::fromUtf8(f.readAll());

          QMessageBox::StandardButton res = RDDialog::question(
              NULL, tr("Possible python crash detected"),
              tr("A previous instance of RenderDoc crashed while loading python extensions.\n\n"
                 "These extensions were enabled:\n%1\n"
                 "Would you like to disable python extensions?")
                  .arg(extNames));

          questioned = true;

          if(res == QMessageBox::Yes)
          {
            cfg.AlwaysLoad_Extensions.clear();
            cfg.Save();
          }
        }

        f.remove();
      }
    }
  }

  QString sentinelFilename =
      // i18n-ignore: Filesystem pattern or sentinel filename for Python extensions.
      ConfigFilePath(lit("python_load%1.sentinel").arg(QCoreApplication::applicationPid()));

  // create a sentinel with the list of extensions we're loading
  {
    QFile f(sentinelFilename);

    if(f.open(QIODevice::WriteOnly | QIODevice::Text))
    {
      QTextStream stream(&f);

      for(rdcstr ext : cfg.AlwaysLoad_Extensions)
        stream << ext << lit("\n");
    }
  }

  for(const ExtensionMetadata &e : exts)
  {
    if(cfg.AlwaysLoad_Extensions.contains(e.package))
    {
      qInfo() << "Automatically loading extension" << QString(e.package);
      LoadExtension(e.package);
    }
  }

  // remove our sentinel now
  {
    QFile f(sentinelFilename);
    f.remove();
  }
}

CaptureContext::~CaptureContext()
{
  // Stop provider processes before the Python bridge, replay thread, or qrenderdoc context goes
  // away. The dock itself remains owned by the ToolWindowManager and is deleted with MainWindow.
  if(m_AIAssistant)
    m_AIAssistant->Shutdown();

  // Python integrations/extensions can own Qt timers, QRunnables, MiniQt panels and capture
  // viewers. Give each lifetime bucket a deterministic chance to quiesce while this context and
  // MiniQtHelper are still valid.
  bool integrationsStopped = PythonContext::ShutdownInternalIntegrations();
  if(!integrationsStopped)
  {
    qCritical() << "A built-in integration did not stop on the first shutdown attempt; retrying";
    integrationsStopped = PythonContext::ShutdownInternalIntegrations();
  }
  if(!integrationsStopped)
  {
    // Python integrations may still retain this CaptureContext, MiniQt objects, or replay work.
    // Continuing this destructor would turn those retained references into use-after-free. This
    // is an exit-only fail-safe after two bounded cancellation attempts, so terminate before any
    // native dependency below is dismantled.
    qFatal("Refusing to destroy CaptureContext while a built-in integration still owns live workers");
  }
  PythonContext::ShutdownExtensions();

  // Registered menu/context callbacks retain Python callables. Destroy those
  // wrappers under the GIL before the interpreter is finalised.
  PythonContext::ProcessExtensionWork([this]() {
    // ShaderCodeEditor callbacks can retain Python workspace controllers. Release the native
    // editor and those callbacks while the interpreter/GIL are still available. Invalidate the
    // borrowed QWidget wrappers before the authoritative native cleanup.
    while(!m_ShaderCodeEditors.isEmpty())
    {
      ShaderCodeEditor *editor = m_ShaderCodeEditors.takeLast();
      if(editor)
      {
        editor->ClearCallbacks();
        PythonContext::InvalidateNativeQtObject(editor);
        delete editor;
      }
    }
    delete m_ShaderCodeEditorService;
    m_ShaderCodeEditorService = NULL;

    for(QObject *object : m_ReplayLabIntegrationObjects)
      delete object;
    m_ReplayLabIntegrationObjects.clear();

    for(const QList<QObject *> &objects : m_ExtensionObjects)
      for(QObject *object : objects)
        delete object;
    m_ExtensionObjects.clear();

    for(QObject *object : m_PendingExtensionObjects)
      delete object;
    m_PendingExtensionObjects.clear();
    m_RegisteredMenuItems.clear();
  });

  // MiniQt/PySide shutdown uses deleteLater in a few paths. Drain those native
  // objects now, not after Py_Finalize when their Python callbacks are invalid.
  QCoreApplication::sendPostedEvents(NULL, QEvent::DeferredDelete);
  delete m_ExtensionWatcher;

  delete m_QtHelper;
  m_QtHelper = NULL;
  delete m_ReplayLabIntegration;
  m_ReplayLabIntegration = NULL;
  QCoreApplication::sendPostedEvents(NULL, QEvent::DeferredDelete);
  RENDERDOC_UnregisterMemoryRegion(this);
  delete m_Icon;
  m_Replay.CloseThread();
  delete m_MainWindow;
}

void CaptureContext::Begin(QString paramFilename, QString remoteHost, uint32_t remoteIdent,
                           bool temp, QString scriptFilename)
{
  m_MainWindow->show();

  if(remoteIdent != 0)
  {
    m_MainWindow->ShowLiveCapture(
        new LiveCapture(*this, remoteHost, remoteHost, remoteIdent, m_MainWindow, m_MainWindow));
  }

  if(!paramFilename.isEmpty())
  {
    QFileInfo checkFile(paramFilename);

    if(checkFile.exists() && checkFile.isFile())
    {
      m_MainWindow->LoadFromFilename(paramFilename, temp);
      if(temp)
        m_MainWindow->takeCaptureOwnership();
    }
  }

  if(!scriptFilename.isEmpty())
  {
    ShowPythonShell();

    if(GetPythonShell()->LoadScriptFromFilename(scriptFilename))
      GetPythonShell()->RunScript();
  }
}

bool CaptureContext::isRunning()
{
  return m_MainWindow && m_MainWindow->isVisible();
}

rdcstr CaptureContext::TempCaptureFilename(const rdcstr &appname)
{
  QString folder = Config().TemporaryCaptureDirectory;

  QDir dir(folder);

  if(folder.isEmpty() || !dir.exists())
  {
    dir = QDir(QDir::tempPath());

    dir.mkdir(lit("RenderDoc"));

    dir = QDir(dir.absoluteFilePath(lit("RenderDoc")));
  }

  return dir.absoluteFilePath(
      QFormatStr("%1_%2.rdc")
          .arg(appname)
          .arg(QDateTime::currentDateTimeUtc().toString(lit("yyyy.MM.dd_HH.mm.ss"))));
}

rdcarray<ExtensionMetadata> CaptureContext::GetInstalledExtensions()
{
  rdcarray<ExtensionMetadata> ret;
  QSet<QString> discoveredPackages;

  for(QString extensionFolder : PythonContext::GetApplicationExtensionsPaths())
  {
    QDirIterator it(extensionFolder, QDirIterator::Subdirectories | QDirIterator::FollowSymlinks);

    while(it.hasNext())
    {
      QFileInfo fileinfo(it.next());

      if(fileinfo.fileName().toLower() == lit("extension.json"))
      {
        QFile f(fileinfo.absoluteFilePath());

        QString package = fileinfo.absolutePath()
                              .replace(extensionFolder, QString())
                              .replace(QLatin1Char('/'), QLatin1Char('.'));

        while(!package.isEmpty() && package[0] == QLatin1Char('.'))
          package.remove(0, 1);

        while(!package.isEmpty() && package[package.size() - 1] == QLatin1Char('.'))
          package.remove(package.size() - 1, 1);

        if(package.isEmpty())
          continue;

        if(f.exists() && f.open(QIODevice::ReadOnly | QIODevice::Text))
        {
          QVariantMap json = JSONToVariant(QString::fromUtf8(f.readAll()));

          if(json.empty())
          {
            qCritical() << fileinfo.absoluteFilePath() << "is corrupt, cannot parse json";
            continue;
          }

          ExtensionMetadata ext;

          ext.package = package;
          ext.filePath = fileinfo.absolutePath();

          ext.hasChanges = m_DirtyExtensions.contains(rdcstr(package));
          ext.failedLoad = m_FailedExtensions.contains(rdcstr(package));

          ext.extensionAPI = 1;
          if(json.contains(lit("extension_api")))
          {
            ext.extensionAPI = json[lit("extension_api")].toInt();
          }
          else
          {
            qCritical() << "Extension" << QString(ext.name) << "is corrupt, no api version entry";
            continue;
          }

          if(json.contains(lit("version")))
          {
            ext.version = json[lit("version")].toString();
          }
          else
          {
            ext.version = lit("1.0");
          }

          if(json.contains(lit("name")))
          {
            ext.name = json[lit("name")].toString();
          }
          else
          {
            ext.name = package;
          }

          if(json.contains(lit("description")))
          {
            ext.description = json[lit("description")].toString();
          }
          else
          {
            ext.description = tr("No description provided");
          }

          if(json.contains(lit("author")))
          {
            ext.author = json[lit("author")].toString();
          }
          else
          {
            ext.author = tr("Unknown Author");
          }

          if(json.contains(lit("url")))
          {
            ext.extensionURL = json[lit("url")].toString();
          }
          else
          {
            ext.extensionURL = rdcstr();
          }

          if(json.contains(lit("minimum_renderdoc")))
          {
            QString minVer = json[lit("minimum_renderdoc")].toString();

            QRegularExpression re(lit("([0-9]*).([0-9]*)"));
            QRegularExpressionMatch match = re.match(minVer);

            bool ok = false, badversion = false;

            if(match.hasMatch())
            {
              int major = match.captured(1).toInt(&ok);

              if(ok)
              {
                int minor = match.captured(2).toInt(&ok);

                // if it needs a higher major version, we can't load it
                if(major > RENDERDOC_VERSION_MAJOR)
                  badversion = true;

                // if major versions are the same and it needs a higher minor, we can't load it
                // either
                if(major == RENDERDOC_VERSION_MAJOR && minor > RENDERDOC_VERSION_MINOR)
                  badversion = true;
              }
            }

            if(!ok)
            {
              qCritical() << "Extension" << QString(ext.name)
                          << "is corrupt, minimum_renderdoc doesn't match a MAJOR.MINOR version";
              continue;
            }

            if(badversion)
            {
              qInfo() << "Extension" << QString(ext.name) << "declares minimum_renderdoc" << minVer
                      << "so skipping";
              continue;
            }
          }

          // Application extension roots are enumerated before per-user roots. Keep the first valid
          // package so the same optional extension cannot appear or load twice. If the application
          // manifest is invalid, allow a valid per-user fallback to be discovered below.
          if(discoveredPackages.contains(package))
            continue;

          discoveredPackages.insert(package);
          ret.push_back(ext);
        }
      }
    }
  }

  return ret;
}

rdcarray<rdcstr> CaptureContext::GetLoadedExtensions()
{
  return m_ExtensionObjects.keys();
}

bool CaptureContext::IsExtensionLoaded(rdcstr name)
{
  return m_ExtensionObjects.contains(name);
}

bool CaptureContext::IsPythonDebuggerConnected()
{
  return PythonContext::IsDebuggingEnabled() && PythonContext::IsDebuggerConnected();
}

rdcstr CaptureContext::LoadExtension(rdcstr name)
{
  QString ret;

  PythonContext::ProcessExtensionWork([this, &ret, name]() {
    for(QObject *o : m_ExtensionObjects[name])
      delete o;

    m_ExtensionObjects[name].clear();

    ret = PythonContext::LoadExtension(*this, name);

    if(ret.isEmpty())
    {
      m_ExtensionObjects[name].swap(m_PendingExtensionObjects);

      m_DirtyExtensions.removeOne(name);
      m_FailedExtensions.removeOne(name);

      for(const ExtensionMetadata &e : GetInstalledExtensions())
        if(e.package == name)
          AddExtensionWatches(e.filePath);
    }
    else
    {
      m_ExtensionObjects.remove(name);
      m_FailedExtensions.push_back(name);

      for(QObject *o : m_PendingExtensionObjects)
        delete o;

      m_PendingExtensionObjects.clear();
    }
  });

  for(QAction *a : m_MainWindow->GetMenuActions())
    CleanMenu(a);

  m_RegisteredMenuItems.removeAll(NULL);

  return ret;
}

void CaptureContext::RegisterWindowMenu(WindowMenu base, const rdcarray<rdcstr> &submenus,
                                        ExtensionCallback callback)
{
  if(base == WindowMenu::Unknown)
  {
    qCritical() << "Can't register menu for WindowMenu::Unknown";
    return;
  }

  if(submenus.isEmpty())
  {
    qCritical() << "Empty list of submenus";
    return;
  }

  if(base == WindowMenu::NewMenu && submenus.count() == 1)
  {
    qCritical() << "Need at least two items in submenus for WindowMenu::NewMenu";
    return;
  }

  const bool showExtensionIcon = !m_RegisteringFirstPartyIntegration;
  QMenu *menu = m_MainWindow->GetBaseMenu(base, submenus[0], showExtensionIcon);

  if(!menu)
  {
    qCritical() << "Couldn't get base menu";
    return;
  }

  std::function<void()> slotcallback = [callback]() {
    callback(PythonContext::GetExtensionPyrenderdoc(), {});
  };

  // if it's a new menu, GetBaseMenu already did the work, so skip the 0th element of submenus
  if(base == WindowMenu::NewMenu)
  {
    rdcarray<rdcstr> items = submenus;
    items.erase(0);
    AddSortedMenuItem(menu, false, showExtensionIcon, items, slotcallback);
  }
  else
  {
    AddSortedMenuItem(menu, false, showExtensionIcon, submenus, slotcallback);
  }
}

void CaptureContext::RegisterPanelMenu(PanelMenu base, const rdcarray<rdcstr> &submenus,
                                       ExtensionCallback callback)
{
  if(base == PanelMenu::Unknown)
  {
    qCritical() << "Can't register menu for PanelMenu::Unknown";
    return;
  }

  RegisteredMenuItem *item = new RegisteredMenuItem();
  item->panel = base;
  item->submenus = submenus;
  item->callback = callback;
  item->showExtensionIcon = !m_RegisteringFirstPartyIntegration;
  m_RegisteredMenuItems.push_back(item);
  m_PendingExtensionObjects.push_back(item);
}

void CaptureContext::RegisterContextMenu(ContextMenu base, const rdcarray<rdcstr> &submenus,
                                         ExtensionCallback callback)
{
  if(base == ContextMenu::Unknown)
  {
    qCritical() << "Can't register menu for ContextMenu::Unknown";
    return;
  }

  RegisteredMenuItem *item = new RegisteredMenuItem();
  item->context = base;
  item->submenus = submenus;
  item->callback = callback;
  item->showExtensionIcon = !m_RegisteringFirstPartyIntegration;
  m_RegisteredMenuItems.push_back(item);
  m_PendingExtensionObjects.push_back(item);
}

void CaptureContext::MenuDisplaying(ContextMenu contextMenu, QMenu *menu,
                                    const ExtensionCallbackData &data)
{
  ContextMenu contextMenuAlt = contextMenu;

  switch(contextMenu)
  {
    case ContextMenu::MeshPreview_GSOutVertex:
    case ContextMenu::MeshPreview_VSInVertex:
    case ContextMenu::MeshPreview_VSOutVertex:
      contextMenuAlt = ContextMenu::MeshPreview_Vertex;
      break;
    case ContextMenu::TextureViewer_InputThumbnail:
    case ContextMenu::TextureViewer_OutputThumbnail:
      contextMenuAlt = ContextMenu::TextureViewer_Thumbnail;
      break;
    default: break;
  }

  for(RegisteredMenuItem *item : m_RegisteredMenuItems)
  {
    if(item->context == contextMenu || item->context == contextMenuAlt)
    {
      AddSortedMenuItem(menu, true, item->showExtensionIcon, item->submenus, [item, data]() {
        rdcarray<rdcpair<rdcstr, PyObject *>> args;

        PythonContext::ConvertPyArgs(data, args);

        item->callback(PythonContext::GetExtensionPyrenderdoc(), args);

        PythonContext::FreePyArgs(args);
      });
    }
  }
}

void CaptureContext::MenuDisplaying(PanelMenu panelMenu, QMenu *menu, QWidget *extensionButton,
                                    const ExtensionCallbackData &data)
{
  for(RegisteredMenuItem *item : m_RegisteredMenuItems)
  {
    if(item->panel == panelMenu)
    {
      AddSortedMenuItem(menu, false, item->showExtensionIcon, item->submenus, [item, data]() {
        rdcarray<rdcpair<rdcstr, PyObject *>> args;

        PythonContext::ConvertPyArgs(data, args);

        item->callback(PythonContext::GetExtensionPyrenderdoc(), args);

        PythonContext::FreePyArgs(args);
      });
    }
  }

  QAction *emptyAction = new QAction(tr("No extension commands configured"), menu);

  if(menu->isEmpty())
  {
    menu->addAction(emptyAction);
    QObject::connect(emptyAction, &QAction::triggered,
                     [this]() { m_MainWindow->showExtensionManager(); });
  }
}

IMiniQtHelper &CaptureContext::GetMiniQtHelper()
{
  return *m_QtHelper;
}

void CaptureContext::MessageDialog(const rdcstr &text, const rdcstr &title)
{
  RDDialog::information(m_MainWindow, title.isEmpty() ? "Python Extension Message" : title, text);
}

void CaptureContext::ErrorDialog(const rdcstr &text, const rdcstr &title)
{
  RDDialog::critical(m_MainWindow, title.isEmpty() ? "Python Extension Error" : title, text);
}

DialogButton CaptureContext::QuestionDialog(const rdcstr &text, const rdcarray<DialogButton> &options,
                                            const rdcstr &title)
{
  QMessageBox::StandardButtons buttons;
  for(DialogButton b : options)
    buttons |= (QMessageBox::StandardButton)b;
  return (DialogButton)RDDialog::question(
      m_MainWindow, title.isEmpty() ? "Python Extension Prompt" : title, text, buttons);
}

rdcstr CaptureContext::OpenFileName(const rdcstr &caption, const rdcstr &dir, const rdcstr &filter)
{
  return RDDialog::getOpenFileName(m_MainWindow, caption.isEmpty() ? "Open a file" : caption, dir,
                                   filter);
}

rdcstr CaptureContext::OpenDirectoryName(const rdcstr &caption, const rdcstr &dir)
{
  return RDDialog::getExistingDirectory(m_MainWindow,
                                        caption.isEmpty() ? "Open a directory" : caption, dir);
}

rdcstr CaptureContext::SaveFileName(const rdcstr &caption, const rdcstr &dir, const rdcstr &filter)
{
  return RDDialog::getSaveFileName(m_MainWindow, caption.isEmpty() ? "Save a file" : caption, dir,
                                   filter);
}

void CaptureContext::AddSortedMenuItem(QMenu *menu, bool rootMenu, bool showExtensionIcon,
                                       const rdcarray<rdcstr> &items,
                                       std::function<void()> callback)
{
  QAction *beforeAction = NULL;
  bool addIcon = rootMenu;

  // if we're inserting into a pre-existing menu, try to find an __examples__ action which tells us
  // where
  // to be inserting
  {
    QList<QAction *> children = menu->actions();
    int idx = 0;
    for(; idx < children.count(); idx++)
    {
      if(children[idx]->text() == lit("__extensions__"))
        break;
    }

    // if we found it, set beforeAction
    if(idx < children.count())
    {
      // search back to find the right place alphabetically to place this, if there are existing
      // menus

      for(; idx > 0;)
      {
        // if the previous child is a separator, break, there are no more previous menus
        if(children[idx - 1]->isSeparator())
          break;

        // if we should be sorted before the previous child, move backwards
        if(children[idx - 1]->text() > items[0])
        {
          idx--;
          continue;
        }

        // if we found the right place, break
        break;
      }

      beforeAction = children[idx];
      addIcon = true;
    }
  }

  for(int i = 0; i < items.count() - 1; i++)
  {
    QList<QAction *> children = menu->actions();

    bool found = false;

    // see if this submenu already exists, if so just iterate down to it
    for(QAction *a : children)
    {
      if(a->text() == items[i])
      {
        menu = a->menu();
        found = true;
        break;
      }
    }

    if(found)
    {
      addIcon = false;
      beforeAction = NULL;
      continue;
    }

    // create the new submenu
    QMenu *submenu = new QMenu(items[i], m_MainWindow);

    // if we don't have a beforeAction, find where to insert this to keep alphabetical order.
    if(!beforeAction)
    {
      for(QAction *a : children)
      {
        if(submenu->menuAction()->text() < a->text())
        {
          beforeAction = a;
          break;
        }
      }
    }

    if(beforeAction)
      menu->insertMenu(beforeAction, submenu);
    else
      menu->addMenu(submenu);

    if(addIcon && showExtensionIcon)
      submenu->setIcon(Icons::plugin());

    addIcon = false;

    beforeAction = NULL;

    menu = submenu;
  }

  QAction *action = new QAction(m_MainWindow);

  action->setText(items.back());

  QObject::connect(action, &QAction::triggered, callback);

  if(!beforeAction)
  {
    QList<QAction *> children = menu->actions();

    for(QAction *a : children)
    {
      if(action->text() < a->text())
      {
        beforeAction = a;
        break;
      }
    }
  }

  if(beforeAction)
    menu->insertAction(beforeAction, action);
  else
    menu->addAction(action);

  if(addIcon && showExtensionIcon)
    action->setIcon(Icons::plugin());

  m_PendingExtensionObjects.push_back(action);
}

void CaptureContext::CleanMenu(QAction *action)
{
  QMenu *menu = action->menu();

  // if this isn't a menu, nothing to do - return
  if(!menu)
    return;

  // first clean all our children
  for(QAction *a : menu->actions())
    CleanMenu(a);

  // if we no longer have any children in this menu, delete it.
  if(menu->actions().isEmpty())
    delete menu;
}

void CaptureContext::LoadCapture(const rdcstr &captureFile, const ReplayOptions &opts,
                                 const rdcstr &origFilename, bool temporary, bool local)
{
  LoadCaptureInternal(captureFile, opts, origFilename, temporary, local, true);
}

ResultDetails CaptureContext::LoadCaptureForReplayRecovery(const rdcstr &captureFile,
                                                           const ReplayOptions &opts,
                                                           const rdcstr &origFilename)
{
  LoadCaptureInternal(captureFile, opts, origFilename, false, true, false);
  return m_Replay.GetCreateStatus();
}

void CaptureContext::LoadCaptureInternal(const rdcstr &captureFile, const ReplayOptions &opts,
                                         const rdcstr &origFilename, bool temporary, bool local,
                                         bool showErrorDialog)
{
  RENDERDOC_PROFILEFUNCTION();

  CloseCapture();

  PointerTypeRegistry::Init();

  BufferInterpreter::context = this;

  m_LoadInProgress = true;
  SetLoadCaptureStage(LoadCaptureStage::OpeningReplay);

  if(local)
    m_Config.CrashReport_LastOpenedCapture = origFilename;
  else
    m_Config.CrashReport_LastOpenedCapture = QString();

  m_Config.Save();

  bool newCapture = (!temporary && !Config().RecentCaptureFiles.contains(origFilename));

  LambdaThread *thread =
      new LambdaThread([this, captureFile, opts, origFilename, temporary, local, showErrorDialog]() {
        LoadCaptureThreaded(captureFile, opts, origFilename, temporary, local, showErrorDialog);
      });
  thread->setName(lit("LoadCapture"));
  thread->selfDelete(true);
  thread->start();

  QElapsedTimer loadTimer;
  loadTimer.start();

  ShowProgressDialogWithStatus(
      m_MainWindow, tr("Loading Capture: %1").arg(QFileInfo(origFilename).fileName()),
      [this]() { return !m_LoadInProgress; }, [this]() { return UpdateLoadProgress(); },
      [this]() { return CurrentLoadStatusText(); });

  if(!m_CaptureLoaded)
  {
    m_MainWindow->setProgress(-1.0f);
    return;
  }

  if(local)
  {
    m_Watcher = new QFileSystemWatcher({captureFile}, GetMainWindow()->Widget());

    QObject::connect(m_Watcher, &QFileSystemWatcher::fileChanged, [this]() {
      ++m_IdleCheckpointGeneration;
      const uint32_t selectedEventId = m_SelectedEventID;
      const uint32_t eventId = m_EventID;
      const uint64_t epoch = m_ScrubEpoch.load();
      Replay().AsyncInvoke([this, selectedEventId, eventId, epoch](IReplayController *r) {
        if(epoch != m_ScrubEpoch.load())
          return;
        r->FileChanged();
        r->SetSelectedEventID(selectedEventId);
        r->SetFrameEvent(eventId, true);
        const std::shared_ptr<const PublishedPipelineState> pipeline =
            std::make_shared<PublishedPipelineState>(r);
        GUIInvoke::call(GetMainWindow()->Widget(), [this, selectedEventId, eventId, epoch, pipeline]() {
          if(epoch != m_ScrubEpoch.load() || !IsCaptureLoaded() || m_EventID != eventId ||
             m_SelectedEventID != selectedEventId)
            return;
          PublishPipelineState(pipeline);
          RefreshUIStatus({}, true, true);
          ScheduleIdleCheckpointMaintenance();
        });
      });
    });
  }

#if defined(RELEASE)
  ANALYTIC_ADDAVG(Performance.LoadTime, double(loadTimer.nsecsElapsed() * 1.0e-9));
#endif

  if(m_APIProps.ShaderLinkage)
    ANALYTIC_SET(CaptureFeatures.ShaderLinkage, true);
  if(m_APIProps.YUVTextures)
    ANALYTIC_SET(CaptureFeatures.YUVTextures, true);
  if(m_APIProps.SparseResources)
    ANALYTIC_SET(CaptureFeatures.SparseResources, true);
  if(m_APIProps.MultiGPU)
    ANALYTIC_SET(CaptureFeatures.MultiGPU, true);
  if(m_APIProps.D3D12Bundle)
    ANALYTIC_SET(CaptureFeatures.D3D12Bundle, true);
  if(m_APIProps.DXILShaders)
    ANALYTIC_SET(CaptureFeatures.DXILShaders, true);

  if(m_APIProps.vendor != GPUVendor::Unknown)
  {
    ANALYTIC_ADDUNIQ(GPUVendors, ToQStr(m_APIProps.vendor));
  }

  m_MainWindow->setProgress(-1.0f);

  if(m_CaptureLoaded)
  {
    m_CaptureTemporary = temporary;

    BufferFormatter::Init(m_APIProps.pipelineType);

    m_CaptureMods = CaptureModifications::NoModifications;

    rdcarray<ICaptureViewer *> viewers(m_CaptureViewers);

    // make sure we're on a consistent event before invoking viewer forms
    if(m_LastAction)
      SetEventID(viewers, m_LastAction->eventId, m_LastAction->eventId, true);
    else if(!m_Actions->empty())
      SetEventID(viewers, m_Actions->back().eventId, m_Actions->back().eventId, true);

    GUIInvoke::blockcall(m_MainWindow, [&viewers]() {
      // notify all the registers viewers that a capture has been loaded
      for(ICaptureViewer *viewer : viewers)
      {
        if(viewer)
          viewer->OnCaptureLoaded();
      }
    });

    if(newCapture && m_Notes.contains(lit("comments")) && m_Config.Comments_ShowOnLoad)
    {
      if(!HasCommentView())
        ShowCommentView();
      RaiseDockWindow(GetCommentView()->Widget());
    }

    // refresh the UI without forcing the replay
    SetEventID({}, m_SelectedEventID, m_EventID, false);
  }
}

float CaptureContext::UpdateLoadProgress()
{
  float val = 0.8f * m_LoadProgress + 0.19f * m_PostloadProgress + 0.01f;

  m_MainWindow->setProgress(val);

  return val;
}

QString CaptureContext::CurrentLoadStatusText() const
{
  const LoadCaptureStage stage =
      LoadCaptureStage(m_LoadCaptureStage.load(std::memory_order_relaxed));

  switch(stage)
  {
    case LoadCaptureStage::OpeningReplay:
      return tr("Loading the RDC and reconstructing replay resources...");
    case LoadCaptureStage::ReadingFrameMetadata: return tr("Reading frame and API metadata...");
    case LoadCaptureStage::BuildingActionList: return tr("Building the frame event list...");
    case LoadCaptureStage::CachingResources:
      return tr("Caching resources, buffers, and descriptor stores...");
    case LoadCaptureStage::ReadingTextures: return tr("Reading texture metadata...");
    case LoadCaptureStage::ReadingPipelineState:
      return tr("Reading the initial pipeline state...");
    case LoadCaptureStage::ReadingEmbeddedData:
      return tr("Reading embedded capture data and saved edits...");
    case LoadCaptureStage::Finalising: return tr("Finalising capture initialisation...");
  }

  return tr("Loading capture...");
}

void CaptureContext::LoadCaptureThreaded(const QString &captureFile, const ReplayOptions &opts,
                                         const QString &origFilename, bool temporary, bool local,
                                         bool showErrorDialog)
{
  m_CaptureFile = origFilename;
  m_RemoteFile = captureFile;

  m_CaptureLocal = local;

  Config().Save();

  m_LoadProgress = 0.0f;
  m_PostloadProgress = 0.0f;
  SetLoadCaptureStage(LoadCaptureStage::OpeningReplay);

  // this function call will block until the capture is either loaded, or there's some failure
  m_Replay.OpenCapture(captureFile, opts, [this](float p) { m_LoadProgress = p; });

  QString filename = QFileInfo(origFilename).fileName();

  // if the renderer isn't running, we hit a failure case so display an error message
  if(!m_Replay.IsRunning())
  {
    if(showErrorDialog)
      RDDialog::critical(NULL, tr("Error opening capture"),
                         tr("Failed to open '%1' for replay\n\n%2")
                             .arg(filename)
                             .arg(m_Replay.GetCreateStatus().Message()));

    m_LoadInProgress = false;
    return;
  }

  if(!temporary)
  {
    AddRecentFile(Config().RecentCaptureFiles, origFilename);

    Config().Save();
  }

  m_EventID = 0;

  m_FirstAction = m_LastAction = NULL;

  // fetch initial data like actions, textures and buffers
  std::shared_ptr<const PublishedPipelineState> initialPipeline;
  m_Replay.BlockInvoke([this, &initialPipeline](IReplayController *r) {
    SetLoadCaptureStage(LoadCaptureStage::ReadingFrameMetadata);

    if(Config().EventBrowser_AddFake)
      r->AddFakeMarkers();

    m_FrameInfo = r->GetFrameInfo();

    m_APIProps = r->GetAPIProperties();

    m_CustomEncodings = r->GetCustomShaderEncodings();
    m_CustomPrefixes = r->GetCustomShaderSourcePrefixes();
    m_TargetEncodings = r->GetTargetShaderEncodings();

    m_PostloadProgress = 0.2f;

    SetLoadCaptureStage(LoadCaptureStage::BuildingActionList);

    m_Actions = &r->GetRootActions();

    m_FirstAction = &m_Actions->at(0);
    while(!m_FirstAction->children.empty())
      m_FirstAction = &m_FirstAction->children[0];

    m_LastAction = &m_Actions->back();
    while(!m_LastAction->children.empty())
      m_LastAction = &m_LastAction->children.back();

    m_PostloadProgress = 0.4f;

    SetLoadCaptureStage(LoadCaptureStage::CachingResources);

    m_WinSystems = r->GetSupportedWindowSystems();

#if defined(RENDERDOC_PLATFORM_WIN32)
    m_CurWinSystem = WindowingSystem::Win32;
#elif defined(RENDERDOC_PLATFORM_LINUX)
    m_CurWinSystem = WindowingSystem::Unknown;

    if(QGuiApplication::platformName() == lit("wayland"))
    {
      // if we're using Wayland we must use wayland since we can't get any other surface type
      for(WindowingSystem sys : m_WinSystems)
      {
        if(sys == WindowingSystem::Wayland)
        {
          m_CurWinSystem = WindowingSystem::Wayland;
          m_WaylandDisplay = (wl_display *)AccessWaylandPlatformInterface("display", NULL);
          break;
        }
      }

      if(m_CurWinSystem == WindowingSystem::Unknown)
      {
        RDDialog::critical(NULL, tr("No wayland support"),
                           tr("Replay doesn't support Wayland surfaces - check you compiled this "
                              "build of RenderDoc with Wayland support enabled."));
      }
    }
    else
    {
      m_CurWinSystem = WindowingSystem::Xlib;

      // prefer XCB, if supported
      for(WindowingSystem sys : m_WinSystems)
      {
        if(sys == WindowingSystem::XCB)
        {
          m_CurWinSystem = WindowingSystem::XCB;
          break;
        }
      }

      if(m_CurWinSystem == WindowingSystem::XCB)
        m_XCBConnection = QX11Info::connection();
      else
        m_X11Display = QX11Info::display();
    }

#elif defined(RENDERDOC_PLATFORM_APPLE)
    m_CurWinSystem = WindowingSystem::MacOS;
#endif

    m_StructuredFile = &r->GetStructuredFile();

    m_ResourceList = r->GetResources();

    CacheResources();

    m_BufferList = r->GetBuffers();
    for(BufferDescription &b : m_BufferList)
      m_Buffers[b.resourceId] = &b;

    m_DescriptorStoreList = r->GetDescriptorStores();
    for(DescriptorStoreDescription &d : m_DescriptorStoreList)
      m_DescriptorStores[d.resourceId] = &d;

    m_PostloadProgress = 0.8f;

    SetLoadCaptureStage(LoadCaptureStage::ReadingTextures);

    m_TextureList = r->GetTextures();
    for(TextureDescription &t : m_TextureList)
      m_Textures[t.resourceId] = &t;

    m_PostloadProgress = 0.9f;

    SetLoadCaptureStage(LoadCaptureStage::ReadingPipelineState);

    initialPipeline = std::make_shared<PublishedPipelineState>(r);

    m_UnreadMessageCount = 0;
    AddMessages(m_FrameInfo.debugMessages);

    m_PostloadProgress = 1.0f;
  });

  GUIInvoke::blockcall(m_MainWindow, [this, initialPipeline]() {
    PublishPipelineState(initialPipeline);
  });

  if(m_FrameInfo.containsAnnotations)
    ANALYTIC_SET(CaptureFeatures.CustomAnnotations, true);

  QThread::msleep(20);

  QDateTime today = QDateTime::currentDateTimeUtc();
  QDateTime compare = today.addDays(-21);

  if(compare > Config().DegradedCapture_LastUpdate && m_APIProps.degraded)
  {
    Config().DegradedCapture_LastUpdate = today;

    RDDialog::critical(
        NULL, tr("Degraded support of capture"),
        tr("%1\nThis capture opened with degraded support - "
           "this could mean missing hardware support caused a fallback to software rendering.\n\n"
           "This warning will not appear every time this happens, "
           "check debug errors/warnings window for more details.")
            .arg(origFilename));
  }

  ICaptureAccess *access = Replay().GetCaptureAccess();

  if(access)
  {
    SetLoadCaptureStage(LoadCaptureStage::ReadingEmbeddedData);

    int idx = access->FindSectionByType(SectionType::ResourceRenames);
    if(idx >= 0)
    {
      bytebuf buf = access->GetSectionContents(idx);
      LoadRenames(QString::fromUtf8((const char *)buf.data(), buf.count()));
    }

    idx = access->FindSectionByType(SectionType::Bookmarks);
    if(idx >= 0)
    {
      bytebuf buf = access->GetSectionContents(idx);
      LoadBookmarks(QString::fromUtf8((const char *)buf.data(), buf.count()));
    }

    idx = access->FindSectionByType(SectionType::Notes);
    if(idx >= 0)
    {
      bytebuf buf = access->GetSectionContents(idx);
      LoadNotes(QString::fromUtf8((const char *)buf.data(), buf.count()));
    }

    idx = access->FindSectionByType(SectionType::EditedShaders);
    if(idx >= 0)
    {
      bytebuf buf = access->GetSectionContents(idx);
      GUIInvoke::call(m_MainWindow, [this, buf]() {
        LoadEdits(QString::fromUtf8((const char *)buf.data(), buf.count()));
      });
    }

    QString driver = access->DriverName();
    if(driver == lit("Image"))
    {
      ANALYTIC_SET(UIFeatures.ImageViewer, true);
    }
    else if(!driver.isEmpty())
    {
      ANALYTIC_ADDUNIQ(APIs, driver);
    }
  }

  SetLoadCaptureStage(LoadCaptureStage::Finalising);
  m_LoadInProgress = false;
  m_CaptureLoaded = true;
}

void CaptureContext::CacheResources()
{
  m_CustomNameCachedID++;

  m_Resources.clear();

  std::sort(m_ResourceList.begin(), m_ResourceList.end(),
            [this](const ResourceDescription &a, const ResourceDescription &b) {
              return GetResourceNameUnsuffixed(&a) < GetResourceNameUnsuffixed(&b);
            });

  for(ResourceDescription &res : m_ResourceList)
    m_Resources[res.resourceId] = &res;
}

void CaptureContext::RecompressCapture()
{
  const QString previousCaptureFile = m_CaptureFile;
  QString destFilename = GetCaptureFilename();
  QString tempFilename;

  qint64 oldSize = QFile(destFilename).size();

  ICaptureFile *cap = NULL;
  ICaptureFile *tempCap = NULL;

  bool inplace = false;

  if(IsCaptureTemporary() || !IsCaptureLocal())
  {
    QMessageBox::StandardButton res =
        RDDialog::question(m_MainWindow, tr("Unsaved capture"),
                           tr("To recompress a capture you must save it first. Save this capture?"),
                           QMessageBox::Yes | QMessageBox::No | QMessageBox::Cancel);

    if(res == QMessageBox::Cancel || res == QMessageBox::No)
      return;

    destFilename = m_MainWindow->GetSavePath();

    // if it's already local, we'll do the save as part of the recompression convert. If it's
    // remote, we need to copy it first, but we copy it to a temporary so we can do the conversion
    // to the target location

    if(IsCaptureLocal())
    {
      tempFilename = GetCaptureFilename();
    }
    else
    {
      tempFilename = TempCaptureFilename(lit("recompress"));
      Replay().CopyCaptureFromRemote(GetCaptureFilename(), tempFilename, m_MainWindow);

      if(!QFile::exists(tempFilename))
      {
        RDDialog::critical(m_MainWindow, tr("Failed to save capture"),
                           tr("Capture couldn't be saved from remote."));
        return;
      }
    }
  }
  else
  {
    // if we're doing this inplace on an already saved capture, then we need to recompress to a
    // temporary and close/move it afterwards.
    inplace = true;
    destFilename = TempCaptureFilename(lit("recompress"));
  }

  if(IsCaptureLocal())
  {
    // for local files we already have a handle. We'll reuse it, then re-open
    cap = m_Replay.GetCaptureFile();
  }
  else
  {
    // for remote files we open a new short-lived handle on the temporary file
    tempCap = cap = RENDERDOC_OpenCaptureFile();
    cap->OpenFile(tempFilename, "rdc", NULL);
  }

  if(!cap)
  {
    RDDialog::critical(m_MainWindow, tr("Unexpected missing handle"),
                       tr("Couldn't get open handle to file for recompression."));
    return;
  }

  int index = cap->FindSectionByType(SectionType::FrameCapture);
  SectionProperties props = cap->GetSectionProperties(index);

  if(props.flags & SectionFlags::ZstdCompressed)
  {
    RDDialog::information(m_MainWindow, tr("Capture already compressed"),
                          tr("This capture is already compressed as much as is possible."));

    if(tempCap)
      tempCap->Shutdown();
    if(!tempFilename.isEmpty())
    {
      m_MainWindow->RemoveRecentCapture(tempFilename);
      QFile::remove(tempFilename);
    }
    return;
  }

  // convert from the currently open cap to the destination
  float progress = 0.0f;

  LambdaThread *th = new LambdaThread([cap, destFilename, &progress]() {
    cap->Convert(destFilename, "rdc", NULL, [&progress](float p) { progress = p; });
  });
  th->setName(lit("RecompressCapture"));
  th->start();
  // wait a few ms before popping up a progress bar
  th->wait(500);
  if(th->isRunning())
  {
    ShowProgressDialog(
        m_MainWindow, tr("Recompressing file."), [th]() { return !th->isRunning(); },
        [&progress]() { return progress; });
  }
  th->deleteLater();

  if(inplace)
  {
    // if we're recompressing "in place", we need to close our capture, move the temporary over
    // the original, then re-open.

    // this releases the hold over the real desired location.
    cap->OpenFile("", "", NULL);

    // now remove the old capture
    QFile::remove(GetCaptureFilename());

    // move the recompressed one over
    QFile::rename(destFilename, GetCaptureFilename());

    // and re-open
    cap->OpenFile(GetCaptureFilename(), "rdc", NULL);
  }
  else
  {
    // we've converted into the desired location. We don't have to do anything else but mark our
    // new locally saved non-temporary status.

    m_CaptureFile = destFilename;
    m_CaptureLocal = true;
    m_CaptureTemporary = false;

    // open the saved capture file. This will let us remove the old file too
    m_Replay.ReopenCaptureFile(m_CaptureFile);

    m_CaptureMods = CaptureModifications::All;

    SaveChanges();
  }

  if(m_AIAssistant && previousCaptureFile != m_CaptureFile)
    m_AIAssistant->RebindCaptureFile(m_CaptureFile);

  // close any temporary resources
  if(tempCap)
    tempCap->Shutdown();
  if(!tempFilename.isEmpty())
  {
    m_MainWindow->RemoveRecentCapture(tempFilename);
    QFile::remove(tempFilename);
  }

  qint64 newSize = QFile(m_CaptureFile).size();

  RDDialog::information(
      m_MainWindow, tr("Capture recompressed"),
      tr("Compression successful, capture is %1% of the original size: %2 MB -> %3 MB")
          .arg(double(newSize * 100) / double(oldSize), 0, 'f', 2)
          .arg(double(oldSize) / (1024.0 * 1024.0), 0, 'f', 1)
          .arg(double(newSize) / (1024.0 * 1024.0), 0, 'f', 1));
}

bool CaptureContext::SaveCaptureTo(const rdcstr &captureFile)
{
  bool success = false;
  QString error;
  const QString previousCaptureFile = m_CaptureFile;

  if(IsCaptureLocal())
  {
    if(QFileInfo(GetCaptureFilename()).exists())
    {
      if(GetCaptureFilename() == captureFile)
      {
        success = true;
      }
      else
      {
        ICaptureFile *capFile = m_Replay.GetCaptureFile();

        if(capFile)
        {
          // this will overwrite
          ResultDetails result = capFile->CopyFileTo(captureFile);
          success = result.OK();
          error = result.Message();
        }
        else
        {
          // QFile::copy won't overwrite, so remove the destination first (the save dialog already
          // prompted for overwrite)
          QFile::remove(captureFile);
          success = QFile::copy(GetCaptureFilename(), captureFile);
          error = tr("File move failed");
        }

        error = tr("Couldn't save to %1: %2").arg(captureFile).arg(error);
      }
    }
    else
    {
      error = tr("Capture '%1' couldn't be found on disk, cannot save.").arg(GetCaptureFilename());
      success = false;
      RDDialog::critical(NULL, tr("File not found"), error);
    }
  }
  else
  {
    Replay().CopyCaptureFromRemote(GetCaptureFilename(), captureFile, m_MainWindow);
    success = QFile::exists(captureFile);

    error = tr("File couldn't be transferred from remote host");
  }

  if(!success)
  {
    RDDialog::critical(NULL, tr("Error Saving"), error);
    return false;
  }

  // if it was a temporary capture, remove the old instnace
  if(m_CaptureTemporary)
  {
    m_MainWindow->RemoveRecentCapture(m_CaptureFile);
    QFile::remove(m_CaptureFile);
  }

  // Update the filename, and mark that it's local and not temporary now.
  m_CaptureFile = captureFile;
  m_CaptureLocal = true;
  m_CaptureTemporary = false;

  m_Replay.ReopenCaptureFile(captureFile);
  SaveChanges();

  if(m_AIAssistant && previousCaptureFile != m_CaptureFile)
    m_AIAssistant->RebindCaptureFile(m_CaptureFile);

  return true;
}

void CaptureContext::CloseCapture()
{
  if(!m_CaptureLoaded)
    return;

  // Cancel any in-progress interactive scrub so a stale settle can't fire against a closed capture.
  m_Scrubbing = false;
  m_ScrubCommitPending = false;
  m_ScrubInFlight = false;
  m_ScrubEpoch++;
  m_ScrubGeneration++;

  m_StatusRefreshDeferred = false;
  m_StatusRefreshForce = false;
  rdcarray<ICaptureViewer *> capviewers(m_CaptureViewers);

  for(ICaptureViewer *viewer : capviewers)
  {
    if(viewer && m_CaptureViewers.contains(viewer))
      viewer->OnCaptureClosed();
  }

  delete m_Watcher;
  m_Watcher = NULL;

  delete m_RGP;
  m_RGP = NULL;

  m_Config.CrashReport_LastOpenedCapture = QString();

  m_CaptureTemporary = false;

  m_CaptureFile = QString();
  m_RemoteFile = QString();

  m_APIProps = APIProperties();
  m_FrameInfo = FrameDescription();
  m_Buffers.clear();
  m_BufferList.clear();
  m_Textures.clear();
  m_TextureList.clear();
  m_Resources.clear();
  m_ResourceList.clear();
  m_OrigToReplacedResources.clear();
  m_ReplacedToOrigResources.clear();
  m_CBufferDebugDisplay.Clear();

  m_CustomNames.clear();
  m_Bookmarks.clear();
  m_Notes.clear();

  m_Actions = &m_EmptyActions;
  m_FirstAction = m_LastAction = NULL;

  PublishPipelineState({});

  m_StructuredFile = &m_DummySDFile;

  m_DebugMessages.clear();
  m_UnreadMessageCount = 0;

  m_CaptureLoaded = false;

  // Detach completions before calling clients. Reentrant publication requests now see the
  // capture as closed, and replay-queue disposal cannot leave a client waiting indefinitely.
  m_ReplayStatusPublications.Cancel();

  BufferInterpreter::context = NULL;

  m_Replay.CloseThread();
}

bool CaptureContext::ImportCapture(const CaptureFileFormat &fmt, const rdcstr &importfile,
                                   const rdcstr &rdcfile)
{
  CloseCapture();

  QString ext = fmt.extension;

  ResultDetails result;

  // shorten the filename after here for error messages
  QString filename = QFileInfo(importfile).fileName();

  float progress = 0.0f;

  LambdaThread *th = new LambdaThread([rdcfile, importfile, ext, &progress, &result]() {
    ICaptureFile *file = RENDERDOC_OpenCaptureFile();

    result = file->OpenFile(importfile, ext.toUtf8().data(),
                            [&progress](float p) { progress = p * 0.5f; });

    if(result.code != ResultCode::Succeeded)
    {
      file->Shutdown();
      return;
    }

    result =
        file->Convert(rdcfile, "rdc", NULL, [&progress](float p) { progress = 0.5f + p * 0.5f; });
    file->Shutdown();
  });
  th->setName(lit("ImportCapture"));
  th->start();
  // wait a few ms before popping up a progress bar
  th->wait(500);
  if(th->isRunning())
  {
    ShowProgressDialog(
        m_MainWindow, tr("Importing from %1, please wait...").arg(filename),
        [th]() { return !th->isRunning(); }, [&progress]() { return progress; });
  }
  th->deleteLater();

  if(!result.OK())
  {
    RDDialog::critical(m_MainWindow, tr("Error converting capture"),
                       tr("Couldn't convert file '%1'\n%2").arg(filename).arg(result.Message()));
    return false;
  }

  return true;
}

void CaptureContext::ExportCapture(const CaptureFileFormat &fmt, const rdcstr &exportfile)
{
  if(!m_CaptureLocal)
    return;

  QString ext = fmt.extension;

  ICaptureFile *local = NULL;
  ICaptureFile *file = NULL;
  ResultDetails result = {ResultCode::Succeeded};

  const SDFile *sdfile = NULL;

  // if we don't need buffers, we can export directly from our existing capture file
  if(!fmt.requiresBuffers)
  {
    file = m_Replay.GetCaptureFile();
    sdfile = m_StructuredFile;
  }

  if(!file)
  {
    local = file = RENDERDOC_OpenCaptureFile();
    result = file->OpenFile(m_CaptureFile, "rdc", NULL);
  }

  QString filename = QFileInfo(m_CaptureFile).fileName();

  if(!result.OK())
  {
    RDDialog::critical(
        m_MainWindow, tr("Error opening file"),
        tr("Couldn't open file '%1' for export\n%2").arg(filename).arg(result.Message()));

    if(local)
      local->Shutdown();
    return;
  }

  float progress = 0.0f;

  LambdaThread *th = new LambdaThread([file, sdfile, ext, exportfile, &progress, &result]() {
    result = file->Convert(exportfile, ext, sdfile, [&progress](float p) { progress = p; });
  });
  th->setName(lit("ExportCapture"));
  th->start();
  // wait a few ms before popping up a progress bar
  th->wait(500);
  if(th->isRunning())
  {
    ShowProgressDialog(
        m_MainWindow, tr("Exporting %1 to %2, please wait...").arg(filename).arg(QString(fmt.name)),
        [th]() { return !th->isRunning(); }, [&progress]() { return progress; });
  }
  th->deleteLater();

  if(local)
    local->Shutdown();

  if(!result.OK())
  {
    RDDialog::critical(m_MainWindow, tr("Error converting capture"),
                       tr("Couldn't convert file '%1'\n%2").arg(filename).arg(result.Message()));
  }
}

void CaptureContext::SetEventID(const rdcarray<ICaptureViewer *> &exclude, uint32_t selectedEventID,
                                uint32_t eventId, bool force)
{
  RENDERDOC_PROFILEFUNCTION();
  ++m_IdleCheckpointGeneration;

  if(!IsCaptureLoaded())
    return;

  if(eventId > m_LastAction->eventId)
  {
    qCritical() << "Invalid EID being selected " << eventId;
    return;
  }

  // A normal event selection supersedes any queued scrub/commit callback. The replay-queue FIFO
  // still preserves command correctness; this generation prevents the older asynchronous GUI
  // completion from publishing its event and pipeline state afterwards.
  m_Scrubbing = false;
  m_ScrubCommitPending = false;
  m_ScrubInFlight = false;
  m_ScrubEpoch++;
  m_ScrubGeneration++;

  m_StatusRefreshDeferred = false;
  m_StatusRefreshForce = false;

  uint32_t prevSelectedEventID = m_SelectedEventID;
  uint32_t prevEventID = m_EventID;

  const uint64_t epoch = m_ScrubEpoch.load();
  const uint64_t navigation = m_ScrubGeneration.load();
  // SetEventID also runs on the capture-loading worker before viewers receive OnCaptureLoaded.
  // Cancel old input state on the GUI thread without selecting rows or touching their models.
  GUIInvoke::call(m_MainWindow, [this, epoch, navigation]() {
    if(epoch != m_ScrubEpoch.load() || navigation != m_ScrubGeneration.load())
      return;
    if(m_EventBrowser)
      m_EventBrowser->CancelReplayNavigation();
    if(m_TimelineBar)
      m_TimelineBar->CancelReplayNavigation();
  });
  std::atomic<bool> done = {false};
  std::shared_ptr<const PublishedPipelineState> pipeline;

  // Only a real selected/effective EID transition is user-context activity.
  // Forced refreshes intentionally replay the same EID after internal viewer,
  // settings, or replacement maintenance; giving those callbacks the user tag
  // would make ReplayLab discard every otherwise-valid background result.
  QString tag = (prevSelectedEventID != selectedEventID || prevEventID != eventId)
                    // i18n-ignore: internal replay callback tag, never shown to the user.
                    ? lit("replaySetEvent")
                    // i18n-ignore: internal replay callback tag, never shown to the user.
                    : lit("replayRefreshEvent");

  m_Replay.AsyncInvoke(
      tag, [this, selectedEventID, eventId, force, &done, &pipeline](IReplayController *r) {
        r->SetSelectedEventID(selectedEventID);
        r->SetFrameEvent(eventId, force);
        pipeline = std::make_shared<PublishedPipelineState>(r);

        done.store(true, std::memory_order_release);
      });

  // wait a short while before displaying the progress dialog (which won't show if we're already
  // done by the time we reach it).
  // Keep waiting if the current tag is a set event, we don't want to be popping up progress bars
  // when the user is browsing the frame if it's going slow. If that's the case we'll just block the
  // UI thread. Instead only pop up the progress bar if some other large task is blocking.
  for(int i = 0; !done.load(std::memory_order_acquire) &&
                 (i < 100 || m_Replay.GetCurrentProcessingTag().isEmpty() ||
                  m_Replay.GetCurrentProcessingTag() == tag);
      i++)
    QThread::msleep(5);

  ShowProgressDialog(m_MainWindow->Widget(), tr("Please wait, working..."),
                     [&done]() { return done.load(std::memory_order_acquire); });

  if(epoch != m_ScrubEpoch.load() || !IsCaptureLoaded())
    return;

  m_SelectedEventID = selectedEventID;
  m_EventID = eventId;
  PublishPipelineState(std::move(pipeline));

  bool updateSelectedEvent = force || prevSelectedEventID != selectedEventID;
  bool updateEvent = force || prevEventID != eventId;

  RefreshUIStatus(exclude, updateSelectedEvent, updateEvent);
  ScheduleIdleCheckpointMaintenance();
}

void CaptureContext::RefreshStatus()
{
  QueueReplayStatusRefresh(true);
}

void CaptureContext::QueueReplayStatusRefresh(bool force)
{
  static const bool timing = qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1;
  if(!IsCaptureLoaded())
    return;

  ++m_IdleCheckpointGeneration;
  ++m_StatusRefreshSerial;
  const uint64_t epoch = m_ScrubEpoch.load();
  // Several replacement registrations and the final refresh hint can arrive in one GUI turn.
  // Snapshot once, preserving any request that needs a replay. Navigation/capture changes must
  // never inherit a previous capture's request or publish a late pipeline into the new view.
  m_StatusRefreshForce = force || (m_StatusRefreshEpoch == epoch && m_StatusRefreshForce);
  m_StatusRefreshEpoch = epoch;
  if(m_StatusRefreshQueued)
    return;

  m_StatusRefreshQueued = true;
  QElapsedTimer refreshTimer;
  if(timing)
    refreshTimer.start();
  QTimer::singleShot(0, m_MainWindow->Widget(), [this, refreshTimer]() {
    m_StatusRefreshQueued = false;
    const uint64_t epoch = m_StatusRefreshEpoch;
    const uint64_t serial = m_StatusRefreshSerial;
    if(!IsCaptureLoaded() || epoch != m_ScrubEpoch.load())
      return;
    // During an in-flight scrub the displayed EID intentionally lags the controller. Let its
    // viewer requests drain, then refresh that latest event instead of replaying an old display.
    if(m_ScrubInFlight || m_ScrubCommitPending)
    {
      m_StatusRefreshDeferred = true;
      return;
    }
    m_StatusRefreshDeferred = false;
    const bool force = m_StatusRefreshForce;
    m_StatusRefreshForce = false;
    const uint64_t navigation = m_ScrubGeneration.load();

    const uint32_t selectedEventId = m_SelectedEventID;
    const uint32_t eventId = m_EventID;
    // Never wait on the GUI thread here. Python callers may hold the GIL, and the replay queue
    // can contain Python callbacks that need it before this refresh is allowed to run.
    // A unique tag cannot replace a synchronous SetEventID callback waiting on its stack-local
    // completion, or discard an earlier forced refresh when a later snapshot-only one arrives.
    m_Replay.AsyncInvoke(
        // i18n-ignore: internal replay callback tag, never shown to the user.
        lit("replayStatusRefresh-") + QString::number(serial),
        [this, epoch, navigation, serial, selectedEventId, eventId, force,
         refreshTimer](IReplayController *r) {
          if(epoch != m_ScrubEpoch.load() || navigation != m_ScrubGeneration.load())
            return;
          r->SetSelectedEventID(selectedEventId);
          r->SetFrameEvent(eventId, force);
          const std::shared_ptr<const PublishedPipelineState> pipeline =
              std::make_shared<PublishedPipelineState>(r);
          GUIInvoke::call(m_MainWindow, [this, epoch, navigation, serial, selectedEventId, eventId,
                                         pipeline, refreshTimer]() {
            if(epoch != m_ScrubEpoch.load() || navigation != m_ScrubGeneration.load() ||
               serial != m_StatusRefreshSerial || !IsCaptureLoaded() ||
               m_SelectedEventID != selectedEventId || m_EventID != eventId)
              return;
            PublishPipelineState(pipeline);
            RefreshUIStatus({}, true, true);
            ScheduleIdleCheckpointMaintenance();
            // Profiling completion follows the viewers' queued output work. A source-save notice
            // alone does not mean that the changed shader has reached the texture display.
            if(timing)
              m_Replay.AsyncInvoke([this, epoch, navigation, serial, eventId,
                                    refreshTimer](IReplayController *) {
                InvokeAfterPresentation(
                    eventId, [this, epoch, navigation, serial, eventId, refreshTimer]() {
                      const bool published = IsCaptureLoaded() && epoch == m_ScrubEpoch.load() &&
                                             navigation == m_ScrubGeneration.load() &&
                                             serial == m_StatusRefreshSerial && eventId == m_EventID;
                      const double totalMS = refreshTimer.nsecsElapsed() / 1.0e6;
                      LogEIDTiming(QString::asprintf(
                          // i18n-ignore: opt-in native completion profiling fields, not UI labels.
                          "status refresh: event=%u serial=%llu nativeTotal=%.6f "
                          "finish_clock_ms=%.6f published=%u",
                          eventId, (unsigned long long)serial, totalMS,
                          refreshTimer.msecsSinceReference() + totalMS, published ? 1U : 0U));
                    });
              });
          });
        });
  });
}

uint64_t CaptureContext::ReplayStatusPublications::Begin(std::function<void(bool)> completed)
{
  const uint64_t request = ++serial;
  QMap<uint64_t, std::function<void(bool)>> superseded;
  superseded.swap(pending);
  pending.insert(request, std::move(completed));
  for(const std::function<void(bool)> &callback : superseded)
    if(callback)
      callback(false);
  return request;
}

bool CaptureContext::ReplayStatusPublications::Current(uint64_t request) const
{
  return request == serial && pending.contains(request);
}

void CaptureContext::ReplayStatusPublications::Complete(uint64_t request, bool published)
{
  auto it = pending.find(request);
  if(it == pending.end())
    return;
  std::function<void(bool)> completed = std::move(it.value());
  pending.erase(it);
  if(completed)
    completed(published);
}

void CaptureContext::ReplayStatusPublications::Cancel()
{
  ++serial;
  QMap<uint64_t, std::function<void(bool)>> cancelled;
  cancelled.swap(pending);
  for(const std::function<void(bool)> &callback : cancelled)
    if(callback)
      callback(false);
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

struct CaptureContextPublicationTestAccess
{
  static void CheckCallbacks()
  {
    CaptureContext::ReplayStatusPublications publications;
    QList<bool> first, second, third;
    SECTION("Superseding a same-event request rejects late publication and completes once")
    {
      const uint64_t a = publications.Begin([&](bool result) { first.push_back(result); });
      const uint64_t b = publications.Begin([&](bool result) { second.push_back(result); });
      CHECK_FALSE(publications.Current(a));
      CHECK(publications.Current(b));
      publications.Complete(a, true);
      REQUIRE(first.size() == 1);
      CHECK_FALSE(first[0]);
      publications.Complete(b, true);
      publications.Complete(b, false);
      REQUIRE(second.size() == 1);
      CHECK(second[0]);
      CHECK_FALSE(publications.Current(b));
    }
    SECTION("A cancellation callback can supersede the new request reentrantly")
    {
      uint64_t newest = 0;
      publications.Begin([&](bool result) {
        first.push_back(result);
        newest = publications.Begin([&](bool accepted) { third.push_back(accepted); });
      });
      const uint64_t superseded =
          publications.Begin([&](bool result) { second.push_back(result); });
      CHECK_FALSE(publications.Current(superseded));
      CHECK(publications.Current(newest));
      REQUIRE(first.size() == 1);
      REQUIRE(second.size() == 1);
      CHECK_FALSE(first[0]);
      CHECK_FALSE(second[0]);
      publications.Complete(newest, true);
      REQUIRE(third.size() == 1);
      CHECK(third[0]);
    }
    SECTION("Close resolves disposed replay work and rejects a reentrant closed-capture request")
    {
      bool loaded = true;
      const uint64_t request = publications.Begin([&](bool result) {
        first.push_back(result);
        const uint64_t reentrant =
            publications.Begin([&](bool accepted) { second.push_back(accepted); });
        if(!loaded)
          publications.Complete(reentrant, false);
      });
      // CloseCapture changes loaded before detaching/completing callbacks. The replay queue can
      // then discard its callback entirely; alternatively its late GUI delivery is harmless.
      loaded = false;
      publications.Cancel();
      CHECK_FALSE(publications.Current(request));
      publications.Complete(request, true);
      publications.Cancel();
      REQUIRE(first.size() == 1);
      REQUIRE(second.size() == 1);
      CHECK_FALSE(first[0]);
      CHECK_FALSE(second[0]);
      CHECK(publications.pending.isEmpty());
    }
    SECTION("Completion detaches before invoking a client that requests another publication")
    {
      uint64_t request = 0, next = 0;
      request = publications.Begin([&](bool result) {
        first.push_back(result);
        publications.Complete(request, false);
        next = publications.Begin([&](bool accepted) { second.push_back(accepted); });
      });
      publications.Complete(request, true);
      REQUIRE(first.size() == 1);
      CHECK(first[0]);
      CHECK(publications.Current(next));
      publications.Cancel();
      REQUIRE(second.size() == 1);
      CHECK_FALSE(second[0]);
    }
    SECTION("A publication without an observer still has a cancellable identity")
    {
      const uint64_t request = publications.Begin({});
      CHECK(publications.Current(request));
      publications.Cancel();
      CHECK_FALSE(publications.Current(request));
      publications.Complete(request, true);
      CHECK(publications.pending.isEmpty());
    }
  }
};

TEST_CASE("Replay publication completion survives supersession and capture close",
          "[capture-context][publication][checkpoints]")
{
  CaptureContextPublicationTestAccess::CheckCallbacks();
}

#endif

void CaptureContext::RefreshReplayStatus(uint32_t expectedEventId,
                                          uint64_t expectedBufferGeneration,
                                          std::function<void(bool)> completed)
{
  static const bool timing = qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1;
  QElapsedTimer clock;
  if(timing)
    clock.start();
  ++m_IdleCheckpointGeneration;
  const uint64_t request = m_ReplayStatusPublications.Begin(std::move(completed));
  const uint64_t epoch = m_ScrubEpoch.load();
  if(!m_ReplayStatusPublications.Current(request) || !IsCaptureLoaded() ||
     CurEvent() != expectedEventId)
  {
    m_ReplayStatusPublications.Complete(request, false);
    return;
  }

  // The setter already rebuilt this event. Snapshot the owned pipeline without SetFrameEvent,
  // and keep the editing queue in flight until every viewer has consumed this publication.
  m_Replay.AsyncInvoke(
      [this, epoch, expectedEventId, expectedBufferGeneration, request, clock](IReplayController *r) {
        const double queueMS = timing ? clock.nsecsElapsed() / 1.0e6 : 0.0;
        std::shared_ptr<const PublishedPipelineState> pipeline;
        if(epoch == m_ScrubEpoch.load() && r->GetCurrentEventID() == expectedEventId &&
           r->GetSharedBufferPatchGeneration() == expectedBufferGeneration)
          pipeline = std::make_shared<PublishedPipelineState>(r);
        const double snapshotMS = timing ? clock.nsecsElapsed() / 1.0e6 - queueMS : 0.0;

        GUIInvoke::call(m_MainWindow, [this, epoch, expectedEventId, expectedBufferGeneration,
                                      request, pipeline, clock, queueMS, snapshotMS]() {
          if(!m_ReplayStatusPublications.Current(request) || !pipeline ||
             epoch != m_ScrubEpoch.load() || !IsCaptureLoaded() ||
             CurEvent() != expectedEventId)
          {
            m_ReplayStatusPublications.Complete(request, false);
            return;
          }

          PublishPipelineState(pipeline);
          const uint64_t serial = m_PipelinePublicationSerial;
          const double notifyBeginMS = timing ? clock.nsecsElapsed() / 1.0e6 : 0.0;
          // Shared buffer data changed, but the selected EID did not. Reissuing selection
          // notifications needlessly rebuilds event selection and extension context. The
          // normal event/data callbacks still refresh all dependent viewers and resources.
          static const bool legacyRefresh =
              qEnvironmentVariableIntValue("RENDERDOC_CBUFFER_EDIT_LEGACY_REFRESH") == 1;
          RefreshUIStatus({}, legacyRefresh, true);
          const double notifyMS = timing ? clock.nsecsElapsed() / 1.0e6 - notifyBeginMS : 0.0;
          m_Replay.AsyncInvoke(
              [this, epoch, serial, expectedEventId, expectedBufferGeneration,
               request, clock, queueMS, snapshotMS, notifyMS](IReplayController *r) {
                const bool valid = r->GetCurrentEventID() == expectedEventId &&
                                   r->GetSharedBufferPatchGeneration() == expectedBufferGeneration;
                InvokeAfterPresentation(
                    expectedEventId, [this, epoch, serial, expectedEventId, request, valid, clock,
                                      queueMS, snapshotMS, notifyMS]() {
                      const bool published = m_ReplayStatusPublications.Current(request) && valid &&
                                             epoch == m_ScrubEpoch.load() && IsCaptureLoaded() &&
                                             CurEvent() == expectedEventId &&
                                             serial == m_PipelinePublicationSerial;
                      if(published)
                        ScheduleIdleCheckpointMaintenance();
                      if(timing)
                        LogEIDTiming(QString::asprintf(
                            "api=UI op=cbuffer_publication event=%u queue_ms=%.3f "
                            "snapshot_ms=%.3f notify_ms=%.3f total_ms=%.3f published=%u",
                            expectedEventId, queueMS, snapshotMS, notifyMS,
                            clock.nsecsElapsed() / 1.0e6, published ? 1U : 0U));
                      m_ReplayStatusPublications.Complete(request, published);
                    });
              });
        });
      });
}

void CaptureContext::ScrubToEvent(uint32_t eventId)
{
  ScrubToSelectedEvent(eventId, eventId);
}

void CaptureContext::ScrubToSelectedEvent(uint32_t selectedEventId, uint32_t eventId)
{
  ++m_IdleCheckpointGeneration;
  if(!IsCaptureLoaded() || !m_LastAction)
    return;

  eventId = qMin(eventId, m_LastAction->eventId);
  // Synthetic pass markers have selected IDs above the final captured action. Their replay
  // endpoint is a real child EID, but clamping the row ID selects an unrelated final event.
  // Keep the same selected/effective distinction as SetEventID.

  // Repeated pointer samples within one event are not new navigation. Edits use RefreshStatus /
  // forced SetEventID and therefore still replay even when the selected event does not change.
  if(m_Scrubbing && m_ScrubSelectedEventID == selectedEventId && m_ScrubEventID == eventId)
    return;
  if(!m_Scrubbing && !m_ScrubCommitPending && m_SelectedEventID == selectedEventId &&
     m_EventID == eventId)
    return;

  const bool startingScrub = !m_Scrubbing;
  m_Scrubbing = true;
  m_ScrubCommitPending = true;
  m_ScrubSelectedEventID = selectedEventId;
  m_ScrubEventID = eventId;
  ++m_ScrubGeneration;

  // Navigation controls acknowledge input immediately. CurEvent and the owned pipeline stay
  // on the completed replay until QueueScrubReplay publishes the new frame.
  const uint64_t inputGeneration = m_ScrubGeneration.load();
  const uint64_t inputEpoch = m_ScrubEpoch.load();
  if(m_EventBrowser)
    m_EventBrowser->OnReplayNavigationRequested(selectedEventId, eventId);
  if(m_TimelineBar)
    m_TimelineBar->OnReplayNavigationRequested(eventId);
  if(!IsCaptureLoaded() || inputEpoch != m_ScrubEpoch.load() ||
     inputGeneration != m_ScrubGeneration.load())
    return;

  if(startingScrub)
  {
    // This is only an input notification for cancelling background history work. Display and
    // pipeline state are published together through the normal OnEventChanged path below.
    rdcarray<ICaptureViewer *> capviewers(m_CaptureViewers);
    for(ICaptureViewer *viewer : capviewers)
      if(viewer && m_CaptureViewers.contains(viewer))
        viewer->OnScrubEvent(eventId);
  }

  QueueScrubReplay();
}

void CaptureContext::QueueScrubReplay()
{
  if(m_ScrubInFlight || !m_ScrubCommitPending || !IsCaptureLoaded())
    return;

  m_ScrubInFlight = true;
  const uint32_t selectedEventId = m_ScrubSelectedEventID;
  const uint32_t eventId = m_ScrubEventID;
  const uint64_t generation = m_ScrubGeneration.load();
  const uint64_t epoch = m_ScrubEpoch.load();
  static const bool eidTiming = qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1;
  QElapsedTimer enqueueTimer;
  if(eidTiming)
    enqueueTimer.start();

  m_Replay.AsyncInvoke(lit("replayInteractiveEvent"),    // i18n-ignore: Internal replay-queue tag.
                       [this, selectedEventId, eventId, generation, epoch,
                        enqueueTimer](IReplayController *r) {
    if(epoch != m_ScrubEpoch.load())
      return;

    // Even if more input arrived while this callback was queued, finish and publish this frame.
    // The single in-flight cycle bounds obsolete work to one event and guarantees progress while
    // the mouse moves continuously; only the subsequent pending target is replaced by new input.

    const qint64 waitNS = eidTiming ? enqueueTimer.nsecsElapsed() : 0;
    QElapsedTimer replayTimer;
    if(eidTiming)
      replayTimer.start();

    r->SetSelectedEventID(selectedEventId);
    r->ScrubFrameEvent(eventId);
    r->CommitScrubFrameEvent(eventId);
    const std::shared_ptr<const PublishedPipelineState> pipeline =
        std::make_shared<PublishedPipelineState>(r);
    const qint64 replayNS = eidTiming ? replayTimer.nsecsElapsed() : 0;
    const qint64 replayFinishNS = eidTiming ? enqueueTimer.nsecsElapsed() : 0;

    // Do not wait for this closure on the replay thread. Some OnEventChanged handlers issue
    // BlockInvoke calls, so waiting here would deadlock the GUI and the replay thread.
    GUIInvoke::call(m_MainWindow, [this, selectedEventId, eventId, generation, epoch, pipeline,
                                   waitNS, replayNS, replayFinishNS, enqueueTimer]() {
      if(epoch != m_ScrubEpoch.load() || !IsCaptureLoaded())
        return;

      const qint64 guiDeliveryNS = eidTiming ? enqueueTimer.nsecsElapsed() - replayFinishNS : 0;

      // Releasing input must leave Qt's cursor at the final requested row. An older in-flight
      // replay can finish after EndScrub; publishing it would pull the row backwards before
      // the final target arrives. While dragging we still publish bounded intermediate frames
      // for progress. Once released, advance straight to the latest target instead.
      if(!m_Scrubbing && generation != m_ScrubGeneration.load())
      {
        m_ScrubInFlight = false;
        QueueScrubReplay();
        return;
      }

      m_SelectedEventID = selectedEventId;
      m_EventID = eventId;
      QElapsedTimer publicationTimer;
      if(eidTiming)
        publicationTimer.start();
      PublishPipelineState(pipeline);
      const qint64 publicationNS = eidTiming ? publicationTimer.nsecsElapsed() : 0;

      QElapsedTimer panelTimer;
      if(eidTiming)
        panelTimer.start();
      RefreshUIStatus({}, true, true);
      const qint64 panelNS = eidTiming ? panelTimer.nsecsElapsed() : 0;
      if(epoch != m_ScrubEpoch.load() || !IsCaptureLoaded())
        return;

      // OnEventChanged has now queued texture binding, display, thumbnails, and panel reads for
      // this event. Drain those callbacks before another scrub can change the physical resources.
      // Their GUI deliveries precede this completion too. Late paint/analysis callbacks also
      // validate their expected EID before using the controller.
      m_Replay.AsyncInvoke(
          lit("replayInteractiveCommit"),    // i18n-ignore: Internal replay-queue tag.
          [this, selectedEventId, eventId, generation, epoch, waitNS, replayNS, panelNS,
           guiDeliveryNS, publicationNS, panelTimer, enqueueTimer](IReplayController *) {
            InvokeAfterPresentation(
                eventId,
                [this, selectedEventId, eventId, generation, epoch, waitNS, replayNS, panelNS,
                 guiDeliveryNS, publicationNS, panelTimer, enqueueTimer]() {
                  if(epoch != m_ScrubEpoch.load() || !IsCaptureLoaded() || m_EventID != eventId)
                    return;

                  const qint64 displayNS = eidTiming ? panelTimer.nsecsElapsed() - panelNS : 0;
                  if(eidTiming)
                  {
                    LogEIDTiming(QString::asprintf(
                        "preview EID %u: queueWait %.2fms, replay %.2fms, generation %llu, state "
                        "%s",
                        (unsigned)eventId, waitNS / 1.0e6, replayNS / 1.0e6,
                        (unsigned long long)generation, m_Scrubbing ? "dragging" : "released"));
                    LogEIDTiming(QString::asprintf(
                        "scrub EID %u: queueWait %.2fms, replay %.2fms, scrubPanels %.2fms, "
                        "generation %llu, displayAndReads %.2fms",
                        (unsigned)eventId, waitNS / 1.0e6, replayNS / 1.0e6, panelNS / 1.0e6,
                        (unsigned long long)generation, displayNS / 1.0e6));
                    if(!m_Scrubbing && generation == m_ScrubGeneration.load())
                      LogEIDTiming(QString::asprintf(
                          // i18n-ignore: opt-in native completion timing; excludes fixture polling.
                          "commit EID %u: queueWait %.2fms, replay/pipeline %.2fms, panels %.2fms, "
                          "generation %llu, displayAndReads %.2fms, nativeTotal %.3fms, "
                          "guiDelivery %.3fms, publishState %.3fms",
                          (unsigned)eventId, waitNS / 1.0e6, replayNS / 1.0e6, panelNS / 1.0e6,
                          (unsigned long long)generation, displayNS / 1.0e6,
                          enqueueTimer.nsecsElapsed() / 1.0e6, guiDeliveryNS / 1.0e6,
                          publicationNS / 1.0e6));
                  }

                  m_ScrubInFlight = false;
                  m_ScrubCommitPending = generation != m_ScrubGeneration.load();
                  QueueScrubReplay();
                  if(!m_ScrubInFlight && !m_ScrubCommitPending && m_StatusRefreshDeferred)
                    QueueReplayStatusRefresh(m_StatusRefreshForce);
                  if(!m_Scrubbing && !m_ScrubCommitPending)
                    ScheduleIdleCheckpointMaintenance();
                },
                generation);
          });
    });
  });
}

void CaptureContext::EndScrub()
{
  // Every completed drag step already publishes full pipeline and panel state. Releasing only
  // ends input; the last requested step continues asynchronously without a second replay.
  m_Scrubbing = false;
  if(!m_ScrubInFlight && !m_ScrubCommitPending && m_StatusRefreshDeferred)
    QueueReplayStatusRefresh(m_StatusRefreshForce);
  if(!m_ScrubInFlight && !m_ScrubCommitPending)
    ScheduleIdleCheckpointMaintenance();
}

void CaptureContext::InvokeAfterPresentation(uint32_t eventId, std::function<void()> completed,
                                             uint64_t scrubGeneration)
{
  GUIInvoke::call(m_MainWindow, [this, eventId, completed, scrubGeneration]() {
    // Superseded previews must release the single in-flight cycle even if their display was
    // cancelled. Only the final released generation is a full presentation completion.
    if(scrubGeneration && (m_Scrubbing || scrubGeneration != m_ScrubGeneration.load()))
      completed();
    else if(m_TextureViewer)
      m_TextureViewer->WhenPresentationReady(eventId, completed);
    else
      completed();
  });
}

void CaptureContext::ScheduleIdleCheckpointMaintenance(int delayMS)
{
  const uint64_t request = ++m_IdleCheckpointGeneration;
  if(!IsCaptureLoaded() || m_Scrubbing || m_ScrubCommitPending || m_ScrubInFlight)
    return;

  const uint64_t epoch = m_ScrubEpoch.load();
  const uint32_t eventId = m_EventID;
  // Capture both mutation versions after the current publication's viewer requests. A timer
  // must never silently adopt a newer edit merely because it targets the same EID.
  m_Replay.AsyncInvoke([this, request, epoch, eventId, delayMS](IReplayController *r) {
    if(request != m_IdleCheckpointGeneration.load() || epoch != m_ScrubEpoch.load() ||
       r->GetCurrentEventID() != eventId)
      return;
    const uint64_t mutation = r->GetShaderReplacementGeneration(eventId);
    const uint64_t buffer = r->GetSharedBufferPatchGeneration();
    GUIInvoke::call(m_MainWindow, [this, request, epoch, eventId, mutation, buffer, delayMS]() {
      if(request != m_IdleCheckpointGeneration.load() || epoch != m_ScrubEpoch.load() ||
         !IsCaptureLoaded() || m_EventID != eventId)
        return;
      QTimer::singleShot(delayMS, m_MainWindow, [this, request, epoch, eventId, mutation, buffer]() {
        if(request != m_IdleCheckpointGeneration.load() || epoch != m_ScrubEpoch.load() ||
           !IsCaptureLoaded() || m_EventID != eventId || m_Scrubbing || m_ScrubCommitPending ||
           m_ScrubInFlight)
          return;
        m_Replay.AsyncInvoke(
            // i18n-ignore: internal low-priority replay queue tag, not a UI label.
            lit("ReplayLab.background.checkpoints"),
            [this, request, epoch, eventId, mutation, buffer](IReplayController *r) {
              if(request != m_IdleCheckpointGeneration.load() || epoch != m_ScrubEpoch.load() ||
                 m_Replay.HasPendingNonBackgroundInvoke())
                return;
              // The controller rechecks event, mutation and buffer generations and admits at
              // most one useful replay. It restores this same event before returning and does
              // not publish another image or rebuild the visible pipeline snapshot.
              const rdcstr receipt = r->RunIdleCheckpointMaintenance(eventId, mutation, buffer);
              const bool continueIdle = receipt.contains("\"continueIdle\":true");
              // i18n-ignore: internal maintenance receipt key, never displayed as a UI label.
              const int retry = QJsonDocument::fromJson(QByteArray(receipt.c_str()))
                                    .object()
                                    .value(QLatin1String("retryIdleAfterMS"))
                                    .toInt();
              const int nextDelay = continueIdle ? 750 : retry >= 750 && retry <= 5000 ? retry : 0;
              if(nextDelay)
                GUIInvoke::call(m_MainWindow, [this, request, epoch, eventId, nextDelay]() {
                  if(request == m_IdleCheckpointGeneration.load() && epoch == m_ScrubEpoch.load() &&
                     IsCaptureLoaded() && m_EventID == eventId && !m_Scrubbing &&
                     !m_ScrubCommitPending && !m_ScrubInFlight)
                    ScheduleIdleCheckpointMaintenance(nextDelay);
                });
              if(qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1)
                GUIInvoke::call(m_MainWindow, [this, receipt]() {
                  LogEIDTiming(QString::fromUtf8(receipt.c_str()));
                });
            });
      });
    });
  });
}

void CaptureContext::LogEIDTiming(const QString &line)
{
  // Append EID-switch timing to <exe dir>/eid_timing.log (RENDERDOC_EID_TIMING=1). The exe dir is
  // inside the repo checkout, so it is easy to inspect. Also mirrored to qInfo / the diag log.
  qInfo("[EIDTiming] %s", qUtf8Printable(line));
  static QFile *logFile = NULL;
  if(logFile == NULL)
  {
    // i18n-ignore: diagnostic log filename, not user-visible UI text
    logFile = new QFile(QDir(qApp->applicationDirPath()).filePath(lit("eid_timing.log")));
    logFile->open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text);
  }
  if(logFile && logFile->isOpen())
  {
    logFile->write((line + lit("\n")).toUtf8());
    logFile->flush();
  }
}

void CaptureContext::CancelReplayBackgroundWork()
{
  ++m_IdleCheckpointGeneration;
  if(m_TextureViewer)
    m_TextureViewer->CancelReplayBackgroundWork();
}

void CaptureContext::ConnectToRemoteServer(RemoteHost host)
{
  rdcarray<RemoteHost> hosts = Config().GetRemoteHosts();

  int hostIdx = -1;
  for(int32_t i = 0; i < hosts.count(); i++)
  {
    if(hosts[i].Hostname() == host.Hostname())
    {
      hostIdx = i;
      break;
    }
  }

  if(hostIdx >= 0)
    m_MainWindow->setRemoteHost(hostIdx);
}

void CaptureContext::SetRemoteHost(int hostIdx)
{
  m_MainWindow->setRemoteHost(hostIdx);
}

bool CaptureContext::IsResourceReplaced(ResourceId id)
{
  return m_OrigToReplacedResources.contains(id);
}

ResourceId CaptureContext::GetResourceReplacement(ResourceId id)
{
  auto it = m_OrigToReplacedResources.find(id);
  if(it != m_OrigToReplacedResources.end())
    return it.value();

  return ResourceId();
}

void CaptureContext::RegisterReplacement(ResourceId from, ResourceId to)
{
  if(from == ResourceId() || to == ResourceId() || from == to)
  {
    qCritical() << "Refusing invalid replacement registration" << ToQStr(from)
                << "->" << ToQStr(to);
    return;
  }

  auto targetOwner = m_ReplacedToOrigResources.constFind(to);
  if(targetOwner != m_ReplacedToOrigResources.constEnd() && targetOwner.value() != from)
  {
    // A reverse entry has exactly one owner. Do not overwrite it and make
    // resource naming/selection disagree with the replay ResourceManager.
    qCritical() << "Refusing replacement target already owned by"
                << ToQStr(targetOwner.value()) << ":" << ToQStr(to);
    return;
  }

  auto previous = m_OrigToReplacedResources.find(from);
  if(previous != m_OrigToReplacedResources.end())
  {
    if(previous.value() == to)
      return;

    // Superseding one original must retire its old reverse edge before the
    // new pair is inserted. Otherwise GetResourceName() can keep treating a
    // freed target Shader as live UI state.
    auto oldReverse = m_ReplacedToOrigResources.find(previous.value());
    if(oldReverse != m_ReplacedToOrigResources.end() && oldReverse.value() == from)
      m_ReplacedToOrigResources.erase(oldReverse);
  }

  m_OrigToReplacedResources[from] = to;
  m_ReplacedToOrigResources[to] = from;

  CacheResources();

  // A replacement may have rebuilt the controller's pipeline. Publish its owned current value
  // before notifying viewers instead of relying on the old controller-pointer alias to update.
  QueueReplayStatusRefresh(false);
}

void CaptureContext::UnregisterReplacement(ResourceId id)
{
  auto it = m_OrigToReplacedResources.find(id);
  if(it != m_OrigToReplacedResources.end())
  {
    m_ReplacedToOrigResources.remove(it.value());
    m_OrigToReplacedResources.remove(it.key());
  }

  CacheResources();

  QueueReplayStatusRefresh(false);
}

bool CaptureContext::RegisterCBufferDebugDisplaySource(
    const rdcstr &ownerToken, ResourceId original, ResourceId replacement,
    const rdcstr &sourceRawSha256, const rdcstr &activeRawSha256, ShaderStage stage,
    uint64_t mutationGeneration, const rdcarray<ConstantBlock> &sourceBlocks,
    const rdcarray<ConstantBlock> &activeBlocks)
{
  if(!IsCaptureLoaded() || !m_Resources.contains(original))
    return false;
  return m_CBufferDebugDisplay.Register(QString(ownerToken), original, replacement,
      QString(sourceRawSha256), QString(activeRawSha256), stage, mutationGeneration,
      sourceBlocks, activeBlocks);
}

void CaptureContext::UnregisterCBufferDebugDisplaySource(const rdcstr &ownerToken)
{
  m_CBufferDebugDisplay.Remove(QString(ownerToken));
}

CBufferDebugDisplaySource CaptureContext::GetCBufferDebugDisplaySource(
    ResourceId original, ShaderStage stage, const rdcstr &activeRawSha256,
    const ConstantBlock &activeBlock)
{
  if(!IsCaptureLoaded() || !m_PublishedPipelineState)
    return CBufferDebugDisplaySource();
  return m_CBufferDebugDisplay.Resolve(original, stage, QString(activeRawSha256), activeBlock,
                                      m_PublishedPipelineState->shaderMutationGeneration);
}

void CaptureContext::RefreshUIStatus(const rdcarray<ICaptureViewer *> &exclude,
                                     bool updateSelectedEvent, bool updateEvent)
{
  static const bool eidTiming = qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1;
  const uint64_t refreshEpoch = m_ScrubEpoch.load();
  // Keep this publication alive if a viewer synchronously publishes another event. The epoch
  // checks below then stop the old notification pass without invalidating that viewer's locals.
  const std::shared_ptr<const PublishedPipelineState> publication = m_PublishedPipelineState;
  const PipeState &pipeline = publication ? publication->pipeline : m_DummyPipelineState;
  // cache and assign pointer type IDs for any known pointer types in current shaders
  for(ShaderStage stage :
      {ShaderStage::Vertex, ShaderStage::Hull, ShaderStage::Domain, ShaderStage::Geometry,
       ShaderStage::Pixel, ShaderStage::Compute, ShaderStage::Task, ShaderStage::Mesh})
  {
    const ShaderReflection *refl = pipeline.GetShaderReflection(stage);
    if(refl)
      PointerTypeRegistry::CacheShader(refl);
  }

  rdcarray<ICaptureViewer *> capviewers(m_CaptureViewers);

  for(ICaptureViewer *viewer : capviewers)
  {
    if(!viewer || exclude.contains(viewer) || !m_CaptureViewers.contains(viewer))
      continue;

    QElapsedTimer viewerTimer;
    if(eidTiming)
      viewerTimer.start();
    if(updateSelectedEvent)
      viewer->OnSelectedEventChanged(m_SelectedEventID);
    // Extension viewers may navigate, close the capture, or unregister themselves synchronously.
    // Do not continue delivering this publication after such a context change.
    if(refreshEpoch != m_ScrubEpoch.load() || !IsCaptureLoaded())
      return;
    if(updateEvent && m_CaptureViewers.contains(viewer))
      viewer->OnEventChanged(m_EventID);
    if(eidTiming)
    {
      const char *name = viewer == m_EventBrowser     ? "events"
                         : viewer == m_TextureViewer  ? "texture"
                         : viewer == m_PipelineViewer ? "pipeline"
                         : viewer == m_APIInspector   ? "apiInspector"
                         : viewer == m_TimelineBar    ? "timeline"
                                                      : "other";
      // i18n-ignore: opt-in per-viewer timing diagnostic, never a UI label.
      LogEIDTiming(QString::asprintf("panel EID %u: viewer=%s duration_ms=%.3f", m_EventID, name,
                                     viewerTimer.nsecsElapsed() / 1.0e6));
    }
    if(refreshEpoch != m_ScrubEpoch.load() || !IsCaptureLoaded())
      return;
  }
}

void CaptureContext::InvokeOntoUIThread(std::function<void()> callback)
{
  GUIInvoke::call(GetMainWindow()->Widget(), callback);
}

void CaptureContext::AddMessages(const rdcarray<DebugMessage> &msgs)
{
  m_UnreadMessageCount += msgs.count();
  for(const DebugMessage &msg : msgs)
    m_DebugMessages.push_back(msg);

  if(m_DebugMessageView)
  {
    GUIInvoke::call(m_DebugMessageView, [this]() { m_DebugMessageView->RefreshMessageList(); });
  }
}

void CaptureContext::ClearMessages()
{
  m_UnreadMessageCount = 0;
  m_DebugMessages.clear();

  if(m_DebugMessageView)
  {
    GUIInvoke::call(m_DebugMessageView, [this]() { m_DebugMessageView->RefreshMessageList(); });
  }
}

void CaptureContext::SetNotes(const rdcstr &key, const rdcstr &contents)
{
  // ignore no-op changes
  if(m_Notes.contains(key) && m_Notes[key] == contents)
    return;

  m_Notes[key] = contents;

  SetModification(CaptureModifications::Notes);

  RefreshUIStatus({}, true, true);
}

void CaptureContext::SetBookmark(const EventBookmark &mark)
{
  if(mark.eventId == 0)
    return;

  ANALYTIC_SET(UIFeatures.Bookmarks, true);

  int index = m_Bookmarks.indexOf(mark);
  if(index >= 0)
  {
    // ignore no-op bookmarks
    if(m_Bookmarks[index].text == mark.text)
      return;

    m_Bookmarks[index] = mark;
  }
  else
  {
    m_Bookmarks.push_back(mark);
  }

  SetModification(CaptureModifications::Bookmarks);

  NotifyBookmarksChanged();
}

void CaptureContext::RemoveBookmark(uint32_t EID)
{
  rdcarray<uint32_t> EIDs;
  EIDs.push_back(EID);
  RemoveBookmarks(EIDs);
}

void CaptureContext::RemoveBookmarks(const rdcarray<uint32_t> &EIDs)
{
  const int previousCount = m_Bookmarks.count();
  for(uint32_t EID : EIDs)
    m_Bookmarks.removeOne(EventBookmark(EID));

  if(previousCount == m_Bookmarks.count())
    return;

  SetModification(CaptureModifications::Bookmarks);

  NotifyBookmarksChanged();
}

void CaptureContext::SetBookmarkOrder(const rdcarray<uint32_t> &EIDs)
{
  if(EIDs.count() != m_Bookmarks.count())
    return;

  QList<EventBookmark> orderedBookmarks;
  QSet<uint32_t> seen;
  bool changed = false;

  for(int i = 0; i < EIDs.count(); ++i)
  {
    const uint32_t EID = EIDs[i];
    if(seen.contains(EID))
      return;
    seen.insert(EID);

    const int bookmarkIndex = m_Bookmarks.indexOf(EventBookmark(EID));
    if(bookmarkIndex < 0)
      return;

    orderedBookmarks.push_back(m_Bookmarks[bookmarkIndex]);
    changed |= bookmarkIndex != i;
  }

  if(!changed)
    return;

  m_Bookmarks = orderedBookmarks;
  SetModification(CaptureModifications::Bookmarks);
  NotifyBookmarksChanged();
}

void CaptureContext::NotifyBookmarksChanged()
{
  if(m_BookmarkRefreshPending)
    return;

  m_BookmarkRefreshPending = true;
  QTimer::singleShot(0, m_MainWindow, [this]() {
    m_BookmarkRefreshPending = false;

    if(m_EventBrowser)
      m_EventBrowser->BookmarksChanged();
    if(m_BookmarksView)
      m_BookmarksView->BookmarksChanged();
  });
}

void CaptureContext::DelayedCallback(uint32_t milliseconds, std::function<void()> callback)
{
  QTimer::singleShot(milliseconds, callback);
}

void CaptureContext::SetModification(CaptureModifications mod)
{
  m_CaptureMods |= mod;
  m_MainWindow->captureModified();
}

void CaptureContext::SaveChanges()
{
  bool success = true;

  if(!m_Replay.GetCaptureFile())
  {
    success = false;
  }
  else
  {
    if(m_CaptureMods & CaptureModifications::Renames)
      success &= SaveRenames();

    if(m_CaptureMods & CaptureModifications::Bookmarks)
      success &= SaveBookmarks();

    if(m_CaptureMods & CaptureModifications::Notes)
      success &= SaveNotes();

    if(m_CaptureMods & CaptureModifications::EditedShaders)
      success &= SaveEdits();
  }

  if(!success)
  {
    RDDialog::critical(m_MainWindow, tr("Can't save file"),
                       tr("Error saving file.\n"
                          "Check for permissions and that it's not open elsewhere"));
  }

  m_CaptureMods = CaptureModifications::NoModifications;
}

bool CaptureContext::SaveRenames()
{
  QVariantMap resources;
  for(ResourceId id : m_CustomNames.keys())
  {
    resources[ToQStr(id)] = m_CustomNames[id];
  }

  QVariantMap root;
  root[lit("CustomResourceNames")] = resources;

  QString json = VariantToJSON(root);

  SectionProperties props;
  props.type = SectionType::ResourceRenames;
  props.version = 1;

  return Replay().GetCaptureFile()->WriteSection(props, json.toUtf8()).OK();
}

void CaptureContext::LoadRenames(const QString &data)
{
  QVariantMap root = JSONToVariant(data);

  if(root.contains(lit("CustomResourceNames")))
  {
    QVariantMap resources = root[lit("CustomResourceNames")].toMap();

    for(const QString &str : resources.keys())
    {
      ResourceId id;

      if(str.startsWith(lit("ResourceId::")))
      {
        qulonglong num = str.mid(sizeof("ResourceId::") - 1).toULongLong();
        memcpy(&id, &num, sizeof(num));
      }
      else
      {
        qCritical() << "Unrecognised resourceid encoding" << str;
      }

      if(id != ResourceId())
        m_CustomNames[id] = resources[str].toString();
    }
  }
}

bool CaptureContext::SaveBookmarks()
{
  QVariantList bookmarks;
  for(const EventBookmark &mark : m_Bookmarks)
  {
    QVariantMap variantmark;
    variantmark[lit("eventId")] = mark.eventId;
    variantmark[lit("text")] = mark.text;

    bookmarks.push_back(variantmark);
  }

  QVariantMap root;
  root[lit("Bookmarks")] = bookmarks;

  QString json = VariantToJSON(root);

  SectionProperties props;
  props.type = SectionType::Bookmarks;
  props.version = 1;

  return Replay().GetCaptureFile()->WriteSection(props, json.toUtf8()).OK();
}

void CaptureContext::LoadBookmarks(const QString &data)
{
  QVariantMap root = JSONToVariant(data);

  if(root.contains(lit("Bookmarks")))
  {
    QVariantList bookmarks = root[lit("Bookmarks")].toList();

    for(QVariant v : bookmarks)
    {
      QVariantMap variantmark = v.toMap();

      EventBookmark mark;
      mark.eventId = variantmark[lit("eventId")].toUInt();
      mark.text = variantmark[lit("text")].toString();

      if(mark.eventId != 0)
        m_Bookmarks.push_back(mark);
    }
  }
}

bool CaptureContext::SaveNotes()
{
  // make sure this format matches SetCaptureFileComments in app_api.cpp if it changes
  QVariantMap root;
  for(const QString &key : m_Notes.keys())
    root[key] = m_Notes[key];

  QString json = VariantToJSON(root);

  SectionProperties props;
  props.type = SectionType::Notes;
  props.version = 1;

  ANALYTIC_SET(UIFeatures.CaptureComments, true);

  return Replay().GetCaptureFile()->WriteSection(props, json.toUtf8()).OK();
}

void CaptureContext::LoadNotes(const QString &data)
{
  QVariantMap root = JSONToVariant(data);

  for(QString key : root.keys())
  {
    if(!key.isEmpty())
      m_Notes[key] = root[key].toString();
  }
}

bool CaptureContext::SaveEdits()
{
  // make sure this format matches SetCaptureFileComments in app_api.cpp if it changes
  QVariantList editors;

  for(ShaderViewer *e : m_ShaderEditors)
  {
    QVariantMap editor = e->SaveEditor();
    if(!editor.isEmpty())
      editors.push_back(e->SaveEditor());
  }

  QVariantMap root;
  root[lit("editors")] = editors;

  QString json = VariantToJSON(root);

  SectionProperties props;
  props.type = SectionType::EditedShaders;
  props.version = 1;

  return Replay().GetCaptureFile()->WriteSection(props, json.toUtf8()).OK();
}

void CaptureContext::LoadEdits(const QString &data)
{
  QVariantMap root = JSONToVariant(data);

  QVariantList editors = root[lit("editors")].toList();

  auto replaceSaveCallback = [this](ICaptureContext *ctx, IShaderViewer *viewer, ResourceId id,
                                    ShaderStage stage, ShaderEncoding shaderEncoding,
                                    ShaderCompileFlags flags, rdcstr entryFunc, bytebuf shaderBytes) {
    ApplyShaderEdit(viewer, id, stage, shaderEncoding, flags, entryFunc, shaderBytes);
  };

  auto replaceRevertCallback = [this](ICaptureContext *ctx, IShaderViewer *view, ResourceId id) {
    RevertShaderEdit(view, id);
  };

  for(QVariant e : editors)
  {
    ShaderViewer *edit = ShaderViewer::LoadEditor(
        *this, e.toMap(), replaceSaveCallback, replaceRevertCallback,
        [this](ShaderViewer *view, bool closed) {
          SetModification(CaptureModifications::EditedShaders);
          if(closed)
            m_ShaderEditors.removeOne(view);
        },
        m_MainWindow->Widget());

    if(edit)
    {
      AddDockWindow(edit->Widget(), DockReference::MainToolArea, NULL);
      m_ShaderEditors.push_back(edit);
    }
  }
}

void CaptureContext::ClearReplayCache()
{
  m_CustomNameCachedID++;

  Replay().AsyncInvoke([](IReplayController *r) { r->ClearReplayCache(); });
}

bool CaptureContext::OpenRGPProfile(const rdcstr &filename)
{
  delete m_RGP;
  m_RGP = NULL;

  if(!m_CaptureLoaded)
  {
    RDDialog::critical(m_MainWindow, tr("Error opening RGP"),
                       tr("Can't open RGP profile with no capture open"));
    return false;
  }

  if(!m_StructuredFile)
  {
    RDDialog::critical(m_MainWindow, tr("Error opening RGP"),
                       tr("Open capture has no structured data - can't initialise RGP interop."));
    return false;
  }

  if(filename.isEmpty() || !QFileInfo(filename).exists())
  {
    RDDialog::critical(m_MainWindow, tr("Error opening RGP"),
                       tr("Invalid filename specified to open as RGP Profile\n%1\n"
                          "Please restart RenderDoc and try again.")
                           .arg(QString(filename)));
    return false;
  }

  QString RGPPath = m_Config.ExternalTool_RadeonGPUProfiler;

  while(!QFileInfo(RGPPath).exists())
  {
    QMessageBox::StandardButton res = RDDialog::question(
        m_MainWindow, tr("Error opening RGP"),
        tr("Path to RGP is incorrectly configured\n\nOpen settings window to configure path?"),
        QMessageBox::Yes | QMessageBox::No | QMessageBox::Cancel);

    if(res == QMessageBox::Cancel || res == QMessageBox::No)
      return false;

    SettingsDialog settings(*this, m_MainWindow);
    settings.focusItem(lit("ExternalTool_RadeonGPUProfiler"));
    RDDialog::show(&settings);

    RGPPath = m_Config.ExternalTool_RadeonGPUProfiler;
  }

  // Make sure the version of RGP specified supports interop. If not, bail.
  // Some older versions of RGP don't have command line support and will crash
  if(!RGPInterop::RGPSupportsInterop(RGPPath))
  {
    QMessageBox::StandardButton res = RDDialog::question(
        m_MainWindow, tr("RGP Version"),
        tr("This version of RGP does not support interop. Please download RGP v1.2 or newer."),
        QMessageBox::Ok);
    return false;
  }

  QString portStr = QString::number(RGPInterop::Port);

  if(!QProcess::startDetached(RGPPath, QStringList() << QString(filename) << portStr))
  {
    RDDialog::critical(m_MainWindow, tr("Error opening RGP"), tr("Failed to launch RGP."));
    return false;
  }

  m_RGP = new RGPInterop(*this);

  return true;
}

rdcstr CaptureContext::GetResourceNameUnsuffixed(ResourceId id) const
{
  if(id == ResourceId())
    return tr("No Resource");

  if(m_ReplacedToOrigResources.contains(id))
    return GetResourceName(m_ReplacedToOrigResources[id]);

  const ResourceDescription *desc = GetResource(id);

  if(desc)
    return GetResourceNameUnsuffixed(desc);

  uint64_t num;
  memcpy(&num, &id, sizeof(num));
  return tr("Unknown Resource %1").arg(num);
}

rdcstr CaptureContext::GetResourceNameUnsuffixed(const ResourceDescription *desc) const
{
  if(m_CustomNames.contains(desc->resourceId))
    return m_CustomNames[desc->resourceId];

  return desc->name;
}

rdcstr CaptureContext::GetResourceName(ResourceId id) const
{
  rdcstr ret = GetResourceNameUnsuffixed(id);

  if(m_OrigToReplacedResources.contains(id))
    ret += tr(" (Edited)");

  return ret;
}

bool CaptureContext::IsAutogeneratedName(ResourceId id)
{
  if(id == ResourceId())
    return true;

  if(m_CustomNames.contains(id))
    return false;

  const ResourceDescription *desc = GetResource(id);

  if(desc)
    return desc->autogeneratedName;

  return true;
}

bool CaptureContext::HasResourceCustomName(ResourceId id)
{
  return m_CustomNames.contains(id);
}

void CaptureContext::SetResourceCustomName(ResourceId id, const rdcstr &name)
{
  if(name.isEmpty())
  {
    if(m_CustomNames.contains(id))
      m_CustomNames.remove(id);
  }
  else
  {
    m_CustomNames[id] = name;
  }

  SetModification(CaptureModifications::Renames);

  CacheResources();

  RefreshUIStatus({}, true, true);
}

#if defined(RENDERDOC_PLATFORM_APPLE)
extern "C" void *makeNSViewMetalCompatible(void *handle);
#endif

WindowingData CaptureContext::CreateWindowingData(QWidget *window)
{
  if(!GUIInvoke::onUIThread())
    qCritical() << "CreateWindowingData called on non-UI thread";

#if defined(WIN32)

  return CreateWin32WindowingData((HWND)window->winId());

#elif defined(RENDERDOC_PLATFORM_LINUX)

  if(m_CurWinSystem == WindowingSystem::Wayland)
  {
    // we don't need this, we just need to force creation of a window handle
    window->winId();
    wl_surface *surface =
        (wl_surface *)AccessWaylandPlatformInterface("surface", window->windowHandle());
    return CreateWaylandWindowingData(m_WaylandDisplay, surface);
  }
  else if(m_CurWinSystem == WindowingSystem::XCB)
  {
    return CreateXCBWindowingData(m_XCBConnection, (xcb_window_t)window->winId());
  }
  else if(m_CurWinSystem == WindowingSystem::Xlib)
  {
    return CreateXlibWindowingData(m_X11Display, (Drawable)window->winId());
  }
  else
  {
    return CreateHeadlessWindowingData(1, 1);
  }

#elif defined(RENDERDOC_PLATFORM_APPLE)

  void *view = (void *)window->winId();

  void *layer = makeNSViewMetalCompatible(view);

  return CreateMacOSWindowingData(view, layer);

#else

#error "Unknown platform"

#endif
}

IMainWindow *CaptureContext::GetMainWindow()
{
  return m_MainWindow;
}

IEventBrowser *CaptureContext::GetEventBrowser()
{
  if(m_EventBrowser)
    return m_EventBrowser;

  m_EventBrowser = new EventBrowser(*this, m_MainWindow);
  m_EventBrowser->setObjectName(lit("eventBrowser"));
  setupDockWindow(m_EventBrowser, true);

  return m_EventBrowser;
}

BookmarksView *CaptureContext::GetBookmarksView()
{
  if(m_BookmarksView)
    return m_BookmarksView;

  m_BookmarksView = new BookmarksView(*this, m_MainWindow);
  // i18n-ignore: stable dock object name used only for layout persistence.
  m_BookmarksView->setObjectName(lit("bookmarksView"));
  setupDockWindow(m_BookmarksView, true);

  return m_BookmarksView;
}

IAPIInspector *CaptureContext::GetAPIInspector()
{
  if(m_APIInspector)
    return m_APIInspector;

  m_APIInspector = new APIInspector(*this, m_MainWindow);
  m_APIInspector->setObjectName(lit("apiInspector"));
  setupDockWindow(m_APIInspector, true);

  return m_APIInspector;
}

IAnnotationViewer *CaptureContext::GetAnnotationViewer()
{
  if(m_AnnotationViewer)
    return m_AnnotationViewer;

  m_AnnotationViewer = new AnnotationDisplay(*this, true, m_MainWindow);
  m_AnnotationViewer->setObjectName(lit("annotationViewer"));
  setupDockWindow(m_AnnotationViewer, true);

  return m_AnnotationViewer;
}

ITextureViewer *CaptureContext::GetTextureViewer()
{
  if(m_TextureViewer)
    return m_TextureViewer;

  m_TextureViewer = new TextureViewer(*this, m_MainWindow);
  m_TextureViewer->setObjectName(lit("textureViewer"));
  setupDockWindow(m_TextureViewer, true);

  return m_TextureViewer;
}

IBufferViewer *CaptureContext::GetMeshPreview()
{
  if(m_MeshPreview)
    return m_MeshPreview;

  m_MeshPreview = new BufferViewer(*this, true, m_MainWindow);
  m_MeshPreview->setObjectName(lit("meshPreview"));
  setupDockWindow(m_MeshPreview, true);

  return m_MeshPreview;
}

IPipelineStateViewer *CaptureContext::GetPipelineViewer()
{
  if(m_PipelineViewer)
    return m_PipelineViewer;

  m_PipelineViewer = new PipelineStateViewer(*this, m_MainWindow);
  m_PipelineViewer->setObjectName(lit("pipelineViewer"));
  setupDockWindow(m_PipelineViewer, true);

  return m_PipelineViewer;
}

ICaptureDialog *CaptureContext::GetCaptureDialog()
{
  if(m_CaptureDialog)
    return m_CaptureDialog;

  m_CaptureDialog = new CaptureDialog(
      *this,
      [this](const QString &exe, const QString &workingDir, const QString &cmdLine,
             const rdcarray<EnvironmentModification> &env, CaptureOptions opts,
             std::function<void(ICaptureConnection *)> callback) {
        return m_MainWindow->OnCaptureTrigger(exe, workingDir, cmdLine, env, opts, callback);
      },
      [this](uint32_t PID, const rdcarray<EnvironmentModification> &env, const QString &name,
             CaptureOptions opts, std::function<void(ICaptureConnection *)> callback) {
        return m_MainWindow->OnInjectTrigger(PID, env, name, opts, callback);
      },
      m_MainWindow, m_MainWindow);
  m_CaptureDialog->setObjectName(lit("capDialog"));
  m_CaptureDialog->setWindowIcon(*m_Icon);

  return m_CaptureDialog;
}

IDebugMessageView *CaptureContext::GetDebugMessageView()
{
  if(m_DebugMessageView)
    return m_DebugMessageView;

  m_DebugMessageView = new DebugMessageView(*this, m_MainWindow);
  m_DebugMessageView->setObjectName(lit("debugMessageView"));
  setupDockWindow(m_DebugMessageView, true);

  return m_DebugMessageView;
}

IDiagnosticLogView *CaptureContext::GetDiagnosticLogView()
{
  if(m_DiagnosticLogView)
    return m_DiagnosticLogView;

  m_DiagnosticLogView = new LogView(*this, m_MainWindow);
  m_DiagnosticLogView->setObjectName(lit("diagnosticLogView"));
  setupDockWindow(m_DiagnosticLogView, true);

  return m_DiagnosticLogView;
}

ICommentView *CaptureContext::GetCommentView()
{
  if(m_CommentView)
    return m_CommentView;

  m_CommentView = new CommentView(*this, m_MainWindow);
  m_CommentView->setObjectName(lit("commentView"));
  setupDockWindow(m_CommentView, true);

  return m_CommentView;
}

IPerformanceCounterViewer *CaptureContext::GetPerformanceCounterViewer()
{
  if(m_PerformanceCounterViewer)
    return m_PerformanceCounterViewer;

  m_PerformanceCounterViewer = new PerformanceCounterViewer(*this, m_MainWindow);
  m_PerformanceCounterViewer->setObjectName(lit("performanceCounterViewer"));
  setupDockWindow(m_PerformanceCounterViewer, true);

  return m_PerformanceCounterViewer;
}

IStatisticsViewer *CaptureContext::GetStatisticsViewer()
{
  if(m_StatisticsViewer)
    return m_StatisticsViewer;

  m_StatisticsViewer = new StatisticsViewer(*this, m_MainWindow);
  m_StatisticsViewer->setObjectName(lit("statisticsViewer"));
  setupDockWindow(m_StatisticsViewer, true);

  return m_StatisticsViewer;
}

ITimelineBar *CaptureContext::GetTimelineBar()
{
  if(m_TimelineBar)
    return m_TimelineBar;

  m_TimelineBar = new TimelineBar(*this, m_MainWindow);
  m_TimelineBar->setObjectName(lit("timelineBar"));
  setupDockWindow(m_TimelineBar, true);

  return m_TimelineBar;
}

IPythonShell *CaptureContext::GetPythonShell()
{
  if(m_PythonShell)
    return m_PythonShell;

  m_PythonShell = new PythonShell(*this, m_MainWindow);
  m_PythonShell->setObjectName(lit("pythonShell"));
  setupDockWindow(m_PythonShell, true);

  return m_PythonShell;
}

ApiMonitorWindow *CaptureContext::GetApiMonitorWindow()
{
  if(m_ApiMonitorWindow)
    return m_ApiMonitorWindow;

  m_ApiMonitorWindow = new ApiMonitorWindow(*this, m_MainWindow);
  m_ApiMonitorWindow->setObjectName(lit("apiMonitorWindow"));
  setupDockWindow(m_ApiMonitorWindow, true);

  return m_ApiMonitorWindow;
}

ReplayLabIntegration &CaptureContext::ReplayLab()
{
  Q_ASSERT(m_ReplayLabIntegration != NULL);
  return *m_ReplayLabIntegration;
}

void CaptureContext::ShowAIAssistant()
{
  m_MainWindow->showAIAssistant();
}

void CaptureContext::AddTextToAIAssistant(const rdcstr &title, const rdcstr &text,
                                           const rdcstr &source, const rdcstr &contextJSON)
{
  GetAIAssistant()->AddTextAttachment(title, text, source, true, contextJSON);
  ShowAIAssistant();
}

AIAssistant *CaptureContext::GetAIAssistant()
{
  if(m_AIAssistant)
    return m_AIAssistant;

  m_AIAssistant = new AIAssistant(*this, m_MainWindow);
  m_AIAssistant->setObjectName(lit("aiAssistant"));
  setupDockWindow(m_AIAssistant, true);

  return m_AIAssistant;
}

IResourceInspector *CaptureContext::GetResourceInspector()
{
  if(m_ResourceInspector)
    return m_ResourceInspector;

  m_ResourceInspector = new ResourceInspector(*this, m_MainWindow);
  m_ResourceInspector->setObjectName(lit("resourceInspector"));
  setupDockWindow(m_ResourceInspector, true);

  return m_ResourceInspector;
}

void CaptureContext::ShowEventBrowser()
{
  m_MainWindow->showEventBrowser();
}

void CaptureContext::ShowAPIInspector()
{
  m_MainWindow->showAPIInspector();
}

void CaptureContext::ShowAnnotationViewer()
{
  m_MainWindow->showAnnotationViewer();
}

void CaptureContext::ShowTextureViewer()
{
  m_MainWindow->showTextureViewer();
}

void CaptureContext::ShowMeshPreview()
{
  m_MainWindow->showMeshPreview();
}

void CaptureContext::ShowPipelineViewer()
{
  m_MainWindow->showPipelineViewer();
}

void CaptureContext::ShowCaptureDialog()
{
  m_MainWindow->showCaptureDialog();
}

void CaptureContext::ShowDebugMessageView()
{
  m_MainWindow->showDebugMessageView();
}

void CaptureContext::ShowDiagnosticLogView()
{
  m_MainWindow->showDiagnosticLogView();
}

void CaptureContext::ShowCommentView()
{
  m_MainWindow->showCommentView();
}

void CaptureContext::ShowPerformanceCounterViewer()
{
  m_MainWindow->showPerformanceCounterViewer();
}

void CaptureContext::ShowStatisticsViewer()
{
  m_MainWindow->showStatisticsViewer();
}

void CaptureContext::ShowTimelineBar()
{
  m_MainWindow->showTimelineBar();
}

void CaptureContext::ShowPythonShell()
{
  m_MainWindow->showPythonShell();
}

void CaptureContext::ShowResourceInspector()
{
  m_MainWindow->showResourceInspector();
}

IShaderViewer *CaptureContext::EditShader(ResourceId id, ShaderStage stage, const rdcstr &entryPoint,
                                          const rdcstrpairs &files, KnownShaderTool knownTool,
                                          ShaderEncoding shaderEncoding, ShaderCompileFlags flags,
                                          IShaderViewer::SaveCallback saveCallback,
                                          IShaderViewer::RevertCallback revertCallback)
{
  ShaderViewer *viewer = NULL;

  if(id != ResourceId())
  {
    auto replaceSaveCallback = [this, saveCallback](
                                   ICaptureContext *ctx, IShaderViewer *viewer, ResourceId id,
                                   ShaderStage stage, ShaderEncoding shaderEncoding,
                                   ShaderCompileFlags flags, rdcstr entryFunc, bytebuf shaderBytes) {
      ApplyShaderEdit(viewer, id, stage, shaderEncoding, flags, entryFunc, shaderBytes);

      if(saveCallback)
        saveCallback(ctx, viewer, id, stage, shaderEncoding, flags, entryFunc, shaderBytes);
    };

    auto replaceRevertCallback = [this, revertCallback](ICaptureContext *ctx, IShaderViewer *view,
                                                        ResourceId id) {
      RevertShaderEdit(view, id);

      if(revertCallback)
        revertCallback(ctx, view, id);
    };

    viewer = ShaderViewer::EditShader(
        *this, id, stage, entryPoint, files, knownTool, shaderEncoding, flags, replaceSaveCallback,
        replaceRevertCallback,
        [this](ShaderViewer *view, bool closed) {
          SetModification(CaptureModifications::EditedShaders);
          if(closed)
            m_ShaderEditors.removeOne(view);
        },
        m_MainWindow->Widget());

    m_ShaderEditors.push_back(viewer);
    SetModification(CaptureModifications::EditedShaders);
  }
  else
  {
    viewer =
        ShaderViewer::EditShader(*this, id, stage, entryPoint, files, knownTool, shaderEncoding,
                                 flags, saveCallback, revertCallback, NULL, m_MainWindow->Widget());
  }

  return viewer;
}

static bytebuf ReplayComparableDXBC(const bytebuf &bytes)
{
  bytebuf result;
  const auto word = [&bytes](size_t at) {
    return uint32_t(bytes[at]) | (uint32_t(bytes[at + 1]) << 8) | (uint32_t(bytes[at + 2]) << 16) |
           (uint32_t(bytes[at + 3]) << 24);
  };
  if(bytes.size() < 32 || memcmp(bytes.data(), "DXBC", 4) || word(24) != bytes.size())
    return result;
  const uint32_t count = word(28);
  if(!count || count > 64 || 32U + count * 4U > bytes.size())
    return result;
  bool executable = false;
  rdcarray<rdcpair<size_t, size_t>> ranges;
  for(uint32_t i = 0; i < count; i++)
  {
    const size_t offset = word(32 + i * 4);
    if(offset < 32U + count * 4U || offset > bytes.size() || bytes.size() - offset < 8)
      return {};
    const size_t length = word(offset + 4);
    if(length > bytes.size() - offset - 8)
      return {};
    const size_t end = offset + 8 + length;
    for(const auto &range : ranges)
      if(offset < range.second && range.first < end)
        return {};
    ranges.push_back({offset, end});
    const byte *tag = bytes.data() + offset;
    executable |= !memcmp(tag, "SHDR", 4) || !memcmp(tag, "SHEX", 4);
    // Keep every interface/root-signature/statistics/unknown chunk. Only debug payloads may carry
    // nondeterministic PDB identities; their source inputs are checked separately below.
    if(memcmp(tag, "SDBG", 4) && memcmp(tag, "SPDB", 4))
      result.append(bytes.data() + offset, end - offset);
  }
  if(!executable)
    result.clear();    // DXIL and unfamiliar containers retain exact-byte comparison.
  return result;
}

static bool SameReplayDXBCAndDebugInputs(const ShaderReflection &a, const ShaderReflection &b)
{
  if(a.encoding != ShaderEncoding::DXBC || b.encoding != ShaderEncoding::DXBC ||
     a.debugInfo.files.empty() || a.debugInfo.files != b.debugInfo.files ||
     a.debugInfo.compileFlags.flags != b.debugInfo.compileFlags.flags ||
     a.debugInfo.entrySourceName != b.debugInfo.entrySourceName ||
     !(a.debugInfo.entryLocation == b.debugInfo.entryLocation) ||
     a.debugInfo.encoding != b.debugInfo.encoding || a.debugInfo.compiler != b.debugInfo.compiler)
    return false;
  const bytebuf first = ReplayComparableDXBC(a.rawBytes);
  return !first.empty() && first == ReplayComparableDXBC(b.rawBytes);
}

#if ENABLE_UNIT_TESTS
TEST_CASE("Shader save reuse preserves executable, interface and debug source identity",
          "[capture-context][shader-save-cache]")
{
  ShaderReflection a, b;
  a.encoding = b.encoding = ShaderEncoding::DXBC;
  a.debugInfo.files.resize(1);
  a.debugInfo.files[0].filename = "owned.hlsl";
  a.debugInfo.files[0].contents = "float4 main():SV_Target{return 1;}";
  b.debugInfo = a.debugInfo;
  a.rawBytes.resize(72);
  memset(a.rawBytes.data(), 0, a.rawBytes.size());
  memcpy(a.rawBytes.data(), "DXBC", 4);
  a.rawBytes[24] = 72;
  a.rawBytes[28] = 3;
  a.rawBytes[32] = 44;
  a.rawBytes[36] = 53;
  a.rawBytes[40] = 62;
  for(size_t offset : {44U, 53U, 62U})
    a.rawBytes[offset + 4] = 1;
  memcpy(a.rawBytes.data() + 44, "SHDR", 4);
  memcpy(a.rawBytes.data() + 53, "RDEF", 4);
  memcpy(a.rawBytes.data() + 62, "SPDB", 4);
  b.rawBytes = a.rawBytes;
  b.rawBytes[70] = 1;
  CHECK(SameReplayDXBCAndDebugInputs(a, b));
  b.rawBytes[52] = 1;
  CHECK_FALSE(SameReplayDXBCAndDebugInputs(a, b));
  b.rawBytes = a.rawBytes;
  b.rawBytes[61] = 1;
  CHECK_FALSE(SameReplayDXBCAndDebugInputs(a, b));
  b.rawBytes = a.rawBytes;
  b.debugInfo.files[0].contents += "// changed source mapping";
  CHECK_FALSE(SameReplayDXBCAndDebugInputs(a, b));
  b.debugInfo = a.debugInfo;
  b.rawBytes[32] = 255;
  CHECK_FALSE(SameReplayDXBCAndDebugInputs(a, b));
  b.rawBytes = a.rawBytes;
  b.debugInfo.files.clear();
  CHECK_FALSE(SameReplayDXBCAndDebugInputs(a, b));
  b.debugInfo = a.debugInfo;
  ShaderCompileFlag flag;
  flag.name = "@cmdline";
  flag.value = "/Od";
  b.debugInfo.compileFlags.flags.push_back(flag);
  CHECK_FALSE(SameReplayDXBCAndDebugInputs(a, b));
}
#endif

void CaptureContext::ApplyShaderEdit(IShaderViewer *viewer, ResourceId id, ShaderStage stage,
                                     ShaderEncoding shaderEncoding, ShaderCompileFlags flags,
                                     const rdcstr &entryFunc, const bytebuf &shaderBytes)
{
  if(shaderBytes.isEmpty())
    return;

  ANALYTIC_SET(UIFeatures.ShaderEditing, true);

  QPointer<QObject> ptr(viewer->Widget());
  static const bool timing = qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1;
  QElapsedTimer inputTimer;
  if(timing)
    inputTimer.start();

  // invoke off to the ReplayController to replace the capture's shader
  // with our edited one
  Replay().AsyncInvoke([this, entryFunc, shaderBytes, shaderEncoding, flags, stage, id, ptr, viewer,
                        inputTimer](IReplayController *r) {
    rdcstr errs;
    const double queueMS = timing ? inputTimer.nsecsElapsed() / 1000000.0 : 0.0;
    QElapsedTimer workTimer;
    if(timing)
      workTimer.start();

    ResourceId from = id;
    ResourceId to;

    rdctie(to, errs) = r->BuildTargetShader(entryFunc, shaderEncoding, shaderBytes, flags, stage);
    const double compileMS = timing ? workTimer.nsecsElapsed() / 1000000.0 : 0.0;
    const bool compiled = to != ResourceId();
    bool reused = false;

    if(to != ResourceId() && (APIProps().pipelineType == GraphicsAPI::Vulkan ||
                              APIProps().pipelineType == GraphicsAPI::D3D12))
    {
      const ResourceId previous = r->GetTargetShaderReplacement(from);
      if(previous != ResourceId() && previous != to)
      {
        const ShaderEntryPoint entry(entryFunc, stage);
        const ShaderReflection *active = r->GetShader(ResourceId(), previous, entry);
        const ShaderReflection *candidate = r->GetShader(ResourceId(), to, entry);
        if(active && candidate && !active->rawBytes.empty() &&
           (active->rawBytes == candidate->rawBytes ||
            (APIProps().pipelineType == GraphicsAPI::D3D12 &&
             SameReplayDXBCAndDebugInputs(*active, *candidate))))
        {
          // Compilation still observes all current source/includes/options. Byte-identical
          // immutable modules can retain the admitted live target and its derived pipelines.
          // Release only this new build; the previous target may belong to a project/debug owner.
          r->FreeTargetResource(to);
          to = previous;
          reused = true;
        }
      }
    }
    const double compareEndMS = timing ? workTimer.nsecsElapsed() / 1000000.0 : 0.0;

    if(to == ResourceId())
    {
      if(APIProps().pipelineType == GraphicsAPI::D3D12)
      {
        rdcstr removeError = r->RemoveTargetShaderReplacementChecked(from);
        if(!removeError.empty())
        {
          // Keep the compiler diagnostic and add the transactional restore
          // diagnostic. The UI owner must remain registered while replay still
          // uses the previous target.
          if(!errs.empty())
            errs += "\n";
          errs += removeError;
        }
        else
        {
          // this GUIInvoke call always needs to go through even if the viewer has been closed.
          GUIInvoke::call(GetMainWindow()->Widget(),
                          [this, from]() { UnregisterReplacement(from); });
        }
      }
      else
      {
        r->RemoveReplacement(from);

        // this GUIInvoke call always needs to go through even if the viewer has been closed.
        GUIInvoke::call(GetMainWindow()->Widget(),
                        [this, from]() { UnregisterReplacement(from); });
      }
    }
    else
    {
      if(APIProps().pipelineType == GraphicsAPI::D3D12)
      {
        rdcstr replaceError = r->ReplaceTargetShaderChecked(from, to);
        if(!replaceError.empty())
        {
          if(!errs.empty())
            errs += "\n";
          errs += replaceError;
          r->FreeTargetResource(to);
        }
        else
        {
          GUIInvoke::call(GetMainWindow()->Widget(),
                          [this, from, to]() { RegisterReplacement(from, to); });
        }
      }
      else
      {
        r->ReplaceResource(from, to);

        GUIInvoke::call(GetMainWindow()->Widget(),
                        [this, from, to]() { RegisterReplacement(from, to); });
      }
    }
    const double nativeMS = timing ? workTimer.nsecsElapsed() / 1000000.0 : 0.0;
    if(ptr)
      GUIInvoke::call(ptr, [this, viewer, errs, inputTimer, queueMS, compileMS, compareEndMS,
                            nativeMS, compiled, reused]() {
        viewer->ShowErrors(errs);
        if(timing)
          LogEIDTiming(QString::asprintf(
              // i18n-ignore: opt-in profiling fields for native shader-save stages, not UI labels.
              "shader save: queue_ms=%.6f compile_ms=%.6f compare_ms=%.6f mutation_ms=%.6f "
              "native_ms=%.6f input_to_notice_ms=%.6f input_clock_ms=%lld compiled=%u "
              "reused_target=%u",
              queueMS, compileMS, compareEndMS - compileMS, nativeMS - compareEndMS, nativeMS,
              inputTimer.nsecsElapsed() / 1000000.0, inputTimer.msecsSinceReference(),
              compiled ? 1U : 0U, reused ? 1U : 0U));
      });
  });
}

void CaptureContext::RevertShaderEdit(IShaderViewer *viewer, ResourceId id)
{
  QPointer<QObject> ptr(viewer->Widget());

  // remove the replacement on close (we could make this more sophisticated if there
  // was a place to control replaced resources/shaders).
  Replay().AsyncInvoke([this, viewer, id, ptr](IReplayController *r) {
    rdcstr errs;
    if(IsCaptureLoaded())
    {
      if(APIProps().pipelineType == GraphicsAPI::D3D12)
        errs = r->RemoveTargetShaderReplacementChecked(id);
      else
        r->RemoveReplacement(id);
    }
    GUIInvoke::call(GetMainWindow()->Widget(), [this, viewer, id, ptr, errs] {
      if(errs.empty())
        UnregisterReplacement(id);
      if(ptr)
        viewer->ShowErrors(errs);
    });
  });
}

IShaderViewer *CaptureContext::DebugShader(const ShaderReflection *shader, ResourceId pipeline,
                                           ShaderDebugTrace *trace, const rdcstr &debugContext)
{
  return ShaderViewer::DebugShader(*this, shader, pipeline, trace, debugContext,
                                   m_MainWindow->Widget());
}

IShaderViewer *CaptureContext::ViewShader(const ShaderReflection *shader, ResourceId pipeline)
{
  return ShaderViewer::ViewShader(*this, shader, pipeline, m_MainWindow->Widget());
}

IShaderMessageViewer *CaptureContext::ViewShaderMessages(ShaderStageMask stages)
{
  return new ShaderMessageViewer(*this, stages, m_MainWindow);
}

IDescriptorViewer *CaptureContext::ViewDescriptorStore(ResourceId id)
{
  DescriptorViewer *viewer = new DescriptorViewer(*this, m_MainWindow);

  viewer->ViewDescriptorStore(id);

  return viewer;
}

IDescriptorViewer *CaptureContext::ViewDescriptors(const rdcarray<Descriptor> &descriptors,
                                                   const rdcarray<SamplerDescriptor> &samplerDescriptors)
{
  DescriptorViewer *viewer = new DescriptorViewer(*this, m_MainWindow);

  viewer->ViewDescriptors(descriptors, samplerDescriptors);

  return viewer;
}

IBufferViewer *CaptureContext::ViewBuffer(uint64_t byteOffset, uint64_t byteSize, ResourceId id,
                                          const rdcstr &format)
{
  BufferViewer *viewer = new BufferViewer(*this, false, m_MainWindow);

  viewer->ViewBuffer(byteOffset, byteSize, id, format);

  return viewer;
}

IBufferViewer *CaptureContext::ViewTextureAsBuffer(ResourceId id, const Subresource &sub,
                                                   const rdcstr &format)
{
  BufferViewer *viewer = new BufferViewer(*this, false, m_MainWindow);

  viewer->ViewTexture(id, sub, format);

  return viewer;
}

IBufferViewer *CaptureContext::ViewConstantBuffer(ShaderStage stage, uint32_t slot, uint32_t idx)
{
  BufferViewer *existing = BufferViewer::HasCBufferView(stage, slot, idx);
  if(existing != NULL)
    return existing;

  BufferViewer *viewer = new BufferViewer(*this, false, m_MainWindow);

  viewer->ViewCBuffer(stage, slot, idx);

  return viewer;
}

IPixelHistoryView *CaptureContext::ViewPixelHistory(ResourceId texID, uint32_t x, uint32_t y,
                                                    uint32_t view, const TextureDisplay &display)
{
  return new PixelHistoryView(*this, texID, QPoint(x, y), view, display, m_MainWindow);
}

QWidget *CaptureContext::CreateBuiltinWindow(const rdcstr &objectName)
{
  if(objectName == "textureViewer")
  {
    return GetTextureViewer()->Widget();
  }
  else if(objectName == "eventBrowser")
  {
    return GetEventBrowser()->Widget();
  }
  else if(objectName == "bookmarksView")
  {
    return GetBookmarksView();
  }
  else if(objectName == "pipelineViewer")
  {
    return GetPipelineViewer()->Widget();
  }
  else if(objectName == "meshPreview")
  {
    return GetMeshPreview()->Widget();
  }
  else if(objectName == "apiInspector")
  {
    return GetAPIInspector()->Widget();
  }
  else if(objectName == "annotationViewer")
  {
    return GetAnnotationViewer()->Widget();
  }
  else if(objectName == "capDialog")
  {
    return GetCaptureDialog()->Widget();
  }
  else if(objectName == "debugMessageView")
  {
    return GetDebugMessageView()->Widget();
  }
  else if(objectName == "commentView")
  {
    return GetCommentView()->Widget();
  }
  else if(objectName == "statisticsViewer")
  {
    return GetStatisticsViewer()->Widget();
  }
  else if(objectName == "timelineBar")
  {
    return GetTimelineBar()->Widget();
  }
  else if(objectName == "pythonShell")
  {
    return GetPythonShell()->Widget();
  }
  else if(objectName == "apiMonitorWindow")
  {
    return GetApiMonitorWindow()->Widget();
  }
  else if(objectName == "aiAssistant")
  {
    return GetAIAssistant()->Widget();
  }
  else if(objectName == "resourceInspector")
  {
    return GetResourceInspector()->Widget();
  }
  else if(objectName == "performanceCounterViewer")
  {
    return GetPerformanceCounterViewer()->Widget();
  }

  return NULL;
}

void CaptureContext::BuiltinWindowClosed(QWidget *window)
{
  if(m_EventBrowser && m_EventBrowser->Widget() == window)
    m_EventBrowser = NULL;
  else if(m_BookmarksView && m_BookmarksView == window)
    m_BookmarksView = NULL;
  else if(m_TextureViewer && m_TextureViewer->Widget() == window)
    m_TextureViewer = NULL;
  else if(m_CaptureDialog && m_CaptureDialog->Widget() == window)
    m_CaptureDialog = NULL;
  else if(m_APIInspector && m_APIInspector->Widget() == window)
    m_APIInspector = NULL;
  else if(m_PipelineViewer && m_PipelineViewer->Widget() == window)
    m_PipelineViewer = NULL;
  else if(m_MeshPreview && m_MeshPreview->Widget() == window)
    m_MeshPreview = NULL;
  else if(m_DebugMessageView && m_DebugMessageView->Widget() == window)
    m_DebugMessageView = NULL;
  else if(m_DiagnosticLogView && m_DiagnosticLogView->Widget() == window)
    m_DiagnosticLogView = NULL;
  else if(m_CommentView && m_CommentView->Widget() == window)
    m_CommentView = NULL;
  else if(m_StatisticsViewer && m_StatisticsViewer->Widget() == window)
    m_StatisticsViewer = NULL;
  else if(m_TimelineBar && m_TimelineBar->Widget() == window)
    m_TimelineBar = NULL;
  else if(m_PythonShell && m_PythonShell->Widget() == window)
    m_PythonShell = NULL;
  else if(m_ApiMonitorWindow && m_ApiMonitorWindow->Widget() == window)
    m_ApiMonitorWindow = NULL;
  else if(m_AIAssistant && m_AIAssistant->Widget() == window)
    m_AIAssistant = NULL;
  else if(m_ResourceInspector && m_ResourceInspector->Widget() == window)
    m_ResourceInspector = NULL;
  else if(m_PerformanceCounterViewer && m_PerformanceCounterViewer->Widget() == window)
    m_PerformanceCounterViewer = NULL;
  else
    qCritical() << "Unrecognised window being closed: " << window;
}

void CaptureContext::setupDockWindow(QWidget *shad, bool hide)
{
  shad->setWindowIcon(*m_Icon);
  if(hide)
    shad->hide();
}

void CaptureContext::AddExtensionWatches(rdcstr filePath)
{
  QDir dir(filePath);

  // ignore directories with no python files
  // i18n-ignore: Filesystem pattern or sentinel filename for Python extensions.
  QStringList pyfiles = dir.entryList(QStringList() << lit("*.py"));
  if(pyfiles.empty())
    return;

  // don't watch the directory itself. New files will not be usable and we will already catch
  // renames.
  // m_ExtensionWatcher->addPath(filePath);
  for(QString pyfile : pyfiles)
    m_ExtensionWatcher->addPath(dir.absoluteFilePath(pyfile));

  for(QString subdir : dir.entryList(QStringList(), QDir::Dirs | QDir::NoDotAndDotDot))
    AddExtensionWatches(dir.absoluteFilePath(subdir));
}

void CaptureContext::ExtensionTouched(const QString &extensionPath)
{
  // find the root extension dir containing this path
  for(const ExtensionMetadata &m : GetInstalledExtensions())
  {
    if(extensionPath.contains(m.filePath))
    {
      // remove all watched paths underneath
      QStringList paths = m_ExtensionWatcher->directories();
      paths.append(m_ExtensionWatcher->files());
      for(QString path : paths)
      {
        if(path.contains(m.filePath))
          m_ExtensionWatcher->removePath(path);
      }

      // mark it as 'dirty' / needing reload
      m_DirtyExtensions.push_back(m.package);
    }
  }
}

void CaptureContext::RaiseDockWindow(QWidget *dockWindow)
{
  ToolWindowManager::raiseToolWindow(dockWindow);
}

void CaptureContext::AddDockWindow(QWidget *newWindow, DockReference ref, QWidget *refWindow,
                                   float percentage)
{
  if(!newWindow)
  {
    qCritical() << "Unexpected NULL newWindow in AddDockWindow";
    return;
  }
  setupDockWindow(newWindow, false);

  ToolWindowManager *mainManager = m_MainWindow->mainToolManager();
  auto dock = [newWindow, mainManager](ToolWindowManager *manager,
                                       const ToolWindowManager::AreaReference &area) {
    if(manager->toolWindows().contains(newWindow))
    {
      // Repeated show/restore callbacks must not register or reparent the same tab again.
      if(area.type() == ToolWindowManager::AddTo && area.area() &&
         manager->areaOf(newWindow) == area.area())
        ToolWindowManager::raiseToolWindow(newWindow);
      else
        manager->moveToolWindow(newWindow, area);
      return;
    }

    // A widget may be detached while still registered. Never register it with two managers:
    // both managers would otherwise retain ownership and eventually destroy the same widget.
    ToolWindowManager *owner = mainManager->toolWindows().contains(newWindow)
                                   ? mainManager
                                   : ToolWindowManager::managerOf(newWindow);
    if(owner && owner != manager && owner->toolWindows().contains(newWindow))
    {
      if(owner->areaOf(newWindow))
        ToolWindowManager::raiseToolWindow(newWindow);
      else
        owner->moveToolWindow(newWindow, ToolWindowManager::LastUsedArea);
      return;
    }
    manager->addToolWindow(newWindow, area);
  };

  if(ref == DockReference::MainToolArea)
  {
    dock(mainManager, m_MainWindow->mainToolArea());
    return;
  }
  else if(ref == DockReference::LeftToolArea)
  {
    dock(mainManager, m_MainWindow->leftToolArea());
    return;
  }
  else if(ref == DockReference::NewFloatingArea)
  {
    dock(mainManager, ToolWindowManager::NewFloatingArea);
    return;
  }
  else if(ref == DockReference::LastUsedArea)
  {
    dock(mainManager, ToolWindowManager::LastUsedArea);
    return;
  }
  else if(ref == DockReference::NoArea)
  {
    // Detaching a window does not require a reference window or a surviving dock area.
    ToolWindowManager *owner = ToolWindowManager::managerOf(newWindow);
    dock(owner && owner->toolWindows().contains(newWindow) ? owner : mainManager,
         ToolWindowManager::NoArea);
    return;
  }
  else if(ref == DockReference::EmptySpace)
  {
    dock(mainManager, ToolWindowManager::EmptySpace);
    return;
  }

  ToolWindowManager *manager = refWindow ? ToolWindowManager::managerOf(refWindow) : NULL;
  if(manager && newWindow == refWindow && manager->areaOf(newWindow))
  {
    ToolWindowManager::raiseToolWindow(newWindow);
    return;
  }
  if(!manager || !manager->areaOf(refWindow))
  {
    // Layout restore detaches tool windows before reattaching surviving areas. Python panel
    // callbacks may retain an undocked reference, so use the stable main manager as fallback.
    dock(mainManager, m_MainWindow->mainToolArea());
    return;
  }

  if(ref == DockReference::TransientPopupArea)
  {
    BufferViewer *buf = qobject_cast<BufferViewer *>(newWindow);

    if(buf && buf->IsCBufferView())
    {
      for(BufferViewer *cb : manager->findChildren<BufferViewer *>())
      {
        if(cb != buf && cb->IsCBufferView() && ToolWindowManager::managerOf(cb) == manager &&
           manager->areaOf(cb))
        {
          dock(manager,
               ToolWindowManager::AreaReference(ToolWindowManager::AddTo, manager->areaOf(cb)));
          return;
        }
      }

      // With no other docked CBuffer view, keep this view's existing location.
      if(ToolWindowManager::managerOf(buf) == manager && manager->areaOf(buf))
        return;
    }

    if(qobject_cast<PixelHistoryView *>(newWindow))
    {
      for(PixelHistoryView *hist : manager->findChildren<PixelHistoryView *>())
      {
        if(hist != newWindow && ToolWindowManager::managerOf(hist) == manager && manager->areaOf(hist))
        {
          dock(manager,
               ToolWindowManager::AreaReference(ToolWindowManager::AddTo, manager->areaOf(hist)));
          return;
        }
      }
    }

    ref = DockReference::RightOf;
  }

  ToolWindowManager::AreaReference areaRef((ToolWindowManager::AreaReferenceType)ref,
                                           manager->areaOf(refWindow), percentage);
  dock(manager, areaRef);
}

IShaderCodeEditor *CaptureContext::CreateShaderCodeEditor(QWidget *parent)
{
  if(!m_ShaderCodeEditorService)
  {
    m_ShaderCodeEditorService = new CodeEditorService(m_MainWindow);
    m_ShaderCodeEditorService->setDiagnosticsHandler(
        [this](ScintillaEdit *source, const QList<CodeEditorService::Diagnostic> &diagnostics) {
          int errors = 0, warnings = 0, firstLine = 1;
          QStringList messages;
          for(const CodeEditorService::Diagnostic &diagnostic : diagnostics)
          {
            if(diagnostic.severity == CodeEditorService::DiagnosticSeverity::Error)
              errors++;
            else if(diagnostic.severity == CodeEditorService::DiagnosticSeverity::Warning)
              warnings++;
            else
              continue;
            if(messages.isEmpty())
              firstLine = diagnostic.line + 1;
            if(messages.size() < 12)
              messages.push_back(diagnostic.message);
          }
          for(ShaderCodeEditor *editor : m_ShaderCodeEditors)
            if(editor && editor->Editor() == source)
              editor->SetDiagnosticSummary(errors, warnings, messages.join(QLatin1Char('\n')), firstLine);
        });
    if(!Config().ShaderEditor_LanguageServer.isEmpty())
      m_ShaderCodeEditorService->setLanguageServerCommand(Config().ShaderEditor_LanguageServer);
    m_ShaderCodeEditorService->setDefinitionHandler(
        [this](const CodeEditorService::Location &location) {
          if(location.editor)
          {
            for(ShaderCodeEditor *editor : m_ShaderCodeEditors)
            {
              if(editor && editor->Editor() == location.editor)
              {
                // Let the workspace select the owning tab before moving a cursor
                // in an editor that may currently be hidden in its stack.
                if(!location.fileName.isEmpty())
                  editor->OpenDefinition(location.fileName, location.line, location.column);
                editor->GoToPosition(location.position);
                return;
              }
            }
          }

          ShaderCodeEditor *active = NULL;
          QWidget *focus = QApplication::focusWidget();
          for(ShaderCodeEditor *editor : m_ShaderCodeEditors)
          {
            if(editor && focus && (focus == editor || editor->isAncestorOf(focus)))
            {
              active = editor;
              break;
            }
          }
          if(!active && !m_ShaderCodeEditors.isEmpty())
            active = m_ShaderCodeEditors.front();
          if(active && !location.fileName.isEmpty())
            active->OpenDefinition(location.fileName, location.line, location.column);
        });
    m_ShaderCodeEditorService->setStatusHandler([this](const QString &message) {
      QWidget *focus = QApplication::focusWidget();
      for(ShaderCodeEditor *editor : m_ShaderCodeEditors)
      {
        if(editor && (!focus || focus == editor || editor->isAncestorOf(focus)))
        {
          editor->SetStatusMessage(message);
          if(focus)
            break;
        }
      }
    });
  }

  ShaderCodeEditor *editor =
      new ShaderCodeEditor(m_ShaderCodeEditorService, parent ? parent : m_MainWindow);
  m_ShaderCodeEditors.push_back(editor);
  QObject::connect(editor, &QObject::destroyed, m_MainWindow,
                   [this, editor](QObject *) { m_ShaderCodeEditors.removeAll(editor); });
  return editor;
}

void CaptureContext::DestroyShaderCodeEditor(IShaderCodeEditor *editor)
{
  ShaderCodeEditor *nativeEditor = dynamic_cast<ShaderCodeEditor *>(editor);
  if(!nativeEditor || !m_ShaderCodeEditors.contains(nativeEditor))
    return;

  nativeEditor->ClearCallbacks();
  m_ShaderCodeEditors.removeAll(nativeEditor);
  PythonContext::InvalidateNativeQtObject(nativeEditor);
  delete nativeEditor;
}

void CaptureContext::EmbedDependentFiles()
{
  if(!m_Replay.GetCaptureAccess())
    return;

  // Always operate on the capture access (local or remote)
  QString tag = lit("replayEmbedDependenciesIntoCapture");
  bool done = false;

  Replay().AsyncInvoke(tag, [this, &done](IReplayController *) {
    m_Replay.GetCaptureAccess()->EmbedDependenciesIntoCapture();
    done = true;
  });

  // wait a short while before displaying the progress dialog
  for(int i = 0; !done && (i < 100 || m_Replay.GetCurrentProcessingTag().isEmpty() ||
                           m_Replay.GetCurrentProcessingTag() == tag);
      i++)
    QThread::msleep(5);

  ShowProgressDialog(m_MainWindow->Widget(), tr("Please wait, working..."),
                     [&done]() { return done; });

  // Local replay
  if(m_Replay.GetCaptureFile())
    return;

  // Remote capture : no local capture file
  if(IsCaptureTemporary())
    return;

  // Local capture file being replayed remotely : copy back from the remote
  if(IsCaptureLocal())
  {
    Replay().CopyCaptureFromRemote(m_RemoteFile, m_CaptureFile, m_MainWindow);

    if(!QFile::exists(m_CaptureFile))
    {
      RDDialog::critical(m_MainWindow, tr("Failed to save capture"),
                         tr("Capture couldn't be copied from remote."));
      return;
    }
  }
}

void CaptureContext::RemoveDependentFiles()
{
  if(!m_Replay.GetCaptureAccess())
    return;

  // Always operate on the capture access (local or remote)
  QString tag = lit("replayRemoveDependenciesFromCapture");
  bool done = false;

  Replay().AsyncInvoke(tag, [this, &done](IReplayController *) {
    m_Replay.GetCaptureAccess()->RemoveDependenciesFromCapture();
    done = true;
  });

  // wait a short while before displaying the progress dialog
  for(int i = 0; !done && (i < 100 || m_Replay.GetCurrentProcessingTag().isEmpty() ||
                           m_Replay.GetCurrentProcessingTag() == tag);
      i++)
    QThread::msleep(5);

  ShowProgressDialog(m_MainWindow->Widget(), tr("Please wait, working..."),
                     [&done]() { return done; });

  // Local replay
  if(m_Replay.GetCaptureFile())
    return;

  // Remote capture : no local capture file
  if(IsCaptureTemporary())
    return;

  // Local capture file being replayed remotely : copy back from the remote
  if(IsCaptureLocal())
  {
    Replay().CopyCaptureFromRemote(m_RemoteFile, m_CaptureFile, m_MainWindow);

    if(!QFile::exists(m_CaptureFile))
    {
      RDDialog::critical(m_MainWindow, tr("Failed to save capture"),
                         tr("Capture couldn't be copied from remote."));
      return;
    }
  }
}
