"""Run a copied current RenderDocBread binary in a private diagnostic profile.

The parent owns one Popen handle and one optional Frida attachment. It never attaches to,
terminates or modifies an existing application. All writable paths are below this worktree.

Inputs are the SHA-verified snapshots under output/baseline-identity and output/runtime-latest.
Task-local dependencies are under output/tooling. Example rerun:
python -B util/perf/replay_audit_driver.py --label rerun-001 --header-control --runtime-name runtime-latest
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'output' / 'tooling'))


def write_json(path, value):
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    deadline = time.monotonic() + 2
    while True:
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def contained(path):
    resolved = path.resolve()
    if ROOT not in resolved.parents:
        raise ValueError('Diagnostic path escapes this worktree ' + str(path))
    for ancestor in (path,) + tuple(path.parents):
        if ancestor.exists() and ancestor.is_symlink():
            raise ValueError('Diagnostic path traverses a symlink')
        if ancestor == ROOT:
            break
    return resolved


def gpu_sample():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,memory.free,utilization.gpu',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        values = result.stdout.strip().split(',')
        return dict(zip(('usedMiB', 'freeMiB', 'utilizationPercent'), [int(value) for value in values]))
    except Exception as error:
        return {'error': str(error)}


class AuditRun:
    def __init__(self, label, runtime_name='runtime-current', timing=True):
        self.directory = contained(ROOT / 'output' / 'runs' / label)
        if self.directory.exists():
            raise ValueError('Use a fresh run label')
        for name in ('config', 'local', 'roaming', 'temp'):
            (self.directory / name).mkdir(parents=True)
        self.runtime = contained(ROOT / 'output' / runtime_name)
        self.timing = timing
        self.receipt = {'schema': 'bread-isolated-replay-audit/v1', 'worktree': str(ROOT),
                        'runtime': str(self.runtime), 'label': label, 'phases': [],
                        'otherProcessesModified': False, 'gpuExclusive': False,
                        'gpuBefore': gpu_sample(), 'startedEpoch': time.time(),
                        'nativeTimingEnabled': timing, 'gpuProcessSamples': []}
        self.process = None
        self.session = None
        self.script = None
        self.samples = []
        self.last_memory = 0
        self.capture_override = None
        self.generic_capture = False

    def save(self):
        write_json(self.directory / 'run.json', self.receipt)
        write_json(self.directory / 'memory.json', self.samples)

    def launch(self):
        profile = contained(Path(self.data_profile)) if getattr(self, 'data_profile', None) else self.directory
        if profile != self.directory:
            if not (profile/'config/UI.config').is_file():
                raise ValueError('A retained data profile must already have its own UI configuration')
            self.receipt['retainedDataProfile'] = str(profile)
        self.receipt['nativeBinaries'] = [
            {'path': str(self.runtime / name),
             'sha256': hashlib.sha256((self.runtime / name).read_bytes()).hexdigest()}
            for name in ('RenderDocBread.exe', 'renderdoc.dll')]
        config = {'rdocConfigData': 1, 'Analytics_TotalOptOut': True,
                  'CheckUpdate_AllowChecks': False, 'Tips_HasSeenFirst': True,
                  'AlwaysLoad_Extensions': [], 'AlwaysReplayLocally': True}
        if profile == self.directory:
            write_json(self.directory / 'config' / 'UI.config', config)
        if profile == self.directory and getattr(self, 'profile_template', None):
            for name in ('UI.config', 'DefaultLayout.config', 'renderdoc.conf'):
                source = Path(self.profile_template) / name
                (self.directory / 'config' / name).write_bytes(source.read_bytes())
            self.receipt['preservedProfile'] = str(self.profile_template)
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(('REPLAYLAB_', 'RENDERDOC_', 'PYTHON', 'QT_', 'QML'))}
        environment.update({
            'RENDERDOC_CONFIG_DIR': str(profile / 'config'),
            'APPDATA': str(profile / 'roaming'), 'LOCALAPPDATA': str(profile / 'local'),
            'TEMP': str(self.directory / 'temp'), 'TMP': str(self.directory / 'temp'),
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'RENDERDOC_EID_TIMING': '1' if self.timing else '0', 'REPLAYLAB_EXTERNAL_MCP_AUTOSTART': '0',
            'BREAD_AUDIT_RUN': str(self.directory),
            'BREAD_AUDIT_REPLACEMENTS': str(ROOT / 'output' / 'baseline-identity' / 'replacement-inputs.json'),
            '_NT_SYMBOL_PATH': str(self.runtime), 'QT_PLUGIN_PATH': str(self.runtime / 'qtplugins'),
            'PATH': str(self.runtime) + os.pathsep + environment.get('PATH', '')})
        if self.generic_capture:
            environment['BREAD_AUDIT_GENERIC'] = '1'
        overrides = getattr(self, 'environment_overrides', {})
        if any(not key.startswith(('RENDERDOC_VULKAN_', 'RENDERDOC_D3D12_', 'RENDERDOC_D3D11_',
                                   'RENDERDOC_GL_', 'RENDERDOC_CBUFFER_',
                                   'RENDERDOC_UI_', 'RENDERDOC_REPLAY_EAGER_MESH_', 'BREAD_AUDIT_')) for key in overrides):
            raise ValueError('Only task-specific diagnostic environment overrides are accepted')
        environment.update(overrides)
        self.receipt['diagnosticEnvironment'] = overrides
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 4  # SW_SHOWNOACTIVATE, preserving the user's foreground window.
        if self.capture_override:
            capture_path = str(self.capture_override)
        else:
            context = read_json(ROOT / 'output' / 'baseline-identity' / 'get_current_renderdoc_context.json')
            capture_path = context['capture']['path']
        self.receipt['capturePath'] = capture_path
        command = [str(self.runtime / 'RenderDocBread.exe'), capture_path,
                   '--ui-python', str(ROOT / 'util' / 'perf' / 'replay_audit_ui.py')]
        self.stdout = (self.directory / 'stdout.log').open('wb')
        self.stderr = (self.directory / 'stderr.log').open('wb')
        self.process = subprocess.Popen(command, cwd=str(self.runtime), env=environment,
                                        stdout=self.stdout, stderr=self.stderr, startupinfo=startup)
        self.receipt['pid'] = self.process.pid
        self.save()
        print('Isolated audit PID', self.process.pid, flush=True)
        # Optional owned-child startup diagnostics; never attach to a user process.
        if getattr(self, 'after_spawn', None):
            self.after_spawn()
        self.wait_file(self.directory / 'state.json', lambda state: state.get('ready'),
                       getattr(self, 'startup_timeout', 240))
        self.receipt['ready'] = read_json(self.directory / 'state.json')
        self.save()

    def sample_memory(self, force=False):
        if not force and time.monotonic() - self.last_memory < 1:
            return
        self.last_memory = time.monotonic()
        import psutil
        try:
            process = psutil.Process(self.process.pid)
            memory = process.memory_info()
            self.samples.append({'epoch': time.time(), 'phase': self.receipt.get('currentPhase', 'startup'),
                                 'rssBytes': memory.rss, 'privateBytes': getattr(memory, 'private', memory.vms),
                                 'handles': process.num_handles(), 'threads': process.num_threads(),
                                 'gpuTotal': gpu_sample()})
        except psutil.Error:
            pass

    def gpu_process_memory(self, phase):
        command = (
            "$values = Get-Counter -Counter '\\GPU Process Memory(*)\\Dedicated Usage',"
            "'\\GPU Process Memory(*)\\Shared Usage' -ErrorAction SilentlyContinue; "
            "$values.CounterSamples | Where-Object { $_.InstanceName -like 'pid_%d_*' } | "
            "Select-Object InstanceName,Path,CookedValue | ConvertTo-Json -Compress" % self.process.pid)
        try:
            result = subprocess.run(['pwsh.exe', '-NoProfile', '-Command', command],
                                    capture_output=True, text=True, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if not result.stdout.strip():
                raise RuntimeError('GPU process counters unavailable ' + result.stderr.strip()[:500])
            values = json.loads(result.stdout)
            self.receipt['gpuProcessSamples'].append({'phase': phase, 'epoch': time.time(), 'counters': values})
        except Exception as error:
            self.receipt['gpuProcessSamples'].append({'phase': phase, 'error': str(error)})

    def wait_file(self, path, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sample_memory()
            if path.is_file():
                try:
                    value = read_json(path)
                except PermissionError:
                    time.sleep(0.005)
                    continue
                if predicate(value):
                    return value
                if value.get('lastError'):
                    raise RuntimeError(value['lastError'])
            if self.process.poll() is not None:
                raise RuntimeError('Isolated application exited ' + str(self.process.returncode))
            time.sleep(0.05)
        raise TimeoutError(str(path))

    def command(self, name, operation, timeout=180, **parameters):
        self.receipt['currentPhase'] = name
        self.sample_memory(force=True)
        self.save()
        if self.script:
            self.script.exports_sync.setphase(name)
        started = time.perf_counter()
        write_json(self.directory / 'command.json', {'id': name, 'operation': operation, **parameters})
        result = self.wait_file(self.directory / ('result-' + name + '.json'), lambda value: True, timeout)
        if self.script:
            write_json(self.directory / ('hooks-' + name + '.json'), self.script.exports_sync.drain())
        self.receipt['phases'].append({'name': name, 'operation': operation,
                                       'wallMs': (time.perf_counter() - started) * 1000,
                                       'ok': result.get('ok')})
        self.sample_memory(force=True)
        if not self.timing and (name in ('memory-warmup', 'final-status') or name.startswith('memory-batch-')):
            self.gpu_process_memory(name)
        self.save()
        print(name, 'ok=' + str(result.get('ok')), flush=True)
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'Command failed'))
        return result

    def attach(self):
        import frida
        self.session = frida.attach(self.process.pid)
        self.script = self.session.create_script((ROOT / 'util' / 'perf' / 'replay_audit_hooks.js').read_text(encoding='utf-8'))
        messages = []
        self.script.on('message', lambda message, data: messages.append(message))
        self.script.load()
        self.receipt['loadedNativeModules'] = self.script.exports_sync.modules()
        patterns = [
            '*WrappedVulkan*ContextReplayLog*', '*WrappedVulkan*Serialise_BeginCaptureFrame*',
            '*VulkanResourceManager*SerialiseImageStates*', '*WrappedVulkan*ApplyInitialContents*',
            '*WrappedVulkan*FlushQ*', '*WrappedVulkan*SubmitCmds*',
            '*ReplayController*ReplayEvent*', '*ReplayController*FetchPipelineState*',
            '*ReplayController*CommitSharedBufferPatches*', '*ReplayOutput*Display*',
            '*CaptureContext*RefreshUIStatus*', '*TextureViewer*OnEventChanged*',
            '*BufferViewer*OnEventChanged*', '*PipelineStateViewer*OnEventChanged*',
            '*VulkanReplay*GetBufferData*', '*VulkanReplay*GetTextureData*',
            '*WrappedVulkan*ApplyInitialState*', '*WrappedVulkan*vkQueueSubmit*']
        discovered = self.script.exports_sync.discover(patterns)
        write_json(self.directory / 'symbols.json', discovered)
        selected = []
        seen = set()
        for group in discovered:
            for entry in group.get('symbols', []):
                name = entry.get('name') or ''
                if name.startswith(('`', '_wrap_')) or any(word in name for word in
                        ('lambda', 'std::', 'operator()', '~', 'Invoker', 'call_', 'function<', 'dtor$')):
                    continue
                if entry['address'] in seen:
                    continue
                seen.add(entry['address'])
                selected.append(entry)
        attached = self.script.exports_sync.attach(selected)
        write_json(self.directory / 'hooks-attached.json', {'functions': attached, 'messages': messages})
        auxiliary = self.script.exports_sync.discover(['*SkipCurrentChunk*', '*Subresource*FromArray*'])
        write_json(self.directory / 'auxiliary-symbols.json', auxiliary)
        print('Attached diagnostic boundaries', len(attached), flush=True)

    def detach(self):
        if self.script:
            self.script.exports_sync.detach()
            self.script.unload()
            self.script = None
        if self.session:
            self.session.detach()
            self.session = None

    def header_control(self):
        self.command('header-original-inline', 'inline_cbuffer')
        self.command('header-original-scrub', 'navigate', events=[1286, 1291, 1296, 1301, 1296, 1291], repeats=2)
        self.command('project-gpu-counters', 'gpu_counters')
        self.command('header-original-fingerprint-a', 'fingerprint', event=1296)
        self.command('header-original-fingerprint-b', 'fingerprint', event=1296)
        self.command('header-original-times', 'headless', events=[1291, 1296], repeats=4)
        self.receipt['releaseHeaderControl'] = self.script.exports_sync.releaseheader(True)
        self.command('header-release-fingerprint-a', 'fingerprint', event=1296)
        self.command('header-release-fingerprint-b', 'fingerprint', event=1296)
        self.command('header-release-times', 'headless', events=[1291, 1296], repeats=4)
        self.command('header-release-scrub', 'navigate', events=[1286, 1291, 1296, 1301, 1296, 1291], repeats=2)
        self.command('header-release-cbuffer', 'cbuffer')
        self.command('header-release-inline', 'inline_cbuffer')
        self.script.exports_sync.releaseheader(False)
        self.command('header-restored-times', 'headless', events=[1291, 1296], repeats=2)

    def run(self, discover_only=False, header_control=False, memory_control=False, memory_header_control=False):
        try:
            self.launch()
            self.command('raw-status', 'status')
            self.command('raw-no-checkpoints', 'configure', checkpoints=False)
            if memory_control:
                self.command('project-apply', 'replacements', timeout=300)
                if memory_header_control:
                    self.attach()
                    self.script.exports_sync.stoptrace()
                    for mode, enabled in [('normal', False), ('release', True), ('returned', False)]:
                        self.script.exports_sync.releaseheader(enabled)
                        self.command('memory-' + mode + '-warmup', 'headless', events=[1291, 1296], repeats=5, recordDriver=False)
                        for batch in range(4):
                            self.command('memory-batch-' + mode + '-' + str(batch), 'headless',
                                         events=[1291, 1296], repeats=10, recordDriver=False)
                    self.script.exports_sync.releaseheader(False)
                    self.command('final-status', 'status')
                    self.receipt['complete'] = True
                    return
                self.command('memory-warmup', 'headless', events=[1291, 1296], repeats=5, recordDriver=False)
                for batch in range(6):
                    self.command('memory-batch-' + str(batch), 'headless', events=[1291, 1296], repeats=10, recordDriver=False)
                self.command('final-status', 'status')
                self.receipt['complete'] = True
                return
            self.command('raw-unhooked', 'headless', events=[1291, 1296], repeats=3)
            self.attach()
            if discover_only:
                self.command('raw-hooks-smoke', 'headless', events=[1291, 1296], repeats=2)
                return
            if header_control:
                self.command('project-apply', 'replacements', timeout=300)
                self.header_control()
                self.receipt['complete'] = True
                return
            self.command('raw-hooked', 'headless', events=[1291, 1296], repeats=3)
            self.command('raw-scrub', 'navigate', events=[1286, 1291, 1296, 1301, 1305, 1310, 1296, 1291], repeats=2)
            self.command('raw-gpu-counters', 'gpu_counters')
            self.command('project-apply', 'replacements', timeout=300)
            self.command('project-status', 'status')
            self.command('project-headless', 'headless', events=[1291, 1296], repeats=3)
            self.command('project-scrub', 'navigate', events=[1286, 1291, 1296, 1301, 1305, 1310, 1296, 1291], repeats=2)
            self.command('project-cbuffer', 'cbuffer')
            self.command('project-gpu-counters', 'gpu_counters')
            self.command('checkpoint-bounded-on', 'configure', checkpoints=True, budget=128 * 1024 * 1024, maxCheckpoints=1)
            self.command('checkpoint-bounded-trial', 'headless', events=[1286, 1291, 1296, 1301], repeats=3, force=False)
            self.command('checkpoint-status', 'status')
            self.command('checkpoint-release', 'configure', checkpoints=False)
            self.detach()
            self.command('memory-warmup', 'headless', events=[1291, 1296], repeats=5, recordDriver=False)
            for batch in range(4):
                self.command('memory-batch-' + str(batch), 'headless', events=[1291, 1296], repeats=10, recordDriver=False)
            self.command('final-status', 'status')
            self.receipt['complete'] = True
        except Exception:
            self.receipt['error'] = traceback.format_exc()
            raise
        finally:
            self.detach()
            if self.process and self.process.poll() is None:
                try:
                    self.command('audit-close', 'close', timeout=8)
                    self.process.wait(timeout=15)
                except Exception:
                    # Only the Popen-owned private diagnostic process is eligible for cleanup.
                    self.process.terminate()
                    self.process.wait(timeout=15)
            self.receipt['exitCode'] = self.process.returncode if self.process else None
            self.receipt['gpuAfter'] = gpu_sample()
            self.receipt['completedEpoch'] = time.time()
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--header-control', action='store_true')
    parser.add_argument('--memory-control', action='store_true')
    parser.add_argument('--memory-header-control', action='store_true')
    parser.add_argument('--runtime-name', default='runtime-current')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.label):
        raise ValueError('Invalid label')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.runtime_name):
        raise ValueError('Invalid runtime name')
    memory_control = args.memory_control or args.memory_header_control
    AuditRun(args.label, args.runtime_name, not memory_control).run(
        args.discover_only, args.header_control, memory_control, args.memory_header_control)


if __name__ == '__main__':
    main()
