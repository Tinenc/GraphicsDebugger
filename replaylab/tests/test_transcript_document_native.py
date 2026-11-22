"""Exercise the production transcript renderer with its shipped Qt 5.9 libraries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Production Windows Qt/MSVC fixture")
def test_transcript_styles_documents_and_live_updates(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler

    qt = ROOT / "qrenderdoc/3rdparty/qt"
    objects = ROOT / "x64/Development/obj/qrenderdoc"
    dependencies = [objects / (name + ".obj") for name in
                    ("AIFileThumbnail", "AIVisualReply", "Resources", "qrc_resources", "precompiled")]
    if not (qt / "x64/lib/Qt5Widgets.lib").exists() or not all(p.exists() for p in dependencies):
        pytest.skip("The shipped Qt development libraries and resource objects are required")
    probe = tmp_path / "transcript.cpp"
    probe.write_text(r'''
#include <QApplication>
#include <QBuffer>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTextBrowser>
#include <QTextDocument>
#include <QTextList>
#include <QPointer>
#include <QTimer>
#include <QDebug>
#include <cstdio>
#define lit QStringLiteral
#include "Windows/AI/AIConversationView.cpp"

void spin(int ms) { QEventLoop loop; QTimer::singleShot(ms,&loop,&QEventLoop::quit);loop.exec(); }
bool hasImage(QTextDocument *doc) {
  return !doc->resource(QTextDocument::ImageResource, QUrl("replaylab-attachment://image")).value<QImage>().isNull();
}
int main(int argc, char **argv) {
  QApplication app(argc,argv); QApplication::setCursorFlashTime(0); Resources::Initialise();
  QJsonObject result;
  const QString styled = QStringLiteral("<html><head><style>"
     "p{color:#111111;}.second{color:#222222;}.first{color:#333333;}"
     "p.second{color:#444444;}.first.first{font-weight:600;}"
     "</style></head><body><p class=\"first second first\" title=\"a &gt; b\" "
     "style=\"color:#abcdef;\"><a href=\"https://example.com/?a=1&amp;b=2\">link</a></p></body></html>");
  const QString expanded=inlineTranscriptStyles(styled);
  QTextDocument cssDocument, inlineDocument;
  cssDocument.setHtml(styled); inlineDocument.setHtml(expanded);
  const QTextCursor before=cssDocument.find("link"), after=inlineDocument.find("link");
  result["cascade"]=before.charFormat().foreground()==after.charFormat().foreground() &&
                    before.charFormat().fontWeight()==after.charFormat().fontWeight();
  result["escapedAttributes"]=expanded.contains("title=\"a &gt; b\"") &&
       after.charFormat().anchorHref()==QStringLiteral("https://example.com/?a=1&b=2");
  result["noSelectors"]=!expanded.contains("<style>") && !expanded.contains("class=");
  QTextDocument citations;
  citations.setHtml(safeMarkdown(QString::fromUtf8(
    "[local](<D:/project with spaces/source/shader.hlsl:125:3>) "
    "[exact](rdoc://shader/abcdef?file=source/shader.hlsl&line=9&column=2) "
    "[script](file:///C:/run.cmd) [raw](javascript:alert(1)) "
    "[include](C:/shader/common.hlsli#L7C4)")));
  QUrl localCitation(citations.find("local").charFormat().anchorHref());
  const QUrlQuery localQuery(localCitation);
  result["shaderSourceLink"]=localCitation.scheme()=="rdoc" && localCitation.host()=="shader" &&
    localQuery.queryItemValue("path",QUrl::FullyDecoded)=="D:/project with spaces/source/shader.hlsl" &&
    localQuery.queryItemValue("line")=="125" && localQuery.queryItemValue("column")=="3";
  result["shaderWorkspaceLink"]=citations.find("exact").charFormat().anchorHref().startsWith("rdoc://shader/abcdef?");
  result["shaderIncludeLink"]=QUrlQuery(QUrl(citations.find("include").charFormat().anchorHref())).queryItemValue("line")=="7";
  result["rejectExecutableLinks"]=citations.find("script").charFormat().anchorHref().isEmpty() &&
                                  citations.find("raw").charFormat().anchorHref().isEmpty();
  QTextDocument table;
  table.setHtml(safeMarkdown("| Feature | Result |\n|---|---|\n| `a|b` | **Visible** |\n\n"
      "```hlsl source=rdoc://shader/example?line=73\nfloat one;\nfloat two;\n```"));
  result["markdownTable"]=table.toPlainText().contains("a|b") && !table.toPlainText().contains("|---|");
  result["exampleLinksToCalculation"]=QUrlQuery(QUrl(table.find("two").charFormat().anchorHref())).queryItemValue("line")=="73";
  QTextDocument numbered;
  numbered.setHtml(safeMarkdown("1. **First**\n   Explanation one.\n   [source](rdoc://shader/id?line=8)\n\n"
     "2. **Second**\n   Explanation two.\n\n3. **Third**\n   Explanation three.\n\nOutside."));
  const auto firstItem=numbered.find("First").block(),secondItem=numbered.find("Second").block(),thirdItem=numbered.find("Third").block();
  result["multilineListsKeepNumbersAndLinks"]=firstItem.textList()&&firstItem.textList()==secondItem.textList()&&
    secondItem.textList()==thirdItem.textList()&&firstItem.textList()->itemNumber(firstItem)==0&&
    secondItem.textList()->itemNumber(secondItem)==1&&thirdItem.textList()->itemNumber(thirdItem)==2&&
    numbered.find("source").charFormat().anchorHref()=="rdoc://shader/id?line=8"&&!numbered.find("Outside").block().textList();

  AIConversationView numberedView;numberedView.setAttribute(Qt::WA_DontShowOnScreen);numberedView.resize(550,900);
  AIChatMessage numberedAnswer;numberedAnswer.id="numbers";numberedAnswer.role="assistant";
  numberedAnswer.text="1. **Rain texture** supplies slopes. [code](rdoc://shader/id?line=8)\n\n"
    "2. **Drops** use those slopes. [RT](rdoc://event/77)\n\n3. **Lighting** combines them.";
  numberedView.setMessages({numberedAnswer});numberedView.show();spin(25);
  auto *numberedBrowser=numberedView.findChild<QTextBrowser*>();
  const auto actualFirst=numberedBrowser->document()->find("Rain texture").block();
  const auto actualSecond=numberedBrowser->document()->find("Drops").block();
  const auto actualThird=numberedBrowser->document()->find("Lighting").block();
  result["productionListsKeepSequentialNumbers"]=actualFirst.textList()&&actualFirst.textList()==actualSecond.textList()&&
    actualSecond.textList()==actualThird.textList()&&actualFirst.textList()->itemNumber(actualFirst)==0&&
    actualSecond.textList()->itemNumber(actualSecond)==1&&actualThird.textList()->itemNumber(actualThird)==2;
  numberedAnswer.text="1. Texture supplies slopes.\n\n[code](rdoc://shader/id?line=8)\n\n"
    "1. Drops use those slopes.\n\n[RT](rdoc://event/77)\n\n1. Lighting combines them.";
  numberedView.setMessages({numberedAnswer});spin(25);
  const auto linkedFirst=numberedBrowser->document()->find("Texture supplies").block();
  const auto linkedSecond=numberedBrowser->document()->find("Drops use").block();
  const auto linkedThird=numberedBrowser->document()->find("Lighting combines").block();
  result["separateCitationLinesStayInlineAndNumbered"]=linkedFirst.textList()&&linkedFirst.textList()==linkedSecond.textList()&&
    linkedSecond.textList()==linkedThird.textList()&&linkedThird.textList()->itemNumber(linkedThird)==2&&
    numberedBrowser->document()->find("code").block()==linkedFirst&&numberedBrowser->document()->find("RT").block()==linkedSecond;
  numberedAnswer.text="1. First stage.\n\n2. Second stage.\n   ```hlsl\n   float phase = frac(time);\n   ```\n   Continue calculation.\n\n3. Third stage.";
  numberedView.setMessages({numberedAnswer});spin(25);
  const auto withCodeFirst=numberedBrowser->document()->find("First stage").block();
  const auto withCodeSecond=numberedBrowser->document()->find("Second stage").block();
  const auto withCodeThird=numberedBrowser->document()->find("Third stage").block();
  result["codeInsideListKeepsSequentialNumbers"]=withCodeFirst.textList()&&withCodeFirst.textList()==withCodeSecond.textList()&&
    withCodeSecond.textList()==withCodeThird.textList()&&withCodeThird.textList()->itemNumber(withCodeThird)==2;
  numberedView.close();

  AIConversationView view;view.setAttribute(Qt::WA_DontShowOnScreen);view.resize(550,900);view.show();
  auto browser=view.findChild<QTextBrowser*>();
  AIChatMessage message;message.id="user";message.role="user";message.taskId="task";
  message.state="running";message.timestampMs=QDateTime::currentMSecsSinceEpoch()-9000;
  message.activityExpanded=true;message.text="**Selected text** [link](https://example.com/?a=1&b=2)\n\n<script>unsafe</script>";
  for(int i=0;i<40;++i){AIActivityItem a;a.id=QString::number(i);a.kind="mcpToolCall";
    a.title="Bread.tool_"+QString::number(i);a.detail="mode, workspace_path";a.completed=true;
    message.activity.push_back(a);}
  AIActivityItem live;live.id="live";live.kind="progressNote";live.title="Progress";
  live.detail="Streaming prefix";live.liveText=true;live.timestampMs=message.timestampMs;
  message.activity.push_back(live);
  QImage image(2,2,QImage::Format_ARGB32);image.fill(Qt::red);QByteArray png;QBuffer buffer(&png);
  buffer.open(QIODevice::WriteOnly);image.save(&buffer,"PNG");
  AIChatAttachment attachment;attachment.id="image";attachment.kind="image";attachment.mediaType="image/png";
  attachment.encoding="base64";attachment.content=QString::fromLatin1(png.toBase64());attachment.title="Preview";
  message.attachments.push_back(attachment);
  AIChatMessage answer;answer.id="answer";answer.role="assistant";
  answer.text="```hlsl\nfloat4 colour = float4(1, 0, 0, 1);\n"
              "/* explanation\nreturn remains a comment */\n"
              "float3 lighting = lerp(colour.xyz, 1.0f, 2.5e-3f);\n```";
  QJsonObject artifact;artifact["path"]="C:/output/report.md";artifact["status"]="completed";
  answer.artifacts.append(artifact);
  QVector<AIChatMessage> messages{message,answer};view.setMessages(messages);spin(25);
  result["codeHighlight"]=browser->document()->find("float4").charFormat().foreground().color()==QColor("#871094");
  result["blockCommentHighlight"]=browser->document()->find("return remains").charFormat().foreground()==
                                browser->document()->find("explanation").charFormat().foreground();
  result["mathFunctionHighlight"]=browser->document()->find("lerp").charFormat().foreground()!=
                                  browser->document()->find("lighting").charFormat().foreground();
  result["exponentLiteralHighlight"]=browser->document()->find("2.5e-3f").charFormat().foreground()==
                                    browser->document()->find("1.0f").charFormat().foreground();
  result["artifactLink"]=browser->document()->find("report.md").charFormat().anchorHref().startsWith("replaylab-artifact://open/");
  result["safeText"]=browser->toPlainText().contains("<script>unsafe</script>");
  result["imageResource"]=hasImage(browser->document());
  QTextCursor selected=browser->document()->find("Selected text");browser->setTextCursor(selected);
  auto selectedDocument=browser->document();
  messages[0].activity.last().detail+=" and new suffix";view.setMessages(messages);spin(80);
  result["liveInPlace"]=browser->document()==selectedDocument &&
      browser->toPlainText().contains("Streaming prefix and new suffix") &&
      browser->textCursor().selectedText()=="Selected text";
  QString elapsedBefore=browser->toPlainText();const int elapsedRevision=browser->document()->revision();
  const QImage elapsedImage=browser->viewport()->grab().toImage();spin(1100);
  result["elapsedInPlace"]=browser->document()==selectedDocument &&
      browser->textCursor().selectedText()=="Selected text" && browser->toPlainText()==elapsedBefore &&
      browser->document()->revision()==elapsedRevision && browser->viewport()->grab().toImage()!=elapsedImage;
  messages[0].activity[0].detail="changed while selected";view.setMessages(messages);
  result["selectionDefers"]=browser->document()==selectedDocument;
  QPointer<QTextDocument> previous=browser->document();selected.clearSelection();browser->setTextCursor(selected);spin(30);
  result["replaceOwnsDocument"]=previous.isNull() && hasImage(browser->document()) &&
      browser->toPlainText().contains("changed while selected");

  view.scrollToMessage("user");spin(20);
  browser->verticalScrollBar()->triggerAction(QAbstractSlider::SliderPageStepAdd);spin(20);
  const QTextCursor row=browser->document()->find("Bread.tool_10");
  browser->verticalScrollBar()->setValue(browser->verticalScrollBar()->value()+browser->cursorRect(row).top());
  const QTextCursor visible=browser->cursorForPosition(QPoint(browser->viewport()->width()/2,1));
  const QString visibleText=visible.block().text();
  const int oldY=browser->cursorRect(QTextCursor(visible.block())).top();
  messages[0].activity.prepend(messages[0].activity[0]);messages[0].activity[0].id="prepended";
  view.setMessages(messages);spin(20);
  QTextCursor restored=browser->document()->find(visibleText);
  result["scrollText"]=visibleText;result["oldY"]=oldY;result["newY"]=browser->cursorRect(QTextCursor(restored.block())).top();
  result["scrollAnchor"]=!restored.isNull() && qAbs(browser->cursorRect(QTextCursor(restored.block())).top()-oldY)<24;

  // A completed turn can still own a selection after focus moves into the composer. Incoming
  // structural updates are correctly held for copying, but an explicit send must release that
  // old selection and resume the live tail even though this is the same conversation.
  messages[0].state="completed";messages[0].finishedTimestampMs=QDateTime::currentMSecsSinceEpoch();
  view.setMessages(messages);spin(20);
  selected=browser->document()->find("Selected text");browser->setTextCursor(selected);
  QFocusEvent focusOut(QEvent::FocusOut);QApplication::sendEvent(browser,&focusOut);
  AIChatMessage followup;followup.id="followup";followup.role="user";followup.taskId="task2";
  followup.text="Continue analysing this frame";followup.state="running";
  followup.timestampMs=QDateTime::currentMSecsSinceEpoch();followup.activityExpanded=true;
  messages.push_back(followup);view.setMessages(messages);spin(20);
  result["selectedHistoryHeld"]=!browser->toPlainText().contains(followup.text) &&
      browser->textCursor().selectedText()=="Selected text";
  // Leave an old scroll callback queued when send takes ownership of the viewport.
  browser->verticalScrollBar()->triggerAction(QAbstractSlider::SliderToMinimum);
  view.followSubmittedTurn();spin(30);
  result["submittedFollowupVisible"]=browser->toPlainText().contains(followup.text) &&
      !browser->textCursor().hasSelection();
  result["submittedFollowupAtTail"]=browser->verticalScrollBar()->maximum()-
      browser->verticalScrollBar()->value()<=2;
  AIActivityItem next;next.id="next";next.kind="progressNote";next.title="Current work";
  next.detail="New follow-up progress";next.liveText=true;next.timestampMs=followup.timestampMs;
  messages.last().activity.push_back(next);view.setMessages(messages);spin(80);
  result["followupStreamingVisible"]=browser->toPlainText().contains(next.detail) &&
      browser->verticalScrollBar()->maximum()-browser->verticalScrollBar()->value()<=2;
  // Reading an earlier row must still remain stable until the user explicitly submits again.
  view.scrollToMessage("user");spin(20);
  messages.last().activity[0].detail+=" after reading history";view.setMessages(messages);spin(80);
  result["backgroundDoesNotStealScroll"]=browser->verticalScrollBar()->maximum()-
      browser->verticalScrollBar()->value()>10;
  view.followSubmittedTurn();view.setMessages(messages);spin(30);
  result["unselectedFollowupAtTail"]=browser->verticalScrollBar()->maximum()-
      browser->verticalScrollBar()->value()<=2;

  QElapsedTimer timer;timer.start();qint64 last=0,maxGap=0;int beats=0,step=0;
  QTimer heartbeat;heartbeat.setInterval(10);QObject::connect(&heartbeat,&QTimer::timeout,[&](){
      const auto now=timer.elapsed();maxGap=qMax(maxGap,now-last);last=now;++beats;});heartbeat.start();
  QTimer stress;stress.setInterval(1);QObject::connect(&stress,&QTimer::timeout,[&](){
    if(step==120){stress.stop();app.quit();return;}
    messages[0].activity[0].completed=!messages[0].activity[0].completed;
    view.setMessages(messages);++step;
  });stress.start();app.exec();
  result["stressGenerations"]=step;result["maxGapMs"]=double(maxGap);result["beats"]=beats;
  result["oneOwnedDocument"]=browser->findChildren<QTextDocument*>(QString(),Qt::FindDirectChildrenOnly).size()==1;
  result["qtVersion"]=QString::fromLatin1(qVersion());
  QFile output(QString::fromLocal8Bit(argv[1])); output.open(QIODevice::WriteOnly);
  output.write(QJsonDocument(result).toJson(QJsonDocument::Compact)); output.close();
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    includes = [ROOT / "qrenderdoc", ROOT / "renderdoc/api/replay", ROOT, qt / "include"] + [qt / "include" / name for name in
                                                           ("QtCore", "QtGui", "QtWidgets")]
    compiled = compiler.compile([str(probe)], output_dir=str(tmp_path),
                                include_dirs=[str(p) for p in includes],
                                extra_postargs=["/EHsc", "/std:c++17", "/MD", "/DQT_NO_DEBUG", "/DRENDERDOC_PLATFORM_WIN32"])
    executable = tmp_path / "transcript.exe"
    compiler.link_executable(compiled + [str(p) for p in dependencies], str(executable.with_suffix("")),
                             libraries=["Qt5Widgets", "Qt5Gui", "Qt5Core", "ole32", "windowscodecs",
                                        "shell32", "user32", "gdi32"],
                             library_dirs=[str(qt / "x64/lib")], target_lang="c++",
                             extra_postargs=["/SUBSYSTEM:CONSOLE"])
    environment = dict(os.environ)
    environment["PATH"] = str(ROOT / "x64/Development") + os.pathsep + environment.get("PATH", "")
    environment["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(qt / "x64/plugins/platforms")
    report = tmp_path / "result.json"
    completed = subprocess.run([str(executable), str(report)], env=environment, check=True, capture_output=True,
                               text=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    assert report.exists(), (completed.returncode, completed.stdout, completed.stderr)
    evidence = json.loads(report.read_text(encoding="utf-8"))
    for key in ("codeInsideListKeepsSequentialNumbers", "separateCitationLinesStayInlineAndNumbered", "productionListsKeepSequentialNumbers", "cascade", "escapedAttributes", "noSelectors", "safeText", "imageResource",
                "liveInPlace", "elapsedInPlace", "selectionDefers", "replaceOwnsDocument", "oneOwnedDocument",
                "codeHighlight", "blockCommentHighlight", "mathFunctionHighlight", "exponentLiteralHighlight",
                "shaderSourceLink", "shaderWorkspaceLink", "shaderIncludeLink", "rejectExecutableLinks",
                "multilineListsKeepNumbersAndLinks",
                "artifactLink", "scrollAnchor", "selectedHistoryHeld", "submittedFollowupVisible",
                "submittedFollowupAtTail", "followupStreamingVisible", "backgroundDoesNotStealScroll",
                "unselectedFollowupAtTail"):
        assert evidence[key], (key, evidence, completed.stderr)
    assert evidence["stressGenerations"] == 120
    assert evidence["beats"] >= 20
    assert evidence["maxGapMs"] < 1000
    print("Transcript native:", evidence)
