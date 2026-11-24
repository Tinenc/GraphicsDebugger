"""Execute the production queue scheduler with transient bridge discovery loss."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Qt/MSVC fixture")
def test_frozen_queue_waits_for_bridge_without_rebinding_or_blocking_other_chats(tmp_path, monkeypatch):
    from setuptools._distutils.ccompiler import new_compiler
    source = (ROOT / "qrenderdoc/Windows/AI/AIAssistant.cpp").read_text(encoding="utf-8")
    scheduler = source.split("void AIAssistant::startNextChat()", 1)[1].split("void AIAssistant::launchChat(", 1)[0]
    discard = source.split("void AIAssistant::discardChatContextFreeze(PendingChat &request)", 1)[1].split("void AIAssistant::discardAllChatContextFreezes()", 1)[0]
    timing = "\n".join(re.findall(r"const (?:qint64|int) ChatBridgeReadiness\w+ = [^;]+;", source))
    source = r'''
#include <QCoreApplication>
#include <QDateTime>
#include <QFile>
#include <QHash>
#include <QJsonDocument>
#include <QJsonObject>
#include <QObject>
#include <QSet>
#include <QString>
#include <QTimer>
#include <QVector>
''' + timing + r'''
int maxConcurrentAIChats(){return 2;}
class AIAssistant: public QObject {
public:
struct PendingChat {
  QString taskId,messageId,conversationId,contextInstanceId,requestedCaptureSessionId,contextFreezeJobId;
  QByteArray contextSnapshot;
  qint64 requestedEventId=0,requestedSelectedEventId=0,launchBridgeReadinessDeadlineMs=0;
  bool contextFreezeRequired=false;
};
struct ChatExecution{PendingChat request;};
struct Context{bool IsCaptureLoaded(){return true;}}m_Ctx;
bool m_ShuttingDown=false,m_ChatLaunchRetryScheduled=false,bridgeReady=false;
QHash<QString,ChatExecution*>m_ChatExecutions;
QVector<PendingChat>m_ChatQueue,launched;
QHash<QString,QString>m_UserPrompts,m_RunningTasks,m_TaskOffsets,m_TaskSequences,m_TaskTails,
 m_TaskTexts,m_TaskAgentMessageStreams,m_TaskConversationIds,m_TaskMessageIds;
bool findBridgeBinding(QString*instance,QString*session){*instance="same";*session="session";return bridgeReady;}
bool conversation(const QString&){return true;}
bool message(const QString&,const QString&){return true;}
void launchChat(PendingChat request){launched.push_back(request);}
void scheduleConversationSave(){}
void unwatchTask(const QString&){}
void advanceChatContextFreeze(){}
void updateComposerState(){}
void startNextChat();
void discardChatContextFreeze(PendingChat &request);
};
void AIAssistant::startNextChat()
''' + scheduler + '\nvoid AIAssistant::discardChatContextFreeze(PendingChat &request)\n' + discard + r'''
int main(int argc,char**argv){
 QCoreApplication app(argc,argv);
 AIAssistant ai;
 AIAssistant::PendingChat first;
 first.taskId="frozen";first.conversationId="one";first.contextSnapshot="frozen";
 first.contextInstanceId="same";first.requestedCaptureSessionId="session";
 first.requestedEventId=882;first.requestedSelectedEventId=658;
 auto second=first;second.taskId="later";
 AIAssistant::PendingChat independent;independent.taskId="free";independent.conversationId="two";
 ai.m_ChatQueue={first,second,independent};ai.startNextChat();
 QJsonObject result;
 result["keptFrozenFifo"]=ai.m_ChatQueue.size()==2 && ai.m_ChatQueue[0].taskId=="frozen" && ai.m_ChatQueue[1].taskId=="later";
 result["otherChatRuns"]=ai.launched.size()==1 && ai.launched[0].taskId=="free";
 result["boundedWait"]=ai.m_ChatQueue[0].launchBridgeReadinessDeadlineMs>QDateTime::currentMSecsSinceEpoch();
 ai.bridgeReady=true;ai.startNextChat();
 auto resumed=ai.launched[1];
 result["sameFrozenIdentity"]=resumed.taskId=="frozen" && resumed.contextInstanceId=="same" &&
  resumed.requestedCaptureSessionId=="session" && resumed.requestedEventId==882 && resumed.requestedSelectedEventId==658;
 result["oneTurnPerChat"]=ai.m_ChatQueue.size()==1 && ai.m_ChatQueue[0].taskId=="later";
 ai.bridgeReady=false;ai.m_ChatQueue[0].launchBridgeReadinessDeadlineMs=QDateTime::currentMSecsSinceEpoch()-1;
 ai.startNextChat();
 result["expiredWaitReachesLaunchValidation"]=ai.m_ChatQueue.isEmpty() && ai.launched.last().taskId=="later";
 auto edited=first;edited.launchBridgeReadinessDeadlineMs=1;
 ai.discardChatContextFreeze(edited);
 result["newFreezeGetsNewWait"]=edited.launchBridgeReadinessDeadlineMs==0 && edited.contextSnapshot.isEmpty();
 QFile output(QString::fromLocal8Bit(argv[1]));output.open(QIODevice::WriteOnly);
 output.write(QJsonDocument(result).toJson(QJsonDocument::Compact));
}
'''
    (tmp_path / "queue.cpp").write_text(source, encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    qt = ROOT / "qrenderdoc/3rdparty/qt"
    with monkeypatch.context() as context:
        context.chdir(tmp_path)
        objects = compiler.compile(["queue.cpp"], output_dir=str(tmp_path),
            include_dirs=[str(qt / "include"), str(qt / "include/QtCore")],
            extra_postargs=["/EHsc", "/std:c++14", "/MD", "/DQT_NO_DEBUG"])
        compiler.link_executable(objects, str(tmp_path / "queue"), libraries=["Qt5Core"],
                                 library_dirs=[str(qt / "x64/lib")], target_lang="c++")
    env = dict(os.environ)
    env["PATH"] = str(qt / "x64/bin") + os.pathsep + env.get("PATH", "")
    report = tmp_path / "result.json"
    subprocess.run([str(tmp_path / "queue.exe"), str(report)], env=env, check=True,
                   capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result and all(result.values()), result
