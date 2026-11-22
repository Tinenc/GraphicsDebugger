from src import console_events
from tests.test_shader_console import _load_module


def test_host_and_native_console_use_the_same_instance_scoped_journal(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    native=_load_module()
    first='123-'+'a'*32;second='456-'+'b'*32
    monkeypatch.setenv('REPLAYLAB_INSTANCE_ID',first)
    console_events.emit_console_event('shader','info','only first host')
    native.publish_console_event('shader','info','only first GUI')
    first_path=console_events._default_journal_directory()
    assert str(first_path)==native.default_console_journal_directory()
    monkeypatch.setenv('REPLAYLAB_INSTANCE_ID',second)
    console_events.emit_console_event('shader','info','only second host')
    native.publish_console_event('shader','info','only second GUI')
    assert first_path!=console_events._default_journal_directory()
    first_events=native._load_generic_console_entries()
    assert {row['text'] for row in first_events['entries']}=={'only second host','only second GUI'}
    monkeypatch.setenv('REPLAYLAB_INSTANCE_ID',first)
    assert {row['text'] for row in native._load_generic_console_entries()['entries']}=={'only first host','only first GUI'}


def test_task_binding_overrides_foreign_process_environment_for_host_logs(tmp_path,monkeypatch):
    from src.ipc_client import bind_direct_tool_call
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    monkeypatch.setenv('REPLAYLAB_INSTANCE_ID','123-'+'a'*32)
    with bind_direct_tool_call('456-'+'b'*32,'c'*32):
        assert console_events._default_journal_directory().name=='456-'+'b'*32
