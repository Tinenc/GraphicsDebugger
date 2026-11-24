"""Compare original and persistent snapshot presentation in an isolated replay.

Run with run_bundled_replay_python.ps1. This reads the named capture without
changing its contents or any running qrenderdoc instance. PNGs contain complete
headless output readbacks; both lanes use identical presentation settings.
Pass --native on Windows for GL/GLES to also compare the real default
framebuffer after the presentation blit, which headless readback cannot test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib


def png(path, pixels, width, height):
    if len(pixels) != width * height * 3:
        raise RuntimeError('Incomplete RGB output readback')
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\0' + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--event', type=int)
    parser.add_argument('--resource', type=int)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    import renderdoc as rd
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = {'capture': str(Path(args.capture).resolve()), 'cases': [], 'ok': False}
    def publish():
        (root / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    publish()
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    cap, ctrl, output, window = None, None, None, None
    try:
        rd.SetDebugLogFile(str(root / 'replay.log'))
        cap = rd.OpenCaptureFile()
        status = cap.OpenFile(args.capture, '', None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError('OpenFile: ' + str(status))
        report['driver'] = cap.DriverName()
        if args.native and report['driver'] not in ('OpenGL', 'OpenGLES'):
            raise RuntimeError('--native requires an OpenGL or OpenGLES capture')
        publish()
        status, ctrl = cap.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError('OpenCapture: ' + str(status))
        def flatten(actions):
            for action in actions:
                yield action
                for child in flatten(action.children):
                    yield child
        actions = list(flatten(ctrl.GetRootActions()))
        event = args.event or max(int(a.eventId) for a in actions)
        report['event'] = event
        ctrl.SetFrameEvent(event, True)
        textures = list(ctrl.GetTextures())
        report['swapbuffers'] = [{'resource': str(t.resourceId), 'format': t.format.Name(),
                                 'width': t.width, 'height': t.height, 'flags': str(t.creationFlags)}
                                for t in textures if t.creationFlags & rd.TextureCategory.SwapBuffer]
        def number(resource):
            return int(str(resource).split('::')[-1])
        texture = next((t for t in textures if number(t.resourceId) == args.resource), None) if args.resource else next(
            t for t in textures if t.creationFlags & rd.TextureCategory.SwapBuffer and not t.creationFlags & rd.TextureCategory.DepthTarget)
        if texture is None:
            raise RuntimeError('Requested texture not found')
        report['texture'] = {'resource': str(texture.resourceId), 'format': texture.format.Name(),
                             'width': texture.width, 'height': texture.height, 'flags': str(texture.creationFlags)}
        width, height = min(960, int(texture.width)), min(432, int(texture.height))
        if args.native:
            import ctypes
            from ctypes import wintypes
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.CreateWindowExW.restype = wintypes.HWND
            user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            window = user.CreateWindowExW(0, 'STATIC', '', 0x80000000, 0, 0, width, height, None, None, None, None)
            if not window:
                raise ctypes.WinError(ctypes.get_last_error())
            win = rd.CreateWin32WindowingData(window)
            gl = ctypes.WinDLL('opengl32')
            gl.glIsEnabled.argtypes, gl.glIsEnabled.restype = [ctypes.c_uint], ctypes.c_ubyte
            gl.wglGetProcAddress.argtypes, gl.wglGetProcAddress.restype = [ctypes.c_char_p], ctypes.c_void_p
            bind = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(gl.wglGetProcAddress(b'glBindFramebuffer'))
            attachment = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                            ctypes.POINTER(ctypes.c_int))(
                                                gl.wglGetProcAddress(b'glGetFramebufferAttachmentParameteriv'))
            def native_data(label):
                enabled = bool(gl.glIsEnabled(0x8db9))
                bind(0x8d40, 0)
                encoding = ctypes.c_int()
                attachment(0x8d40, 0x405 if report['driver'] == 'OpenGLES' else 0x402,
                           0x8210, ctypes.byref(encoding))
                # GLES exposes the default back buffer, not GL_FRONT. Both swap-chain images
                # contain this case after the two identical presentations below.
                gl.glReadBuffer(0x405)
                data = (ctypes.c_ubyte * (width * height * 3))()
                gl.glPixelStorei(0x0d05, 1)
                gl.glReadPixels(0, 0, width, height, 0x1907, 0x1401, data)
                raw = bytes(data)
                raw = b''.join(raw[y * width * 3:(y + 1) * width * 3] for y in reversed(range(height)))
                png(root / (label + '-native.png'), raw, width, height)
                return {'framebufferSrgb': enabled, 'defaultFramebufferEncoding': hex(encoding.value),
                        'sha256': hashlib.sha256(raw).hexdigest(), 'mean': sum(raw)/len(raw)}
        else:
            win = rd.CreateHeadlessWindowingData(width, height)
        output = ctrl.CreateOutput(win, rd.ReplayOutputType.Texture)
        for cast_name in ('Typeless', 'UNorm', 'UNormSRGB'):
            for gamma in (False, True):
                source = rd.TextureDisplay()
                source.resourceId = texture.resourceId
                source.typeCast = getattr(rd.CompType, cast_name)
                source.scale = -1.0
                source.rangeMin, source.rangeMax = 0.0, 1.0
                source.red, source.green, source.blue, source.alpha = True, True, True, False
                source.linearDisplayAsGamma = gamma
                output.SetTextureDisplay(source)
                output.Display()
                if args.native:
                    output.SetTextureDisplay(source)
                    output.Display()
                native_original = native_data(cast_name + str(gamma) + '-original') if args.native else None
                original = bytes(output.ReadbackOutputTexture())
                snapshot = rd.TextureDisplay()
                resolved = ctrl.ResolveEventTextureSnapshot(event, source, snapshot)
                row = {'cast': cast_name, 'gamma': gamma, 'resolved': resolved,
                       'originalSha256': hashlib.sha256(original).hexdigest()}
                row['nativeOriginal'] = native_original
                if resolved:
                    row.update(snapshotResource=str(snapshot.resourceId),
                               snapshotCast=str(snapshot.typeCast), snapshotGamma=snapshot.linearDisplayAsGamma)
                    output.SetTextureDisplay(snapshot)
                    output.Display()
                    if args.native:
                        output.SetTextureDisplay(snapshot)
                        output.Display()
                    row['nativeSnapshot'] = native_data(cast_name + str(gamma) + '-snapshot') if args.native else None
                    cached = bytes(output.ReadbackOutputTexture())
                    row.update(snapshotSha256=hashlib.sha256(cached).hexdigest(), identical=cached == original,
                               meanAbsoluteError=sum(abs(a - b) for a, b in zip(cached, original)) / max(1, len(original)),
                               maxAbsoluteError=max(abs(a - b) for a, b in zip(cached, original)))
                    if args.native:
                        row['nativeIdentical'] = (row['nativeOriginal']['sha256'] == row['originalSha256'] and
                                                  row['nativeSnapshot']['sha256'] == row['snapshotSha256'])
                    label = cast_name + ('-gamma' if gamma else '-raw')
                    png(root / (label + '-original.png'), original, width, height)
                    png(root / (label + '-snapshot.png'), cached, width, height)
                report['cases'].append(row)
                publish()
        save = rd.TextureSave()
        save.resourceId, save.destType, save.alpha = texture.resourceId, rd.FileType.PNG, rd.AlphaMapping.Discard
        report['saveTexture'] = str(ctrl.SaveTexture(save, str(root / 'export.png')))
        report['ok'] = all(row.get('identical', False) and row.get('nativeIdentical', not args.native)
                           for row in report['cases'])
        publish()
        print(json.dumps({'ok': report['ok'], 'cases': len(report['cases']), 'output': str(root)}, ensure_ascii=True))
        if not report['ok']:
            raise RuntimeError('Presentation differs; inspect result.json and the native output PNGs')
    finally:
        if output is not None:
            output.Shutdown()
        if window is not None:
            user.DestroyWindow(window)
        if ctrl is not None:
            ctrl.Shutdown()
        if cap is not None:
            cap.Shutdown()
        rd.ShutdownReplay()


if __name__ == '__main__':
    main()
