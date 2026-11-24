"""Run the production trace model against recoverable tool failures and channel routing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Production Windows Qt/MSVC fixture")
def test_agent_lifecycle_and_exposed_reasoning_channels(tmp_path, monkeypatch):
    from setuptools._distutils.ccompiler import new_compiler

    qt = ROOT / "qrenderdoc/3rdparty/qt"
    if not (qt / "x64/lib/Qt5Core.lib").exists():
        pytest.skip("The shipped Qt development libraries are required")
    probe = tmp_path / "trace.cpp"
    probe.write_text(r'''
#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>
#include "Windows/AI/AIAgentTraceStore.h"
int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  AIAgentTraceStore store;
  qint64 sequence=0;
  auto event = [&](const char *kind, QJsonObject data, QString text=QString()) {
    QJsonObject item;
    item["schema"]="replaylab-ai-task-event/v1";item["taskId"]="task";
    item["sequence"]=double(++sequence);item["timestampUnixMs"]=double(sequence*1000);
    item["kind"]=kind;item["data"]=data;item["text"]=text;
    store.ConsumeEvent(item);
  };
  QJsonObject result;
  event("queued",{});
  event("running",{{"activityKind","mcpToolCall"},{"activityId","finish"},{"status","failed"}});
  result["toolFailureKeepsAgentRunning"]=store.Agent("task","main")->status=="running";
  event("running",{{"activityKind","mcpToolCall"},{"activityId","read"},{"status","completed"}});
  result["toolSuccessDoesNotCompleteAgent"]=store.Agent("task","main")->status=="running";
  event("reasoning-delta",{{"channel","raw"},{"lifecycle","failed"}});
  result["reasoningFailureDoesNotTerminateTurn"]=store.Agent("task","main")->status=="running";
  event("running",{{"phase","child"},{"planTaskId","child-1"},{"status","completed"}});
  result["childTerminalPreserved"]=store.Agent("task","child-1")->status=="completed";

  QJsonObject raw{{"channel","raw"},{"itemId","r1"},{"reasoningId","[r1,raw,0]"},{"contentIndex",0}};
  QJsonObject summary{{"channel","summary"},{"itemId","r1"},{"reasoningId","[r1,summary,0]"},{"summaryIndex",0}};
  result["separateKinds"]=AIAgentTraceStore::ConversationReasoningKind(raw)=="reasoning-raw" &&
                          AIAgentTraceStore::ConversationReasoningKind(summary)=="reasoning";
  result["separateStreams"]=AIAgentTraceStore::ConversationReasoningId(raw)!=
                            AIAgentTraceStore::ConversationReasoningId(summary);
  const QString part0=AIAgentTraceStore::ConversationReasoningId(summary);
  summary["summaryIndex"]=1;
  result["separateParts"]=part0!=AIAgentTraceStore::ConversationReasoningId(summary);
  result["snapshotIdentity"]=part0.startsWith(AIAgentTraceStore::ConversationSummaryPrefix("r1"));
  result["opaqueNeverProjected"]=AIAgentTraceStore::ConversationReasoningKind(
      {{"channel","opaque"}}).isEmpty();
  raw["lifecycle"]="updated";summary["lifecycle"]="updated";
  event("reasoning-delta",raw,"provider raw text");
  event("reasoning-delta",summary,"provider summary text");
  result["traceChannelsRemainSeparate"]=store.Agent("task","main")->rawReasoning.contains("provider raw text") &&
      !store.Agent("task","main")->rawReasoning.contains("provider summary text") &&
      store.Agent("task","main")->reasoningSummary.contains("provider summary text");
  event("completed",{{"terminalStatus","partial"}});
  result["partialTerminal"]=store.Agent("task","main")->status=="partial";
  event("completed",{{"terminalStatus","blocked"}});
  result["blockedTerminal"]=store.Agent("task","main")->status=="blocked";
  QJsonObject unrelated{{"schema","replaylab-ai-task-event/v1"},{"taskId","other"},
      {"sequence",1},{"timestampUnixMs",1},{"kind","queued"}};
  store.ConsumeEvent(unrelated);
  store.ForgetTask("task");
  event("reasoning-delta",raw,"late private text");
  result["deletedTaskCannotReturn"]=store.Task("task")==nullptr && !store.TaskIds().contains("task");
  result["deletionPreservesOtherTask"]=store.Task("other")!=nullptr;
  store.Clear();
  event("queued",{});
  result["deletionSurvivesCaptureSwitch"]=store.Task("task")==nullptr;
  QFile output(QString::fromLocal8Bit(argv[1])); output.open(QIODevice::WriteOnly);
  output.write(QJsonDocument(result).toJson(QJsonDocument::Compact));
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    # distutils appends absolute source directories below output_dir, which
    # duplicates the full temp path and exceeds MSVC's path limit on CI.
    # Compile exact copies by relative name without changing the tested code.
    source_root = ROOT / "qrenderdoc/Windows/AI"
    for name in ("AIAgentTraceStore.cpp", "AIAgentTraceStore.h"):
        (tmp_path / name).write_bytes((source_root / name).read_bytes())
    with monkeypatch.context() as context:
        context.chdir(tmp_path)
        compiled = compiler.compile(
            ["trace.cpp", "AIAgentTraceStore.cpp"],
            output_dir=str(tmp_path),
            include_dirs=[str(ROOT / "qrenderdoc"), str(qt / "include"), str(qt / "include/QtCore")],
            extra_postargs=["/EHsc", "/std:c++14", "/MD", "/DQT_NO_DEBUG"],
        )
    executable = tmp_path / "trace.exe"
    compiler.link_executable(compiled, str(executable.with_suffix("")), libraries=["Qt5Core"],
                             library_dirs=[str(qt / "x64/lib")], target_lang="c++")
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join([
        str(qt / "x64/bin"), str(ROOT / "x64/Development"), environment.get("PATH", "")])
    report = tmp_path / "result.json"
    subprocess.run([str(executable), str(report)], env=environment, check=True,
                   capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    results = json.loads(report.read_text(encoding="utf-8"))
    assert results and all(results.values()), results
