import sys
from pathlib import Path
import atexit
import tempfile
import uuid
import shutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if config.option.basetemp is None:
        scratch = Path(tempfile.gettempdir()) / ("bread-packaging-tests-" + uuid.uuid4().hex)
        config.option.basetemp = str(scratch)
        from build_fs import long_path
        atexit.register(lambda: shutil.rmtree(long_path(scratch), ignore_errors=True))
