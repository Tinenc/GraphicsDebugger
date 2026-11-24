#include "Windows/AI/AIConversationView.h"
#include "Code/Resources.h"
#include <QApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QTextBrowser>
#include <QTextDocument>
#include <cstdio>
#include <algorithm>

int main(int argc,char **argv) {
 QApplication app(argc,argv);
 Resources::Initialise();
 AIConversationView left,right;left.resize(700,800);right.resize(700,800);left.show();right.show();
 AIChatMessage message;message.id=QStringLiteral("bench");message.role=QStringLiteral("user");message.text=QStringLiteral("Benchmark task");message.state=QStringLiteral("running");message.timestampMs=QDateTime::currentMSecsSinceEpoch();message.activityExpanded=true;
 for(int i=0;i<2000;i++){AIActivityItem item;item.id=QString::number(i);item.kind=QStringLiteral("tool");item.title=QStringLiteral("Event %1").arg(i);item.detail=QStringLiteral("{\"eventId\":%1,\"status\":\"completed\",\"source\":\"retained immutable capture data\"}").arg(i);item.completed=true;message.activity.push_back(item);}
 QVector<qint64> timings;
 for(int i=0;i<6;i++){
  AIActivityItem item;item.id=QStringLiteral("new-%1").arg(i);item.kind=QStringLiteral("tool");item.title=QStringLiteral("New event %1").arg(i);item.completed=true;message.activity.push_back(item);
  QElapsedTimer timer;timer.start();left.setMessages({message});right.setMessages({message});app.processEvents();timings.push_back(timer.elapsed());
 }
 auto browser=left.findChild<QTextBrowser *>();int before=browser->document()->toPlainText().count(QStringLiteral("Event "));
 QMetaObject::invokeMethod(browser,"anchorClicked",Qt::DirectConnection,Q_ARG(QUrl,QUrl(QStringLiteral("replaylab-activity://more/bench"))));app.processEvents();
 int after=browser->document()->toPlainText().count(QStringLiteral("Event "));
 std::sort(timings.begin(),timings.end());
 QJsonObject result{{"twoViews",true},{"activities",message.activity.size()},{"medianLayoutMs",double(timings[timings.size()/2])},{"maximumLayoutMs",double(timings.last())},{"visibleHistoryRows",before},{"visibleAfterMore",after}};
 const QByteArray json=QJsonDocument(result).toJson(QJsonDocument::Compact);std::puts(json.constData());
 if(argc>1&&QString::fromLocal8Bit(argv[1])==QStringLiteral("--expect-paging")&&!(before<120&&after>before))return 2;
 return 0;
}
