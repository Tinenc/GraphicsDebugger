from __future__ import annotations

import sys
import time


# Deliberately omit the JSON-RPC newline. The Host must reject this while the
# frame is still bounded, before an SDK parser accumulates arbitrary stdout.
sys.stdout.write("x" * (4 * 1024 * 1024 + 64 * 1024))
sys.stdout.flush()
time.sleep(10)
