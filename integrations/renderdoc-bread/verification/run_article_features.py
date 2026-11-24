"""Build a private D3D11 fixture and verify the delivered annotation/mesh UI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from setuptools._distutils.ccompiler import new_compiler

parser = argparse.ArgumentParser()
parser.add_argument('--app', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
repo = Path(__file__).resolve().parents[3]
app = args.app.resolve(strict=True)
root = Path(tempfile.mkdtemp(prefix='bread-df-article-'))
for name in ('config', 'local', 'roaming'):
    (root / name).mkdir()
source = (Path(__file__).parent / 'capture_triangle.cpp').read_text(encoding='utf-8')
source = source.replace('"../../../renderdoc/api/app/renderdoc_app.h"', '"renderdoc/api/app/renderdoc_app.h"')
source = source.replace('float4 PS(VOut i):SV_TARGET { return Tint * float4(i.uv,abs(i.normal.z),1); }',
    'Texture2D<float4> Pattern : register(t0); SamplerState LinearClamp : register(s0);\n'
    'float4 PS(VOut i):SV_TARGET { return Tint * Pattern.Sample(LinearClamp,i.uv); }')
source = source.replace('api->StartFrameCapture(device.Get(), nullptr);', '''
    D3D11_TEXTURE2D_DESC patternDesc=td;
    patternDesc.Width=2; patternDesc.Height=2; patternDesc.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    unsigned int patternPixels[]={0xff4060ff,0xffe0b040,0xff60d040,0xffd050a0};
    D3D11_SUBRESOURCE_DATA patternData={patternPixels,8,0};
    ComPtr<ID3D11Texture2D> pattern; ComPtr<ID3D11ShaderResourceView> patternView;
    check(device->CreateTexture2D(&patternDesc,&patternData,&pattern));
    check(device->CreateShaderResourceView(pattern.Get(),nullptr,&patternView));
    D3D11_SAMPLER_DESC samplerDesc={}; samplerDesc.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    samplerDesc.AddressU=samplerDesc.AddressV=samplerDesc.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;
    samplerDesc.MaxLOD=D3D11_FLOAT32_MAX;
    ComPtr<ID3D11SamplerState> sampler; check(device->CreateSamplerState(&samplerDesc,&sampler));
    api->StartFrameCapture(device.Get(), nullptr);
    auto patternPtr=patternView.Get(); ctx->PSSetShaderResources(0,1,&patternPtr);
    auto samplerPtr=sampler.Get(); ctx->PSSetSamplers(0,1,&samplerPtr);
    ''')
source = source.replace('ctx->DrawIndexed(3,0,0); ctx->Flush();',
    'ctx->DrawIndexed(3,0,0); ctx->DrawIndexed(3,0,0); ctx->Flush();')
fixture = root / 'article_capture.cpp'
fixture.write_text(source, encoding='utf-8')
compiler = new_compiler(compiler='msvc')
objects = compiler.compile([str(fixture)], output_dir=str(root), include_dirs=[str(repo)],
    extra_postargs=['/EHsc', '/std:c++14', '/MD', '/utf-8'])
executable = root / 'article_capture.exe'
compiler.link_executable(objects, str(executable.with_suffix('')),
    libraries=['d3d11', 'd3dcompiler', 'dxguid'], target_lang='c++')
capture_result = subprocess.run([str(executable), str(app.parent/'TinecmaTool.dll'), str(root/'article')],
    capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
captures = list(root.glob('*.rdc'))
assert len(captures) == 1, capture_result.stdout
capture = captures[0]
before = hashlib.sha256(capture.read_bytes()).hexdigest()
(root/'config/UI.config').write_text(json.dumps({
    'rdocConfigData':1,'UILanguage':'zh_CN','Analytics_TotalOptOut':True,
    'CheckUpdate_AllowChecks':False,'Tips_HasSeenFirst':True,'UIStyle':'ultra'}),encoding='utf-8')
environment = dict(os.environ, RENDERDOC_CONFIG_DIR=str(root/'config'),
    LOCALAPPDATA=str(root/'local'), APPDATA=str(root/'roaming'), BREAD_ARTICLE_REPO=str(repo),
    RD_ANNOTATION_CAPTURE=str(capture), BREAD_DIAG_UI_REPORT=str(root/'report.json'),
    BREAD_DIAG_UI_IMAGE=str(root/'annotation.png'))
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps({'run':str(root),'app':str(app),'capture':str(capture)},indent=2),encoding='utf-8')
with (root/'gui.log').open('w',encoding='utf-8') as log:
    process = subprocess.Popen([str(app),'--ui-python',str(Path(__file__).with_name('article_features_ui.py'))],
        cwd=app.parent, env=environment, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=20)
        raise
report=json.loads((root/'report.json').read_text(encoding='utf-8'))
report.update(run=str(root),captureSha256=before,
    captureHashUnchanged=hashlib.sha256(capture.read_bytes()).hexdigest()==before,guiExit=process.returncode)
args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
assert report['ok'] and report['captureHashUnchanged'] and process.returncode == 0, report
print(json.dumps({'status':'passed','checks':len(report['checks']),'run':str(root)},ensure_ascii=False))
