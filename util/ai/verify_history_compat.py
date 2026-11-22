"""Compile the actual Qt history store and background-process code in OS temp."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[2]
SOURCE = r'''
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QProcess>
#include <windows.h>
#include "AIConversationStore.h"
#include "AIConversationMerge.h"
#include "BackgroundProcess.h"
QString root;
QString ConfigFilePath(const QString &name) { return QDir(root).filePath(name); }
void require(bool value, const char *why) { if(!value) qFatal("%s", why); }
QByteArray bytes(const QString &path) { QFile f(path); require(f.open(QIODevice::ReadOnly), "read fixture"); return f.readAll(); }
QJsonObject row(const char *id, const char *text) { return {{"id",id},{"text",text},{"timestampUnixMs","123"}}; }
QByteArray payload(const QJsonArray &messages, const char *id="old") {
 return QJsonDocument(QJsonObject{{"schema","replaylab-ai-conversations/v5"},
 {"activeConversationId",id},{"conversations",QJsonArray{QJsonObject{{"id",id},
 {"updatedUnixMs","456"},{"messages",messages}}}}}).toJson(QJsonDocument::Compact);
}
int main(int argc, char **argv) {
 QCoreApplication app(argc,argv);
 if(app.arguments().contains("--child")) { printf("%s",GetConsoleWindow()==NULL?"hidden":"console"); return 0; }
 const QString local=QString::fromWCharArray(_wgetenv(L"LOCALAPPDATA"));
 const QString roaming=QString::fromWCharArray(_wgetenv(L"APPDATA"));
 const QString capture=QDir(local).filePath(QString::fromUtf8("抓帧/t3.rdc"));
 QString error;
 root=QDir(roaming).filePath("q_render_soco_doc");
 require(AIConversationStore::Save(capture,payload(QJsonArray{row("one","original")}),&error),"legacy save");
 const QString oldPath=AIConversationStore::StoragePathForCapture(capture);
 const QByteArray oldBytes=bytes(oldPath);
 root=QDir(local).filePath("RenderDocBread/ui");
 require(AIConversationStore::Save(capture,payload(QJsonArray{},"blank"),&error),"blank portable save");
 QByteArray restored;
 require(AIConversationStore::Load(capture,&restored,&error)&&error.isEmpty(),"restore legacy into blank portable");
 QJsonObject doc=QJsonDocument::fromJson(restored).object();
 require(doc.value("conversations").toArray().size()==2,"retain old plus new thread");
 require(doc.value("activeConversationId").toString()=="old","select recovered active chat");
 require(bytes(oldPath)==oldBytes,"legacy originals unchanged");
 QByteArray second;
 require(AIConversationStore::Load(capture,&second,&error)&&second==restored,"idempotent second load");
 const QJsonObject merged=AIConversationMerge::merge(QJsonDocument::fromJson(payload(QJsonArray{row("two","new")})).object(),
 QJsonDocument::fromJson(payload(QJsonArray{row("one","old")})).object());
 require(merged.value("conversations").toArray()[0].toObject().value("messages").toArray().size()==2,"same thread preserves disjoint messages");
 const QJsonObject base=QJsonDocument::fromJson(payload(QJsonArray{row("one","old")})).object();
 const QJsonObject remote=AIConversationMerge::merge(QJsonDocument::fromJson(payload(QJsonArray{row("remote","remote")},"remote-thread")).object(),base);
 const QJsonObject own=QJsonDocument::fromJson(payload(QJsonArray{row("one","old"),row("local","local")})).object();
 const QJsonObject concurrent=AIConversationMerge::concurrent(own,base,remote);
 require(concurrent.value("conversations").toArray().size()==2,"concurrent independent threads preserved");
 QJsonObject empty=base;empty["conversations"]=QJsonArray{};
 const QJsonObject deleted=AIConversationMerge::concurrent(base,base,empty);
 require(deleted.value("conversations").toArray().isEmpty(),"stale writer cannot resurrect deleted chat");
 root=QDir(local).filePath("explicit-private-profile");
 require(AIConversationStore::Load(capture,&second,&error)&&second.isEmpty(),"isolated profile must not inherit");
 root=QDir(local).filePath("RenderDocBread/ui");
 require(AIConversationStore::Load(capture+".different",&second,&error)&&second.isEmpty(),"different capture is separate");
 const QString current=AIConversationStore::StoragePathForCapture(capture);
 QFile corrupt(current);require(corrupt.open(QIODevice::WriteOnly),"write corrupt fixture");corrupt.write("invalid");corrupt.close();
 require(!AIConversationStore::Load(capture,&second,&error)&&!error.isEmpty(),"corrupt primary is never overwritten");
 require(bytes(current)=="invalid","corrupt original preserved");
 const QString largeCapture=QDir(local).filePath("large-history.rdc");
 const auto attachmentRow=[](const char *id, QChar byte) {
   QJsonObject message=row(id,"image attached");
   message["attachments"]=QJsonArray{QJsonObject{{"content",QString(3 * 1024 * 1024, byte)}}};
   return message;
 };
 const QJsonObject largeOlder=attachmentRow("older",QLatin1Char('A'));
 const QJsonObject largeNewer=attachmentRow("newer",QLatin1Char('B'));
 root=QDir(roaming).filePath("q_render_soco_doc");
 require(AIConversationStore::Save(largeCapture,payload(QJsonArray{largeOlder}),&error),"large legacy save");
 const QString largeLegacy=AIConversationStore::StoragePathForCapture(largeCapture);
 const QByteArray originalLargeLegacy=bytes(largeLegacy);
 root=QDir(local).filePath("RenderDocBread/ui");
 require(AIConversationStore::Save(largeCapture,payload(QJsonArray{largeNewer}),&error),"large active save");
 require(AIConversationStore::Load(largeCapture,&second,&error)&&error.isEmpty(),"merge beyond 4 MiB");
 const auto combined=QJsonDocument::fromJson(second).object()["conversations"].toArray()[0].toObject()["messages"].toArray();
 require(combined==QJsonArray{largeOlder,largeNewer}&&second.size()>6*1024*1024,"preserve both attachment contents");
 require(bytes(largeLegacy)==originalLargeLegacy,"preserve large legacy original");
 require(AIConversationStore::Load(largeCapture,&restored,&error)&&restored==second,"reopen enlarged archive");
 QProcess child;ConfigureBackgroundProcess(&child);child.start(app.applicationFilePath(),QStringList{"--child"});
 require(child.waitForFinished(5000),"background process finished");
 require(child.exitCode()==0&&child.readAllStandardOutput()=="hidden","actual child has no console");
 printf("{\"ok\":true,\"checks\":[\"legacy-restore\",\"old-and-new-preserved\",\"idempotent-load\",\"same-thread-merge\",\"concurrent-thread-preservation\",\"stale-deletion-preservation\",\"isolated-profile\",\"capture-separation\",\"corrupt-preservation\",\"large-archive-and-legacy-attachments\",\"hidden-native-child\"]}");
 return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    vswhere = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
    vs = subprocess.check_output([str(vswhere), "-latest", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
    with tempfile.TemporaryDirectory(prefix="bread-native-history-") as work:
        folder = Path(work)
        shim = folder / "include/Code/Interface/QRDInterface.h"
        shim.parent.mkdir(parents=True)
        shim.write_text('#include <QString>\nQString ConfigFilePath(const QString &filename);\n')
        (folder / "test.cpp").write_text(SOURCE, encoding="utf-8")
        qt = REPO / "qrenderdoc/3rdparty/qt"
        command = ["cl.exe", "/nologo", "/EHsc", "/MD", "/utf-8", "/std:c++17", "/DQT_NO_DEBUG",
                   "/I"+str(folder / "include"), "/I"+str(qt / "include"), "/I"+str(qt / "include/QtCore"),
                   "/I"+str(REPO / "qrenderdoc/Windows/AI"), "/I"+str(REPO / "qrenderdoc/Code"),
                   str(folder / "test.cpp"), str(REPO / "qrenderdoc/Windows/AI/AIConversationStore.cpp"),
                   "/Fe:"+str(folder / "test.exe"), "/link", str(qt / "x64/lib/Qt5Core.lib")]
        (folder / "compile.cmd").write_text('@echo off\ncall "'+vs+'\\VC\\Auxiliary\\Build\\vcvars64.bat" >nul\n'+subprocess.list2cmdline(command)+'\n', encoding="utf-8")
        result = subprocess.run(["cmd.exe", "/d", "/c", str(folder / "compile.cmd")], cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stdout[-4000:] + result.stderr[-2000:])
        env = dict(os.environ, PATH=str(qt / "x64/bin")+os.pathsep+os.environ["PATH"],
                   LOCALAPPDATA=str(folder / "local"), APPDATA=str(folder / "roaming"))
        for name in ("local", "roaming"):
            (folder / name).mkdir()
        result = subprocess.run([str(folder / "test.exe")], cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stdout[-2000:] + result.stderr[-2000:])
        value = json.loads(result.stdout)
    args.receipt.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(value))


if __name__ == "__main__":
    main()
