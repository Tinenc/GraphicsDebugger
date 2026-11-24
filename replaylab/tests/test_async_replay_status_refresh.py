"""Run the production refresh methods on a real Qt loop and a slow replay queue."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Qt/MSVC regression fixture")
def test_refresh_never_waits_for_replay_and_discards_obsolete_publications(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler

    qt = Path(os.environ.get("BREAD_TEST_QT", str(ROOT / "qrenderdoc/3rdparty/qt/x64")))
    includes = qt / "include" if (qt / "include").exists() else qt.parent / "include"
    if not (qt / "lib/Qt5Core.lib").exists():
        pytest.skip("Qt development libraries are unavailable")
    production = (ROOT / "qrenderdoc/Code/CaptureContext.cpp").read_text(encoding="utf-8")
    methods = production.split("void CaptureContext::RefreshStatus()", 1)[1].split(
        "uint64_t CaptureContext::ReplayStatusPublications::Begin", 1
    )[0]
    source = tmp_path / "refresh.cpp"
    source.write_text(r'''
#include <QCoreApplication>
#include <QTimer>
#include <QThread>
#include <QEventLoop>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QJsonObject>
#include <atomic>
#include <memory>
#include <functional>
#include <iostream>
#define lit QStringLiteral
struct Window : QObject { QObject *Widget() { return this; } };
struct IReplayController {
  int calls=0, forced=0, event=0;
  void SetSelectedEventID(unsigned) {}
  void SetFrameEvent(unsigned eid, bool force) {
    QThread::msleep(400); ++calls; if(force) ++forced; event=eid;
  }
};
struct Replay {
  QThread thread; QObject worker; IReplayController controller;
  Replay() { worker.moveToThread(&thread); thread.start(); }
  ~Replay() { thread.quit(); thread.wait(); }
  void AsyncInvoke(const QString &, std::function<void(IReplayController*)> fn) {
    QTimer::singleShot(0, &worker, [this,fn]() { fn(&controller); });
  }
  void AsyncInvoke(std::function<void(IReplayController*)> fn) { AsyncInvoke(QString(), fn); }
};
struct GUIInvoke {
  static void call(QObject *owner, std::function<void()> fn) { QTimer::singleShot(0, owner, fn); }
};
struct CaptureContext {
  struct PublishedPipelineState {
    int event; PublishedPipelineState(IReplayController *r) : event(r->event) {}
  };
  Window window; Window *m_MainWindow=&window;
  Replay m_Replay;
  std::atomic<uint64_t> m_ScrubEpoch{0}, m_ScrubGeneration{0};
  uint64_t m_IdleCheckpointGeneration=0, m_StatusRefreshSerial=0, m_StatusRefreshEpoch=0;
  bool m_StatusRefreshForce=false, m_StatusRefreshQueued=false, m_StatusRefreshDeferred=false, loaded=true;
  bool m_ScrubInFlight=false, m_ScrubCommitPending=false;
  unsigned m_SelectedEventID=42, m_EventID=42;
  int publications=0, viewers=0, publishedEvent=0;
  bool IsCaptureLoaded() { return loaded; }
  void PublishPipelineState(std::shared_ptr<const PublishedPipelineState> state) {
    ++publications; publishedEvent=state->event;
  }
  void RefreshUIStatus(std::initializer_list<int>, bool, bool) { ++viewers; }
  void ScheduleIdleCheckpointMaintenance() {}
  void InvokeAfterPresentation(unsigned, std::function<void()> fn) {
    QTimer::singleShot(0, &window, fn);
  }
  void LogEIDTiming(const QString &) {}
  void RefreshStatus(); void QueueReplayStatusRefresh(bool force);
};
''' + "void CaptureContext::RefreshStatus()" + methods + r'''
void spin(int ms) { QEventLoop loop; QTimer::singleShot(ms, &loop, &QEventLoop::quit); loop.exec(); }
int main(int argc, char **argv) {
  QCoreApplication app(argc, argv); CaptureContext ctx;
  QElapsedTimer clock; clock.start(); qint64 prev=0, gap=0; int beats=0;
  QTimer timer; timer.setInterval(10);
  QObject::connect(&timer,&QTimer::timeout,[&]() { auto t=clock.elapsed(); gap=qMax(gap,t-prev); prev=t; ++beats; });
  timer.start();
  auto start=clock.elapsed();
  ctx.QueueReplayStatusRefresh(false); ctx.RefreshStatus(); ctx.QueueReplayStatusRefresh(false);
  auto returned=clock.elapsed()-start; spin(550);
  bool coalesced=ctx.m_Replay.controller.calls==1 && ctx.m_Replay.controller.forced==1
                 && ctx.publications==1 && ctx.viewers==1;
  ctx.RefreshStatus();
  QTimer::singleShot(30,[&]() { ++ctx.m_ScrubEpoch; ctx.m_EventID=43; ctx.m_SelectedEventID=43; });
  spin(550); bool navigation=ctx.publications==1;
  ctx.RefreshStatus();
  QTimer::singleShot(30,[&]() { ctx.RefreshStatus(); });
  spin(950); bool superseded=ctx.publications==2 && ctx.publishedEvent==43;
  ctx.m_ScrubInFlight=true; ++ctx.m_ScrubGeneration; ctx.RefreshStatus(); spin(80);
  bool deferred=ctx.m_StatusRefreshDeferred && ctx.publications==2;
  ctx.m_ScrubInFlight=false; ctx.m_EventID=44; ctx.m_SelectedEventID=44;
  ctx.QueueReplayStatusRefresh(ctx.m_StatusRefreshForce); spin(550);
  deferred=deferred && ctx.publications==3 && ctx.publishedEvent==44;
  ctx.RefreshStatus();
  QTimer::singleShot(30,[&]() { ctx.loaded=false; ++ctx.m_ScrubEpoch; });
  spin(550); bool close=ctx.publications==3;
  QJsonObject result; result["returnMs"]=double(returned); result["maxGapMs"]=double(gap);
  result["heartbeats"]=beats; result["coalesced"]=coalesced; result["navigation"]=navigation;
  result["superseded"]=superseded; result["close"]=close; result["deferred"]=deferred;
  std::cout << QJsonDocument(result).toJson(QJsonDocument::Compact).constData() << std::endl;
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    objects = compiler.compile([str(source)], output_dir=str(tmp_path),
                               include_dirs=[str(includes), str(includes / "QtCore")],
                               extra_postargs=["/EHsc", "/std:c++14"])
    executable = tmp_path / "refresh.exe"
    compiler.link_executable(objects, str(executable), libraries=["Qt5Core"],
                             library_dirs=[str(qt / "lib")], target_lang="c++")
    environment = dict(os.environ)
    environment["PATH"] = str(qt / "bin") + os.pathsep + environment.get("PATH", "")
    result = subprocess.run([str(executable)], env=environment, check=True,
                            capture_output=True, text=True, timeout=15)
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["returnMs"] < 100, evidence
    assert evidence["maxGapMs"] < 200, evidence
    assert evidence["heartbeats"] > 80, evidence
    for key in ("coalesced", "navigation", "superseded", "close", "deferred"):
        assert evidence[key], evidence
    print("Async replay refresh:", evidence)
