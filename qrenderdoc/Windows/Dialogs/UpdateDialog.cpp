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

#include "UpdateDialog.h"
#include <QApplication>
#include <QDesktopServices>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QRegularExpression>
#include <QTextBrowser>
#include <QTimer>
#include <QVersionNumber>
#include <QXmlStreamReader>
#include "Code/BackgroundProcess.h"
#include "Code/QRDUtils.h"
#include "ui_UpdateDialog.h"

#if defined(Q_OS_WIN)
#include <windows.h>
#endif

namespace
{
bool IsReleaseVersion(const QString &version)
{
  const QVersionNumber parsed = QVersionNumber::fromString(version);
  return parsed.segmentCount() == 3 && parsed.toString() == version;
}

class ReleaseNotesBrowser : public QTextBrowser
{
public:
  using QTextBrowser::QTextBrowser;

protected:
  QVariant loadResource(int, const QUrl &) override
  {
    // Release notes must not load remote images or read local files.
    return QVariant();
  }
};

QString SystemProgram(const QString &path)
{
  return QDir(QString::fromLocal8Bit(qgetenv("SystemRoot")))
      .filePath(lit("System32/") + path);    // i18n-ignore: Windows system executable directory.
}
}

QUrl BreadRelease::RepositoryPage()
{
  // i18n-ignore: Canonical GitHub source repository URL.
  return QUrl(lit("https://github.com/Straw1997/renderdocBread"));
}

QUrl BreadRelease::ReleasesPage()
{
  return QUrl(RepositoryPage().toString() + lit("/releases"));    // i18n-ignore: GitHub URL path.
}

bool BreadRelease::InstallationInProgress()
{
#if defined(Q_OS_WIN)
  wchar_t image[32768];
  const DWORD length = GetModuleFileNameW(nullptr, image, ARRAY_COUNT(image));
  if(length == 0 || length == ARRAY_COUNT(image))
    return false;

  const QDir app = QFileInfo(QString::fromWCharArray(image, int(length))).dir();
  const QString lock = app.filePath(QLatin1String(".bread-update.lock"));
  return GetFileAttributesW(reinterpret_cast<LPCWSTR>(lock.utf16())) != INVALID_FILE_ATTRIBUTES;
#else
  return false;
#endif
}

QString BreadRelease::InstalledVersion()
{
  const QDir app(QCoreApplication::applicationDirPath());
  QFile manifest(
      app.filePath(lit("RenderDocBread.manifest.json")));    // i18n-ignore: Release manifest key,
                                                             // marker or schema identifier.
  if(!app.exists(lit("RenderDocBread.portable")) ||          // i18n-ignore: GitHub API value.
     !manifest.open(
         QIODevice::ReadOnly))    // i18n-ignore: Release manifest key, marker or schema identifier.
    return QString();

  const QJsonObject package = QJsonDocument::fromJson(manifest.readAll()).object();
  const QString version =
      package
          .value(lit("breadVersion"))    // i18n-ignore: GitHub API value.
          .toString();    // i18n-ignore: Release manifest key, marker or schema identifier.
  if(package.value(lit("schema")).toString() ==
         lit("renderdocbread-release/v1") &&    // i18n-ignore: Release manifest key, marker or schema identifier.
     package.value(lit("product")).toString() ==
         lit("RenderDocBread") &&    // i18n-ignore: Release manifest key, marker or schema identifier.
     package.value(lit("platform")).toString() ==
         lit("windows-x64") &&    // i18n-ignore: Release manifest key, marker or schema identifier.
     package.value(lit("profile")).toString() == lit("standard") &&
     IsReleaseVersion(version))    // i18n-ignore: Release manifest key, marker or schema identifier.
    return version;

  return QString();
}

BreadRelease BreadRelease::FromGitHub(const QByteArray &json)
{
  BreadRelease release;
  const QJsonObject info = QJsonDocument::fromJson(json).object();
  // i18n-ignore: Internal marker for the public GitHub website fallback.
  const bool publicMetadata = info.value(lit("bread_public_release")).toBool();
  const QString tag = info.value(lit("tag_name"))    // i18n-ignore: GitHub API value.
                          .toString();               // i18n-ignore: GitHub API value.
  if(info.value(lit("draft")).toBool(true) ||
     info.value(lit("prerelease")).toBool(true) ||    // i18n-ignore: GitHub release API field, path
                                                      // or validation pattern.
     !tag.startsWith(QLatin1Char('v')) || !IsReleaseVersion(tag.mid(1)))
    return release;

  release.version = tag.mid(1);
  const QString page = ReleasesPage().toString();
  release.page = QUrl(page + lit("/tag/") +                  // i18n-ignore: GitHub API value.
                      tag);                                  // i18n-ignore: GitHub API value.
  release.notesHtml = info.value(lit("body_html"))           // i18n-ignore: GitHub API value.
                          .toString();                       // i18n-ignore: GitHub API value.
  release.notesText = info.value(lit("body")).toString();    // i18n-ignore: GitHub release API
                                                             // field, path or validation pattern.

  const QRegularExpression namePattern(
      lit("^RenderDocBread-[0-9][0-9.-]*-win-x64\\.zip$"));    // i18n-ignore: GitHub release API
                                                               // field, path or validation pattern.
  const QRegularExpression digestPattern(
      lit("^sha256:([0-9a-f]{64})$"));    // i18n-ignore: GitHub API value.
  for(const QJsonValue &value :
      info.value(lit("assets")).toArray())    // i18n-ignore: GitHub release API field, path or
                                              // validation pattern.
  {
    const QJsonObject asset = value.toObject();
    const QString name = asset
                             .value(lit("name"))    // i18n-ignore: update protocol.
                             .toString();           // i18n-ignore: GitHub API value.
    const QString url = page + lit("/download/") + tag +
                        QLatin1Char('/') +    // i18n-ignore: update protocol.
                        name;                 // i18n-ignore: GitHub API value.
    const auto digest = digestPattern.match(
        asset.value(lit("digest")).toString());    // i18n-ignore: GitHub release API field, path or
                                                   // validation pattern.
    const double size =
        asset.value(lit("size")).toDouble(-1);    // i18n-ignore: GitHub release API
                                                  // field, path or validation pattern.
    if(namePattern.match(name).hasMatch() &&
       asset.value(lit("state")).toString() ==    // i18n-ignore: update protocol.
           lit("uploaded") &&                     // i18n-ignore: GitHub API value.
       asset.value(lit("browser_download_url")).toString() == url &&
       digest.hasMatch() &&    // i18n-ignore: GitHub API value.
       (size > 0 || (publicMetadata && size == 0)) && size < 1.0e12)
    {
      // An ambiguous installer should be selected on the release page.
      if(!release.download.isEmpty())
      {
        release.download = QUrl();
        break;
      }
      release.download = QUrl(url);
      release.sha256 = digest.captured(1);
      release.size = qint64(size);
    }
  }
  return release;
}

namespace
{
using ReleaseReply = std::function<void(QByteArray, QString)>;
constexpr qint64 MaxUnlistedArchiveBytes = 2LL * 1024 * 1024 * 1024;

bool HasProxyEnvironment()
{
  return !qgetenv("HTTPS_PROXY").isEmpty() || !qgetenv("https_proxy").isEmpty() ||
         !qgetenv("ALL_PROXY").isEmpty() || !qgetenv("all_proxy").isEmpty();
}

bool IsTransientCurlError(int code)
{
  // curl exit codes for DNS, connection, partial transfer, timeout, TLS handshake and I/O errors.
  return code == 5 || code == 6 || code == 7 || code == 18 || code == 28 || code == 35 ||
         code == 52 || code == 55 || code == 56 || code == 92;
}

void Request(QProcess *process, const QUrl &url, const QStringList &options, bool direct)
{
  ConfigureBackgroundProcess(process);
  QStringList args = {lit("-q"),        // i18n-ignore: update protocol.
                      lit("--fail"),    // i18n-ignore: curl argument.
                      lit("--silent"),
                      lit("--show-error"),    // i18n-ignore: curl argument.
                      lit("--location"),
                      lit("--proto"),    // i18n-ignore: curl argument.
                      lit("=https"),
                      lit("--proto-redir"),    // i18n-ignore: curl argument.
                      lit("=https"),
                      lit("--connect-timeout"),    // i18n-ignore: curl argument.
                      lit("20")};                  // i18n-ignore: curl argument.
  if(direct)
    args << lit("--noproxy") << lit("*");    // i18n-ignore: Curl direct route options.
  args.append(options);
  args.append(url.toString());
  process->start(SystemProgram(lit("curl.exe")),    // i18n-ignore: update protocol.
                 args);                             // i18n-ignore: curl argument.
}

void FetchMetadata(QObject *owner, const QUrl &url, QStringList options, ReleaseReply reply)
{
  // Bound every metadata request, including redirects to release assets.
  options << QLatin1String("--connect-timeout") << QLatin1String("6") << QLatin1String("--max-time")
          << QLatin1String("12") << QLatin1String("--max-filesize") << QLatin1String("2097152");
  QProcess *process = new QProcess(owner);
  auto complete = [process, reply](const QByteArray &data, const QString &error) {
    QObject *owner = process->parent();
    process->deleteLater();
    // Deliver outside QProcess signals so callers can safely create the next request or show a dialog.
    QTimer::singleShot(0, owner, [reply, data, error]() { reply(data, error); });
  };

  QObject::connect(process, &QProcess::errorOccurred, process,
                   [process, complete](QProcess::ProcessError error) {
                     if(error == QProcess::FailedToStart)
                       complete(QByteArray(), process->errorString());
                   });
  QObject::connect(
      process, OverloadedSlot<int, QProcess::ExitStatus>::of(&QProcess::finished), process,
      [process, complete, url, options, attempt = 0](int code, QProcess::ExitStatus status) mutable {
        const QByteArray data = process->readAllStandardOutput();
        const QString error = QString::fromLocal8Bit(process->readAllStandardError()).trimmed();
        const bool proxy = HasProxyEnvironment();
        const bool direct = proxy && attempt == 0;
        const bool retryPublic = status == QProcess::NormalExit && IsTransientCurlError(code) &&
                                 attempt < (proxy ? 2 : 1) &&
                                 url.host() == BreadRelease::RepositoryPage().host();
        if(status == QProcess::NormalExit && code == 0)
          complete(data, QString());
        else if(direct || retryPublic)
        {
          ++attempt;
          if(direct)
            qInfo() << "Retrying the GitHub update request on a direct connection.";
          QTimer::singleShot(250, process, [process, url, options, direct]() {
            Request(process, url, options, direct);
          });
        }
        else
          complete(QByteArray(), error.isEmpty() ? process->errorString() : error);
      });
  Request(process, url, options, false);
}

QString FeedNotes(const QByteArray &data, const QUrl &releasePage)
{
  QXmlStreamReader xml(data);
  while(!xml.atEnd())
  {
    xml.readNext();
    if(!xml.isStartElement() || xml.name() != QLatin1String("entry"))
      continue;

    QString notes;
    bool matches = false;
    while(xml.readNextStartElement())
    {
      if(xml.name() == QLatin1String("content"))
      {
        notes = xml.readElementText();
        continue;
      }

      if(xml.name() == QLatin1String("link") &&
         xml.attributes().value(QLatin1String("href")) == releasePage.toString())
        matches = true;
      xml.skipCurrentElement();
    }

    if(matches)
      return notes;
  }
  return QString();
}

QJsonObject PublicMetadata(const QString &version, const QString &notes = QString())
{
  // Keep cached responses compatible with the release dialog parser.
  return {{QLatin1String("tag_name"), QLatin1Char('v') + version},
          {QLatin1String("draft"), false},
          {QLatin1String("prerelease"), false},
          {QLatin1String("body_html"), notes},
          {QLatin1String("bread_public_release"), true}};
}

void FetchPublicAsset(QObject *owner, const QString &version, const QString &notes, ReleaseReply reply)
{
  // i18n-ignore: Canonical Windows x64 package asset name.
  const QString name = lit("RenderDocBread-%1-win-x64.zip").arg(version);
  const QString download = BreadRelease::ReleasesPage().toString() + QLatin1String("/download/v") +
                           version + QLatin1Char('/') + name;
  // i18n-ignore: Canonical companion file for the exact release asset.
  FetchMetadata(owner, QUrl(download + lit(".sha256")), {},
                [version, notes, name, download, reply](QByteArray sha, QString) {
                  QJsonObject release = PublicMetadata(version, notes);
                  // i18n-ignore: SHA-256 sidecar format.
                  const QRegularExpression line(lit("^([0-9a-f]{64})[ \\t]+\\*?") +
                                                QRegularExpression::escape(name) + QLatin1Char('$'));
                  const auto match = line.match(QString::fromLatin1(sha).trimmed());
                  if(match.hasMatch())
                  {
                    // The public fallback can lack a byte count. The installer still verifies the full SHA-256.
                    const QJsonObject asset{
                        {QLatin1String("name"), name},
                        {QLatin1String("browser_download_url"), download},
                        {QLatin1String("state"), QLatin1String("uploaded")},
                        {QLatin1String("size"), 0},
                        {QLatin1String("digest"), QLatin1String("sha256:") + match.captured(1)}};
                    release[QLatin1String("assets")] = QJsonArray{asset};
                  }
                  reply(QJsonDocument(release).toJson(QJsonDocument::Compact), QString());
                });
}

void FetchPublicRelease(QObject *owner, const QString &installedVersion, const QString &apiError,
                        ReleaseReply reply)
{
  // The web Latest redirect remains available when the anonymous REST quota is exhausted.
  const QString releases = BreadRelease::ReleasesPage().toString();
  const QUrl latest(releases + QLatin1String("/latest"));
  const QString tagPrefix = releases + QLatin1String("/tag/v");
  const QStringList options{QLatin1String("--output"), QLatin1String("NUL"),
                            QLatin1String("--write-out"), QLatin1String("%{url_effective}")};
  FetchMetadata(
      owner, latest, options,
      [owner, installedVersion, apiError, reply, releases, tagPrefix](QByteArray data, QString error) {
        const QString destination = QString::fromUtf8(data).trimmed();
        const QString version = destination.mid(tagPrefix.size());
        if(!destination.startsWith(tagPrefix, Qt::CaseInsensitive) || !IsReleaseVersion(version))
        {
          reply(QByteArray(), error.isEmpty() ? apiError : error);
          return;
        }

        if(QVersionNumber::fromString(version) <= QVersionNumber::fromString(installedVersion))
        {
          reply(QJsonDocument(PublicMetadata(version)).toJson(QJsonDocument::Compact), QString());
          return;
        }

        const QUrl page(tagPrefix + version);
        const QUrl feed(releases + QLatin1String(".atom"));
        FetchMetadata(owner, feed, {}, [owner, page, version, reply](QByteArray data, QString) {
          FetchPublicAsset(owner, version, FeedNotes(data, page), reply);
        });
      });
}

}

void BreadRelease::Check(QObject *owner, const QString &installedVersion, ReleaseReply completed)
{
  QUrl api = ReleasesPage();
  // i18n-ignore: GitHub API host and public latest release route.
  api.setHost(lit("api.github.com"));
  api.setPath(lit("/repos") + api.path() + lit("/latest"));    // i18n-ignore: GitHub API URL path.
  // i18n-ignore: GitHub media type for rendered release notes.
  FetchMetadata(owner, api, {lit("--header"), lit("Accept: application/vnd.github.full+json")},
                [owner, installedVersion, completed](QByteArray data, QString error) {
                  if(!FromGitHub(data).version.isEmpty())
                    completed(data, QString());
                  else
                    FetchPublicRelease(owner, installedVersion, error, completed);
                });
}

UpdateDialog::UpdateDialog(const QString &installedVersion, const BreadRelease &release,
                           QWidget *parent)
    : QDialog(parent), ui(new Ui::UpdateDialog), m_Release(release)
{
  ui->setupUi(this);
  setWindowFlags(windowFlags() & ~Qt::WindowContextHelpButtonHint);
  setWindowTitle(tr("Update Available - v%1").arg(release.version));
  ui->updateVer->setText(windowTitle());
  ui->currentVersion->setText(QFormatStr("v%1").arg(installedVersion));
  ui->newVersion->setText(QFormatStr("v%1").arg(release.version));

  ReleaseNotesBrowser *notes = new ReleaseNotesBrowser(this);
  ui->verticalLayout_3->insertWidget(0, notes, 1);
  notes->setMinimumHeight(250);
  notes->setAccessibleName(tr("Release notes"));
  notes->setOpenLinks(false);
  notes->document()->setBaseUrl(release.page);
  QObject::connect(notes, &QTextBrowser::anchorClicked, this, [release](const QUrl &url) {
    const QUrl resolved = release.page.resolved(url);
    if(resolved.scheme() ==
       lit("https"))    // i18n-ignore: Update staging archive filename or URL scheme.
      QDesktopServices::openUrl(resolved);
  });
  if(!release.notesHtml.trimmed().isEmpty())
    notes->setHtml(release.notesHtml);
  else if(!release.notesText.trimmed().isEmpty())
    notes->setPlainText(release.notesText);
  else
    notes->setPlainText(tr("This release has no notes."));

  ui->progressBar->hide();
  ui->progressText->hide();
  ui->update->setEnabled(!release.download.isEmpty());
  if(release.download.isEmpty())
  {
    ui->progressText->setText(tr("This release does not have a verifiable Windows x64 package."));
    ui->progressText->show();
  }
  if(release.size > 0 && !release.download.isEmpty())
  {
    ui->downloadSize->setText(tr("%1 MB").arg(double(release.size) / 1000000.0, 0, 'f', 2));
  }
  else
  {
    ui->downloadSize->setText(tr("See release page"));
  }

  QObject::connect(ui->close, &QPushButton::clicked, this, &UpdateDialog::reject);
  m_Download = new QProcess(this);
  m_ProgressTimer = new QTimer(this);
  m_ProgressTimer->setInterval(250);
  QObject::connect(m_ProgressTimer, &QTimer::timeout, this, [this]() {
    const qint64 size =
        QFileInfo(m_Staging->filePath(lit("update.zip")))    // i18n-ignore: update protocol.
            .size();    // i18n-ignore: Update staging archive filename or URL scheme.
    if(size > 0 && m_Release.size > 0)
      UpdateTransferProgress(size, m_Release.size, &m_DownloadTimer, ui->progressBar,
                             ui->progressText, tr("Downloading update..."));
  });
  QObject::connect(m_Download, &QProcess::errorOccurred, this, [this](QProcess::ProcessError err) {
    if(err == QProcess::FailedToStart)
      DownloadFailed(m_Download->errorString());
  });
  QObject::connect(
      m_Download, OverloadedSlot<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
      [this](int code, QProcess::ExitStatus status) {
        m_ProgressTimer->stop();
        if(m_Cancelled)
        {
          QDialog::reject();
          return;
        }

        const qint64 size = QFileInfo(m_Staging->filePath(QLatin1String("update.zip"))).size();
        const qint64 maxSize = m_Release.size > 0 ? m_Release.size : MaxUnlistedArchiveBytes;
        if(status != QProcess::NormalExit || code != 0)
        {
          const QString error = QString::fromLocal8Bit(m_Download->readAllStandardError()).trimmed();
          const int http = m_Download->readAllStandardOutput().trimmed().toInt();
          const bool retryHttp = code == 22 && (http == 403 || http == 408 || http == 429 ||
                                                (http >= 500 && http <= 504));
          if(status == QProcess::NormalExit && (IsTransientCurlError(code) || retryHttp) &&
             m_DownloadAttempts < 3 && size < maxSize)
          {
            qInfo() << "Retrying the Bread update download from byte" << size;
            QTimer::singleShot(500, this, &UpdateDialog::StartDownload);
          }
          else
            DownloadFailed(error);
          return;
        }
        if(size <= 0 || size > maxSize || (m_Release.size > 0 && size != m_Release.size))
        {
          DownloadFailed(tr("The downloaded package has an unexpected size."));
          return;
        }
        accept();
      });
  ui->close->setFocus();
  resize(680, 590);
}

UpdateDialog::~UpdateDialog()
{
  m_ProgressTimer->stop();
  QObject::disconnect(m_Download, nullptr, this, nullptr);
  // Stop the downloader before the staging directory is destroyed on window close.
  if(m_Download->state() != QProcess::NotRunning)
  {
    m_Download->kill();
    m_Download->waitForFinished();
  }
  delete ui;
}

void UpdateDialog::reject()
{
  if(m_Download->state() != QProcess::NotRunning)
  {
    m_Cancelled = true;
    m_Download->kill();
    return;
  }

  QDialog::reject();
}

void UpdateDialog::on_releaseNotes_clicked()
{
  QDesktopServices::openUrl(m_Release.page);
}

void UpdateDialog::DownloadFailed(const QString &error)
{
  m_ProgressTimer->stop();
  m_Staging.reset();
  ui->progressBar->hide();
  ui->progressText->setText(tr("Download failed.\n%1").arg(error));
  ui->update->setEnabled(true);
  ui->update->setText(tr("Retry Update"));
  ui->close->setText(tr("Close"));
}

void UpdateDialog::on_update_clicked()
{
  if(RDDialog::question(
         this, tr("Update RenderDocBread"),
         tr("Download and install this update? RenderDocBread will close and "
            "restart after the download. Save any work in progress before continuing."),
         QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes)
    return;

  const QDir app(QCoreApplication::applicationDirPath());
  m_Staging.reset(new QTemporaryDir(app.filePath(
      lit(".bread-update-XXXXXX"))));    // i18n-ignore: Temporary directory name template.
  ui->progressText->show();
  if(!m_Staging->isValid())
  {
    DownloadFailed(tr("Could not create a staging folder in the installation directory."));
    return;
  }

  ui->progressBar->show();
  ui->progressBar->setRange(0, m_Release.size > 0 ? 10000 : 0);
  ui->progressBar->setValue(0);
  ui->progressText->setText(tr("Downloading update..."));
  ui->close->setText(tr("Cancel"));
  ui->update->setEnabled(false);
  m_DownloadAttempts = 0;
  m_DownloadTimer.start();
  StartDownload();
}

void UpdateDialog::StartDownload()
{
  if(!isVisible())
    return;

  const bool direct = ++m_DownloadAttempts == 2 && HasProxyEnvironment();
  m_ProgressTimer->start();
  const QString archive = m_Staging->filePath(QLatin1String("update.zip"));
  const qint64 offset = QFileInfo(archive).size();
  const qint64 maxSize = m_Release.size > 0 ? m_Release.size : MaxUnlistedArchiveBytes;
  QStringList options{QLatin1String("--speed-time"),      QLatin1String("45"),
                      QLatin1String("--speed-limit"),     QLatin1String("1024"),
                      QLatin1String("--connect-timeout"), QLatin1String("8"),
                      QLatin1String("--max-filesize"),    QString::number(maxSize - offset),
                      QLatin1String("--write-out"),       QLatin1String("%{http_code}"),
                      QLatin1String("--output"),          QDir::toNativeSeparators(archive)};
  if(offset > 0)
    options << QLatin1String("--continue-at") << QLatin1String("-");
  Request(m_Download, m_Release.download, options, direct);
}

bool UpdateDialog::StartInstaller(const QTemporaryDir &staging, const QString &installedVersion,
                                  const BreadRelease &release)
{
  QFile script(lit(":/ApplyBreadUpdate.ps1"));    // i18n-ignore: updater argument.
  const QString path =
      staging.filePath(lit("ApplyBreadUpdate.ps1"));    // i18n-ignore: updater argument.
  if(!script.copy(path))
    return false;

  // Pass the active UI language to the independent Windows installer.
  const QJsonObject progress{
      {QLatin1String("title"), tr("Installing RenderDocBread update")},
      {QLatin1String("waiting"), tr("Waiting for RenderDocBread to close...")},
      {QLatin1String("verify"), tr("Verifying the downloaded package...")},
      {QLatin1String("extract"), tr("Extracting and verifying update files...")},
      {QLatin1String("close"), tr("Closing background processes...")},
      {QLatin1String("check"), tr("Checking files in the current installation...")},
      {QLatin1String("replace"), tr("Installing the new version...")},
      {QLatin1String("restart"), tr("Opening the updated application...")},
      {QLatin1String("clean"), tr("Cleaning up temporary update files...")},
      {QLatin1String("rollback"), tr("Restoring the previous installation...")},
      {QLatin1String("restore"), tr("Opening the previous version...")},
      {QLatin1String("failed"), tr("The update could not complete. See the installer log at %1.")},
  };
  const QByteArray progressText = QJsonDocument(progress).toJson(QJsonDocument::Compact);
  QFile labels(staging.filePath(QLatin1String("progress.json")));
  if(!labels.open(QIODevice::WriteOnly) || labels.write(progressText) != progressText.size())
    return false;
  labels.close();

  const QString shell = SystemProgram(
      lit("WindowsPowerShell/v1.0/powershell.exe"));    // i18n-ignore: Installer resource path or
                                                        // PowerShell argument.
  const QStringList arguments = {lit("-NoProfile"),     // i18n-ignore: updater argument.
                                 lit("-NonInteractive"),     // i18n-ignore: updater argument.
                                 lit("-WindowStyle"),        // i18n-ignore: updater argument.
                                 lit("Hidden"),              // i18n-ignore: updater argument.
                                 lit("-ExecutionPolicy"),    // i18n-ignore: updater argument.
                                 lit("Bypass"),              // i18n-ignore: updater argument.
                                 lit("-File"),               // i18n-ignore: updater argument.
                                 QDir::toNativeSeparators(path),
                                 lit("-ParentProcessId"),    // i18n-ignore: updater argument.
                                 QString::number(QCoreApplication::applicationPid()),
                                 lit("-InstalledVersion"),    // i18n-ignore: updater argument.
                                 installedVersion,
                                 lit("-TargetVersion"),    // i18n-ignore: updater argument.
                                 release.version,
                                 lit("-Sha256"),    // i18n-ignore: updater argument.
                                 release.sha256};
  return QProcess::startDetached(shell, arguments);
}
