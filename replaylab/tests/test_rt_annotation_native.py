"""Exercise the actual Qt canvas without opening a desktop window or capture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Qt/MSVC fixture")
def test_mark_geometry_erasure_undo_and_immutable_source(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler

    qt = ROOT / "qrenderdoc/3rdparty/qt"
    if not (qt / "x64/lib/Qt5Widgets.lib").exists():
        pytest.skip("The shipped Qt development libraries are required")
    probe = tmp_path / "annotation.cpp"
    probe.write_text(r'''
#include <QApplication>
#include <QFile>
#include <QJsonDocument>
#include "3rdparty/flowlayout/FlowLayout.cpp"
#include "Windows/AI/AITextureAnnotation.cpp"
#include "Windows/AI/AIConversationView.h"

int main(int argc, char **argv) {
  QApplication app(argc,argv);
  QImage original(800,400,QImage::Format_RGB32);original.fill(QColor(40,80,120));
  QImage acceptedOriginal, acceptedMarked;QJsonObject acceptedContext;
  bool mayAccept=false, closed=false;
  AITextureAnnotation panel(original,"EID 77",{{"capture",QJsonObject{{"eventId",77}}}},
    [&](const QImage &source,const QImage &marked,const QJsonObject &context) {
      acceptedOriginal=source;acceptedMarked=marked;acceptedContext=context;return mayAccept;
    },[&](){closed=true;});
  panel.resize(900,640);panel.ensurePolished();panel.layout()->activate();
  auto *canvas=static_cast<AIAnnotationCanvas *>(panel.findChild<QWidget *>("aiAnnotationCanvas"));
  canvas->resize(800,500); // 50 pixels of vertical letterboxing
  auto position=[&](double u,double v){return QPointF(u*800,50+v*400);};
  auto mouse=[&](QEvent::Type kind,QPointF p) {
    QMouseEvent e(kind,p,Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
    QApplication::sendEvent(canvas,&e);
  };
  auto drag=[&](MarkTool tool,double x0,double y0,double x1,double y1) {
    canvas->tool=tool;mouse(QEvent::MouseButtonPress,position(x0,y0));
    mouse(QEvent::MouseMove,position(x1,y1));mouse(QEvent::MouseButtonRelease,position(x1,y1));
  };
  QJsonObject result;
  mouse(QEvent::MouseButtonPress,QPointF(5,5));mouse(QEvent::MouseButtonRelease,QPointF(5,5));
  result["letterboxIgnored"]=canvas->marks.isEmpty();
  canvas->color=QColor("#2060ff");
  drag(MarkTool::Ellipse,.2,.3,.5,.7);
  canvas->color=QColor("#ff3040");
  drag(MarkTool::Arrow,.9,.8,.4,.5);
  QJsonArray strokes=canvas->strokes();
  result["normalizedCircle"]=strokes[0].toObject()["points"].toArray()[0].toArray()==QJsonArray({.2,.3});
  result["arrowTip"]=strokes[1].toObject()["points"].toArray()[1].toArray()==QJsonArray({.4,.5});
  result["distinctColorIdentity"]=strokes[0].toObject()["color"]=="#2060ff" &&
      strokes[1].toObject()["color"]=="#ff3040";
  canvas->resize(1000,800);
  result["resizeKeepsCoordinates"]=canvas->strokes()==strokes;
  canvas->resize(800,500);
  QWheelEvent wheel(position(.5,.5),position(.5,.5),QPoint(),QPoint(0,240),240,
                    Qt::Vertical,Qt::NoButton,Qt::NoModifier);
  QApplication::sendEvent(canvas,&wheel);
  drag(MarkTool::Arrow,.4,.4,.5,.5);
  const auto zoomPoints=canvas->strokes().last().toObject()["points"].toArray();
  result["zoomMapsToFullImage"]=zoomPoints[0].toArray()==QJsonArray({.436,.436}) &&
      zoomPoints[1].toArray()==QJsonArray({.5,.5});
  canvas->restore(true);
  for(auto kind:{QEvent::MouseButtonPress,QEvent::MouseMove,QEvent::MouseButtonRelease}) {
    QMouseEvent event(kind,kind==QEvent::MouseButtonPress?QPointF(100,100):QPointF(150,120),
                      Qt::MiddleButton,Qt::MiddleButton,Qt::NoModifier);
    QApplication::sendEvent(canvas,&event);
  }
  drag(MarkTool::Arrow,.4,.4,.5,.5);
  result["panMapsToFullImage"]=canvas->strokes().last().toObject()["points"].toArray()[1].toArray()==QJsonArray({.46,.468});
  canvas->restore(true);canvas->fit();
  drag(MarkTool::Eraser,.2,.5,.2,.5);
  const auto erased=canvas->strokes();
  result["eraserKeepsEditableActions"]=canvas->marks.size()==3 &&
      canvas->marks.last().tool==MarkTool::Eraser;
  result["eraseOnlyLocalCurve"]=canvas->annotated().pixelColor(160,200)==original.pixelColor(160,200) &&
      canvas->annotated().pixelColor(280,120)!=original.pixelColor(280,120);
  canvas->restore(true);
  result["undoRestores"]=canvas->strokes()==strokes;
  canvas->restore(false);
  result["redoErases"]=canvas->strokes()==erased;
  QJsonObject restoredContext{{"annotation",QJsonObject{{"strokes",erased}}}};
  AITextureAnnotation restored(original,"EID 77",restoredContext,
      [](const QImage &,const QImage &,const QJsonObject &){return true;},[](){});
  auto *restoredCanvas=static_cast<AIAnnotationCanvas *>(restored.findChild<QWidget *>("aiAnnotationCanvas"));
  result["reopenRestoresColorsAndLocalErasure"]=restoredCanvas->strokes()==erased &&
      restoredCanvas->annotated()==canvas->annotated();
  canvas->restore(true);
  result["originalImmutable"]=canvas->image==original && original.pixelColor(160,200)==QColor(40,80,120);
  result["separateOverlay"]=canvas->annotated()!=original;
  QPushButton *add=nullptr;
  for(auto *button:panel.findChildren<QPushButton *>())
    if(button->text()=="Add to conversation") add=button;
  add->click();result["failedAcceptRetainsCanvas"]=!closed && canvas->marks.size()==2;
  mayAccept=true;add->click();
  result["acceptPinsIdentity"]=closed && acceptedContext["capture"].toObject()["eventId"]==77;
  result["acceptIncludesVectors"]=acceptedContext["annotation"].toObject()["strokes"].toArray()==strokes;
  result["acceptIncludesBothImages"]=acceptedOriginal==original && acceptedMarked!=original;
  if(argc>2) {
    QImage preview(panel.size(),QImage::Format_RGB32);preview.fill(Qt::white);
    panel.render(&preview);preview.save(QString::fromLocal8Bit(argv[2]));
  }
  canvas->clear();result["clearWorks"]=canvas->marks.isEmpty();
  canvas->restore(true);result["clearUndoWorks"]=canvas->strokes()==strokes;
  canvas->clear();
  drag(MarkTool::Pen,.1,.5,.9,.5);
  canvas->eraserWidth=40;
  drag(MarkTool::Eraser,.5,.5,.5,.5);
  result["eraserDiameterInImage"]=canvas->annotated().pixelColor(415,200)==original.pixelColor(415,200) &&
      canvas->annotated().pixelColor(425,200)!=original.pixelColor(425,200);
  canvas->penWidth=3;
  drag(MarkTool::Pen,.48,.5,.52,.5);
  result["drawingAfterErasing"]=canvas->annotated().pixelColor(400,200)!=original.pixelColor(400,200);
  canvas->clear();
  for(int i=0;i<70;i++)drag(MarkTool::Arrow,.1,.1,.2,.2);
  result["strokeBudget"]=canvas->marks.size()==64;
  canvas->clear();canvas->tool=MarkTool::Pen;
  for(int stroke=0;stroke<6;stroke++) {
    mouse(QEvent::MouseButtonPress,position(.1,.1));
    for(int i=0;i<1000;i++)mouse(QEvent::MouseMove,position(.1+(i%90)/100.0,.1+((i/90)%8)/10.0));
    mouse(QEvent::MouseButtonRelease,position(.9,.9));
  }
  int count=0;for(const auto &mark:canvas->marks)count+=mark.points.size();
  result["longPenRemainsBounded"]=count<=1024 && canvas->marks.last().points.size()<=256;
  result["longPenRetainsEndpoint"]=canvas->strokes().last().toObject()["points"].toArray().last().toArray()==QJsonArray({.9,.9});
  AIChatAttachment clean, marked;
  clean.context={{"annotation",QJsonObject{{"pairId","pair"},{"imageRole","original"}}}};
  marked.context={{"annotation",QJsonObject{{"pairId","pair"},{"imageRole","annotated"}}}};
  result["oneVisibleImageForCompletePair"]=AIHideAnnotationOriginal(clean,{clean,marked}) &&
      !AIHideAnnotationOriginal(marked,{clean,marked});
  result["legacyOrphanIsVisible"]=!AIHideAnnotationOriginal(clean,{clean});
  // An inherited RT view is independent of canvas size and snapshot resolution. Exercise
  // logical coordinates at multiple display DPIs, including smaller-than-fit and panned views.
  AITextureAnnotation inherited(original,"EID 77",{},
      [](const QImage &,const QImage &,const QJsonObject &){return true;},[](){});
  inherited.resize(1100,720);inherited.layout()->activate();
  auto *inheritedCanvas=static_cast<AIAnnotationCanvas *>(inherited.findChild<QWidget *>("aiAnnotationCanvas"));
  auto globalBounds=[&]() {
    inheritedCanvas->grab();
    const auto v=inheritedCanvas->property("annotationImageRect").toList();
    return QRectF(v[0].toDouble(),v[1].toDouble(),v[2].toDouble(),v[3].toDouble())
        .translated(inheritedCanvas->mapToGlobal(QPoint()));
  };
  bool preserved=true, resized=true, anchored=true, mapped=true;
  for(double dpr:{1.0,1.5,2.0}) for(double scale:{.2,1.0,3.5}) {
    // The full RT is 2560x1280; the clean AI image is only 800x400.
    const QRectF expected(QPointF(inherited.mapToGlobal(QPoint()))+QPointF(-137/dpr,-83/dpr),
                          QSizeF(2560,1280)*(scale/dpr));
    inherited.SetImageViewRect(expected);
    preserved &= globalBounds()==expected;
    inherited.resize(720,510);inherited.layout()->activate();
    resized &= globalBounds()==expected;
    inherited.resize(1100,720);inherited.layout()->activate();
    resized &= globalBounds()==expected;
    const QPointF center=inheritedCanvas->rect().center();
    const QPointF localOrigin=expected.topLeft()-inheritedCanvas->mapToGlobal(QPoint());
    const QPointF uv((center.x()-localOrigin.x())/expected.width(),
                     (center.y()-localOrigin.y())/expected.height());
    QWheelEvent zoomIn(center,center,QPoint(),QPoint(0,120),120,
                      Qt::Vertical,Qt::NoButton,Qt::NoModifier);
    QApplication::sendEvent(inheritedCanvas,&zoomIn);
    const QRectF zoomed=globalBounds();
    const QPointF screenCenter=center+inheritedCanvas->mapToGlobal(QPoint());
    anchored &= QLineF(zoomed.topLeft()+QPointF(uv.x()*zoomed.width(),uv.y()*zoomed.height()),screenCenter).length()<1e-8;
    anchored &= std::abs(zoomed.width()-expected.width()*1.25)<1e-8;
  }
  const QRectF drawingRect(QPointF(inheritedCanvas->mapToGlobal(QPoint()))+QPointF(-200,-100),QSizeF(1600,800));
  inherited.SetImageViewRect(drawingRect);
  inheritedCanvas->tool=MarkTool::Arrow;
  for(auto kind:{QEvent::MouseButtonPress,QEvent::MouseMove,QEvent::MouseButtonRelease}) {
    const QPointF p=kind==QEvent::MouseButtonPress?QPointF(200,100):QPointF(600,300);
    QMouseEvent e(kind,p,Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
    QApplication::sendEvent(inheritedCanvas,&e);
  }
  const auto inheritedPoints=inheritedCanvas->strokes().last().toObject()["points"].toArray();
  mapped=inheritedPoints[0].toArray()==QJsonArray({.25,.25}) &&
         inheritedPoints[1].toArray()==QJsonArray({.5,.5});
  result["inheritExactScaleAndPanAcrossDpi"]=preserved;
  result["toolbarAndCanvasResizeDoNotRefit"]=resized;
  result["wheelPreservesCursorAnchorBelowAndAboveFit"]=anchored;
  result["inheritedViewMapsMarksToFullSnapshot"]=mapped;
  inheritedCanvas->fit();
  const QSizeF fitSize=QSizeF(original.size()).scaled(QSizeF(inheritedCanvas->size()),Qt::KeepAspectRatio);
  result["explicitFitStillWorks"]=globalBounds().size()==fitSize;
  QFile out(QString::fromLocal8Bit(argv[1]));out.open(QIODevice::WriteOnly);
  out.write(QJsonDocument(result).toJson());
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    objects = compiler.compile([str(probe)], output_dir=str(tmp_path),
                               include_dirs=[str(ROOT / "qrenderdoc"), str(qt / "include"),
                                             *[str(qt / "include" / name) for name in ("QtCore", "QtGui", "QtWidgets")]],
                               extra_postargs=["/EHsc", "/std:c++14", "/MD", "/DQT_NO_DEBUG", "/utf-8"])
    executable = tmp_path / "annotation.exe"
    compiler.link_executable(objects, str(executable.with_suffix("")), libraries=["Qt5Widgets", "Qt5Gui", "Qt5Core"],
                             library_dirs=[str(qt / "x64/lib")], target_lang="c++")
    environment = dict(os.environ)
    environment["PATH"] = str(ROOT / "x64/Development") + os.pathsep + environment.get("PATH", "")
    environment["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(qt / "x64/plugins/platforms")
    report = tmp_path / "report.json"
    preview = Path(os.environ.get("BREAD_ANNOTATION_PREVIEW", str(tmp_path / "preview.png")))
    subprocess.run([str(executable), str(report), str(preview)], env=environment, check=True, capture_output=True,
                   timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    results = json.loads(report.read_text(encoding="utf-8"))
    assert results and all(results.values()), {key: value for key, value in results.items() if not value}
