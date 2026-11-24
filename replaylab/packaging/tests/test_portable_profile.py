import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_portable_profile import discard_generated_bytecode, profile_environment


def test_automatic_profile_does_not_inherit_a_config_override(tmp_path):
    root = tmp_path / '中文-😀'
    env, expected = profile_environment({'RENDERDOC_CONFIG_DIR': 'C:/live',
        'REPLAYLAB_CONFIG_DIR': 'C:/private', 'PYTHONPATH': 'C:/dev', 'PATH': 'C:/dev',
        'LOCALAPPDATA': 'C:/real', 'SystemRoot': 'C:/Windows'}, root)
    assert 'RENDERDOC_CONFIG_DIR' not in env
    assert 'REPLAYLAB_CONFIG_DIR' not in env and 'PYTHONPATH' not in env
    assert expected == root / 'user/AppData/Local/RenderDocBread/ui'
    assert env['BREAD_PROFILE_SMOKE_EXPECTED'] == str(expected)
    assert Path(env['LOCALAPPDATA']).is_dir()
    assert env['PATH'] == str(Path('C:/Windows/System32'))


def test_explicit_unicode_override_is_distinct_from_the_automatic_profile(tmp_path):
    env, expected = profile_environment({}, tmp_path, explicit=True)
    assert env['RENDERDOC_CONFIG_DIR'] == str(expected)
    assert expected == tmp_path / 'explicit-配置-😀'


@pytest.mark.parametrize('fail', [False, True])
def test_gui_bytecode_cleanup_preserves_existing_files_and_handles_failures(tmp_path, fail):
    cache = tmp_path / 'PySide2/__pycache__'
    cache.mkdir(parents=True)
    existing = cache / 'existing.pyc'
    existing.write_bytes(b'previous user cache')
    generated = cache / 'generated.pyc'
    source = tmp_path / 'PySide2/source.py'
    source.write_text('source')
    try:
        with discard_generated_bytecode(tmp_path):
            generated.write_bytes(b'new GUI cache')
            if fail:
                raise RuntimeError('GUI validation failed')
    except RuntimeError:
        assert fail
    assert existing.read_bytes() == b'previous user cache'
    assert source.read_text() == 'source'
    assert not generated.exists()
