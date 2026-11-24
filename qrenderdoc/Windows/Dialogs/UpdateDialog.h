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

#pragma once

#include <QDialog>
#include <QElapsedTimer>
#include <QTemporaryDir>
#include <QUrl>
#include <functional>
#include <memory>

namespace Ui
{
class UpdateDialog;
}

class QProcess;
class QTimer;

struct BreadRelease
{
  QString version;
  QString notesHtml;
  QString notesText;
  QUrl page;
  QUrl download;
  QString sha256;
  qint64 size = 0;

  static QUrl RepositoryPage();
  static QUrl ReleasesPage();
  static QString InstalledVersion();
  static bool InstallationInProgress();
  static BreadRelease FromGitHub(const QByteArray &json);
  static void Check(QObject *owner, const QString &installedVersion,
                    std::function<void(QByteArray, QString)> completed);
};

class UpdateDialog : public QDialog
{
  Q_OBJECT
public:
  UpdateDialog(const QString &installedVersion, const BreadRelease &release, QWidget *parent);
  ~UpdateDialog();

  std::unique_ptr<QTemporaryDir> TakeStaging() { return std::move(m_Staging); }
  static bool StartInstaller(const QTemporaryDir &staging, const QString &installedVersion,
                             const BreadRelease &release);

private slots:
  void on_releaseNotes_clicked();
  void on_update_clicked();

private:
  void reject() override;
  void DownloadFailed(const QString &error);
  void StartDownload();

  Ui::UpdateDialog *ui;
  BreadRelease m_Release;
  QProcess *m_Download;
  QTimer *m_ProgressTimer;
  QElapsedTimer m_DownloadTimer;
  std::unique_ptr<QTemporaryDir> m_Staging;
  bool m_Cancelled = false;
  int m_DownloadAttempts = 0;
};
