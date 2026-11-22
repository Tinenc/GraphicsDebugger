"""Actual Qt texture filtering, cache invalidation and UV tiling interaction."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform!='win32',reason='Windows Qt/MSVC fixture')
def test_channels_cache_and_space_tiling(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler
    probe=tmp_path/'mesh_display.cpp'
    probe.write_text(r'''
#include <QApplication>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include "Windows/MeshUVView.cpp"
QColor Formatter::m_DarkChecker=QColor(55,55,55);
QColor Formatter::m_LightChecker=QColor(80,80,80);
int main(int argc,char **argv){
 QApplication app(argc,argv);QJsonObject result;
 auto data=std::make_shared<MeshUVData>();
 data->texture=QImage(2,2,QImage::Format_ARGB32);data->texture.fill(qRgba(64,128,192,32));
 bool masks=true,cache=true;
 for(unsigned int mask=0;mask<16;mask++){
   data->textureChannels=mask;const auto image=data->displayTexture();const auto c=image.pixelColor(0,0);
   const bool single=mask&&(mask&(mask-1))==0;
   const int gray=mask==1?64:mask==2?128:mask==4?192:32;
   masks&=single?(c.red()==gray&&c.green()==gray&&c.blue()==gray&&c.alpha()==255):
     (c.red()==((mask&1)?64:0)&&c.green()==((mask&2)?128:0)&&c.blue()==((mask&4)?192:0)&&c.alpha()==((mask&8)?32:255));
   cache&=data->displayTexture().cacheKey()==image.cacheKey();
 }
 result["allSixteenChannelCombinations"]=masks;result["unchangedTextureUsesCache"]=cache;
 const auto oldKey=data->displayTexture().cacheKey();data->texture.setPixel(0,0,qRgba(10,20,30,40));
 result["sourceMutationInvalidatesCache"]=data->displayTexture().cacheKey()!=oldKey&&data->displayTexture().pixelColor(0,0).alpha()==40;
 data->textureChannels=7;data->texture.fill(QColor(20,100,180));
 auto geometry=std::make_shared<MeshUVGeometry>();geometry->wire.addRect(QRectF(.2,.2,.5,.5));data->geometry=geometry;
 MeshUVView view(data,nullptr);view.resize(400,300);view.textureChanged();
 auto before=view.grab().toImage();const auto uvZoom=view.property("uvZoom");
 QKeyEvent space(QEvent::KeyPress,Qt::Key_Space,Qt::NoModifier);app.sendEvent(&view,&space);
 auto tiled=view.grab().toImage();
 result["spaceRepeatsOutsideUnitTile"]=view.property("uvTextureTiled").toBool()&&tiled.pixelColor(5,150)==QColor(20,100,180)&&before.pixel(5,150)!=tiled.pixel(5,150);
 QKeyEvent repeat(QEvent::KeyPress,Qt::Key_Space,Qt::NoModifier,QString(),true);app.sendEvent(&view,&repeat);
 result["repeatDoesNotToggleAgain"]=view.property("uvTextureTiled").toBool();
 app.sendEvent(&view,&space);result["secondSpaceRestoresWithoutCameraChange"]=before==view.grab().toImage()&&view.property("uvZoom")==uvZoom;
 auto pickData=std::make_shared<MeshUVData>();auto pickGeometry=std::make_shared<MeshUVGeometry>();
 pickGeometry->vertices={{{-.8f,-.5f,0,1},{.1,.2},10,true},{{-.2f,-.5f,0,1},{.4,.2},11,true},{{-.5f,.4f,0,1},{.25,.65},12,true},
                         {{.2f,-.5f,0,1},{.6,.2},20,true},{{.8f,-.5f,0,1},{.9,.2},21,true},{{.5f,.4f,0,1},{.75,.65},22,true}};
 pickGeometry->triangles={{{0,1,2}},{{3,4,5}}};pickData->geometry=pickGeometry;pickData->mode=MeshUVData::Selection::Point;
 MeshUVView pickView(pickData,nullptr);pickView.resize(400,300);int callbacks=0;pickView.selectionChanged=[&](){++callbacks;};
 QMatrix4x4 identity;pickView.selectMeshLinked(QPointF(100,160),QSize(400,300),identity,false);
 result["linkedMeshUsesHoveredFaceWithoutClick"]=pickData->selected==QSet<quint64>({10,11,12})&&pickData->mode==MeshUVData::Selection::Point&&callbacks==1;
 pickView.selectMeshLinked(QPointF(300,160),QSize(400,300),identity,false);
 result["linkedMeshAddsOnlyHoveredIsland"]=pickData->selected==QSet<quint64>({10,11,12,20,21,22})&&callbacks==2;
 pickData->selected.clear();pickView.selectLinkedAt(QPointF(269,140));
 result["linkedUVUsesHoveredFaceWithoutClick"]=pickData->selected==QSet<quint64>({20,21,22})&&pickData->mode==MeshUVData::Selection::Point&&callbacks==3;
 pickView.selectLinkedAt(QPointF(1,1));result["linkedEmptySpaceKeepsSelection"]=pickData->selected==QSet<quint64>({20,21,22});
 QFile out(QString::fromLocal8Bit(argv[1]));out.open(QIODevice::WriteOnly);out.write(QJsonDocument(result).toJson());
}
''',encoding='utf-8')
    qt=ROOT/'qrenderdoc/3rdparty/qt';compiler=new_compiler(compiler='msvc')
    objects=compiler.compile([str(probe)],output_dir=str(tmp_path),
        include_dirs=[str(ROOT/'qrenderdoc'),str(ROOT/'renderdoc/api/replay'),str(qt/'include'),*[str(qt/'include'/n)for n in ('QtCore','QtGui','QtWidgets')]],
        extra_postargs=['/EHsc','/std:c++14','/MD','/DQT_NO_DEBUG','/DRENDERDOC_PLATFORM_WIN32','/DRELEASE','/utf-8'])
    executable=tmp_path/'mesh_display.exe'
    compiler.link_executable(objects,str(executable.with_suffix('')),libraries=['Qt5Widgets','Qt5Gui','Qt5Core','opengl32'],
        library_dirs=[str(qt/'x64/lib')],target_lang='c++')
    env=dict(os.environ);env['PATH']=str(ROOT/'x64/Development')+os.pathsep+env.get('PATH','')
    env['QT_QPA_PLATFORM_PLUGIN_PATH']=str(qt/'x64/plugins/platforms')
    report=tmp_path/'result.json'
    subprocess.run([str(executable),str(report)],env=env,check=True,capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    result=json.loads(report.read_text());assert all(result.values()),result
