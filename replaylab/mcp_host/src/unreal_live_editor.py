"""Bread's persistent Editor-thread bridge. Loaded only in the selected UE process."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import traceback

import unreal


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def main():
    startup = Path(os.environ["BREAD_UNREAL_LIVE_REQUEST"]).resolve()
    settings = json.loads(startup.read_bytes())
    project = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve()
    if settings.get("schema") != "bread-live-unreal/v1" or Path(settings["project"]) != project:
        raise RuntimeError("Bread live connection project does not match this Editor")
    directory = startup.parent
    if Path(settings["script"]).resolve() != Path(__file__).resolve():
        raise RuntimeError("Bread live connection script identity mismatch")
    state = {"busy": False}

    def tick(unused_delta):
        if state["busy"]:
            return
        for path in sorted((directory / "requests").glob("*.request.json")):
            job_id = path.name.removesuffix(".request.json")
            if not re.fullmatch(r"[a-f0-9]{32}", job_id):
                continue
            request = json.loads(path.read_bytes())
            if request.get("nonce") != settings["nonce"] or request.get("jobId") != job_id:
                continue
            code = request.get("code")
            if not isinstance(code, str) or len(code.encode("utf-8")) > 120000:
                continue
            path.replace(path.with_name(job_id + ".executing.json"))
            state["busy"] = True
            receipt = {"schema": settings["schema"], "nonce": settings["nonce"], "jobId": job_id,
                       "pid": os.getpid(), "project": str(project)}
            output = io.StringIO()
            try:
                namespace = {"unreal": unreal, "__name__": "__bread_editor_job__"}
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    exec(compile(code, str(path), "exec"), namespace)
                result = namespace.get("result")
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 512 * 1024:
                    raise ValueError("Editor result exceeds 512 KiB. Save large artifacts to project evidence.")
                receipt.update(status="executed", result=result)
            except Exception:
                receipt.update(status="failed", diagnostic=traceback.format_exc())
            finally:
                state["busy"] = False
            receipt["output"] = output.getvalue()[-12000:]
            write_json(path.with_name(job_id + ".response.json"), receipt)
            break

    # Keep callback and state reachable while the interactive Editor owns the connection.
    unreal._bread_live_connection = {"state": state, "callback": tick,
                                     "handle": unreal.register_slate_post_tick_callback(tick)}
    ready = {**settings, "pid": os.getpid(),
             "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(directory / "ready.json", ready)


if __name__ == "__main__":
    main()
