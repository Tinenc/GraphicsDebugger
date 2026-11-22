"""Exercise the deployed camera ABI, including degenerate rotations and concurrent readers."""
import json,os,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
@pytest.mark.skipif(sys.platform!='win32',reason='Windows native camera ABI')
def test_camera_degenerate_rotation_and_concurrent_reads(tmp_path):
 from setuptools._distutils.ccompiler import new_compiler
 source=tmp_path/'camera_probe.cpp'
 source.write_text(r'''#include <windows.h>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <limits>
#include <thread>
#include <vector>
#include "api/replay/renderdoc_replay.h"
int main(int argc,char **argv){
 HMODULE module=LoadLibraryA(argv[1]);if(!module)return 2;
 auto init=(decltype(&RENDERDOC_InitCamera))GetProcAddress(module,"RENDERDOC_InitCamera");
 auto orbit=(decltype(&RENDERDOC_SetMeshPreviewOrbit))GetProcAddress(module,"RENDERDOC_SetMeshPreviewOrbit");
 auto step=(decltype(&RENDERDOC_RotateMeshPreviewOrbit))GetProcAddress(module,"RENDERDOC_RotateMeshPreviewOrbit");
 if(!init||!orbit)return 3;
 ICamera *cam=init(CameraType::Arcball);cam->SetArcballDistance(2);cam->SetPosition(0,0,0);
 cam->RotateArcball(1,0,2,0);
 auto finite=[](FloatVector v){return std::isfinite(v.x)&&std::isfinite(v.y)&&std::isfinite(v.z);};
 const bool collinear=finite(cam->GetRight())&&finite(cam->GetForward());
 cam->ResetArcball();cam->SetPosition(1,2,3);cam->SetPosition(std::numeric_limits<float>::quiet_NaN(),0,0);
 const auto p=cam->GetPosition();const bool badInput=finite(p)&&p.x==1&&p.y==2&&p.z==3;
 cam->SetPosition(0,0,0);cam->SetArcballDistance(2);cam->ResetArcball();
 std::atomic<bool> valid(true);std::vector<std::thread> readers;
 for(int i=0;i<3;++i)readers.emplace_back([&](){for(int j=0;j<30000;++j){auto v=cam->GetForward();
  if(!finite(v)||std::abs(v.x*v.x+v.y*v.y+v.z*v.z-1)>1e-4f)valid=false;
  if(!finite(cam->GetRight())||!finite(cam->GetUp())||!finite(cam->GetPosition()))valid=false;}});
 for(int j=0;j<30000;++j){orbit(cam,j*.001f,j*.002f);cam->SetPosition(j*.0001f,0,0);}
 for(auto &reader:readers)reader.join();
 auto projected = [](FloatVector before, FloatVector after, FloatVector axis) {
   return (after.x-before.x)*axis.x+(after.y-before.y)*axis.y+(after.z-before.z)*axis.z;
 };
 bool horizontal = true, bothPoles = step != nullptr, rolledPitch = step != nullptr;
 ICamera *fly = init(CameraType::FPSLook);
 for(ICamera *preview : {cam, fly}) {
   auto orient = [&](float yaw, float pitch) {
     if(preview==cam) orbit(cam,yaw,pitch);
     else fly->SetFPSRotation(-pitch,-yaw,0.0f);
   };
   for(float pitch : {-1.8f,-1.3f,1.3f,1.8f}) {
     orient(.6f,pitch);
     const FloatVector before=preview->GetForward(), right=preview->GetRight();
     if(step) step(preview,.025f,0.0f,preview->GetUp().y<0.0f);
     else orient(.625f,pitch);
     horizontal &= projected(before,preview->GetForward(),right)>.005f;
   }
   if(step) {
     for(float sign : {-1.0f,1.0f}) {
       orient(.6f,sign*1.5f);
       for(int i=0;i<20;i++) {
         const FloatVector before=preview->GetForward(), up=preview->GetUp();
         step(preview,0.0f,sign*.01f,false);
         const FloatVector after=preview->GetForward();
         bothPoles &= finite(after) && sign*projected(before,after,up)<-.008f;
       }
       bothPoles &= preview->GetUp().y<0.0f;
     }
     if(preview==cam) {
       cam->ResetArcball();
       cam->RotateArcball(1,0,0,1);
     } else {
       fly->SetFPSRotation(0,0,-1.57079632679f);
     }
     const FloatVector before=preview->GetForward();
     step(preview,0.0f,.025f,false);
     rolledPitch &= std::abs(preview->GetForward().y-before.y)>.02f;
   }
 }
 fly->Shutdown();cam->Shutdown();
 std::printf("{\"collinearRotationFinite\":%s,\"nonFiniteInputRejected\":%s,\"concurrentBasisFinite\":%s,\"horizontalDirectionAcrossPoles\":%s,\"continuousVerticalOrbit\":%s,\"verticalOrbitEscapesSideRoll\":%s}\n",
    collinear?"true":"false",badInput?"true":"false",valid?"true":"false",
    horizontal?"true":"false",bothPoles?"true":"false",rolledPitch?"true":"false");
}''',encoding='utf-8')
 compiler=new_compiler(compiler='msvc');objects=compiler.compile([str(source)],output_dir=str(tmp_path),include_dirs=[str(ROOT/'renderdoc')],extra_postargs=['/EHsc','/std:c++17','/MD','/DRENDERDOC_PLATFORM_WIN32'])
 exe=tmp_path/'camera_probe.exe';compiler.link_executable(objects,str(exe.with_suffix('')),target_lang='c++')
 env=dict(os.environ);env['PATH']=str(ROOT/'x64/Development')+os.pathsep+env.get('PATH','')
 candidate=Path(os.environ.get('BREAD_CAMERA_CANDIDATE',str(ROOT/'x64/Development/renderdoc.dll')))
 result=subprocess.run([str(exe),str(candidate)],env=env,capture_output=True,text=True,check=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
 evidence=json.loads(next(line for line in result.stdout.splitlines()if line.startswith('{')));print('Camera native:',evidence);assert all(evidence.values()),evidence
 if os.environ.get('BREAD_CAMERA_BEFORE'):
  before=subprocess.run([str(exe),os.environ['BREAD_CAMERA_BEFORE']],env=env,capture_output=True,text=True,check=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
  previous=json.loads(next(line for line in before.stdout.splitlines()if line.startswith('{')))
  print('Previous camera:',previous)
  assert not previous['horizontalDirectionAcrossPoles'],previous
