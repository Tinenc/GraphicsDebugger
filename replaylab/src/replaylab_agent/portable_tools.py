"""Provider-neutral local tools. No Codex pipe, account or network service required.

The script executors are exposed only for the existing explicitly enabled local
execution preference. They are trusted local execution, not a security sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from contextlib import contextmanager

from .portable_capabilities import bundle_root, public_status, require_components


@contextmanager
def _scratch():
    temporary = tempfile.TemporaryDirectory(prefix="bread-tool-")
    try:
        yield temporary.name
    finally:
        # Windows job termination can briefly retain a descendant's redirected
        # file handle after the direct process has been reaped. Retry cleanup
        # only for this operation-owned directory; do not hide persistent errors.
        for attempt in range(6):
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))


def _run_owned(command, *, timeout, **kwargs):
    """Never share MCP stdin; close only this operation's Windows process tree."""
    from .connectors.acp import _WindowsJob
    with subprocess.Popen(command, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs) as process:
        job = _WindowsJob.assign(process.pid)
        try:
            process.wait(timeout=timeout)
            return process.returncode
        except BaseException:
            if job:
                job.close()
            process.kill()
            process.wait(timeout=5)
            raise
        finally:
            if job:
                job.close()


def execute(language: str, code: str, working_directory: str, timeout_seconds: int = 60) -> dict:
    if language not in {"python", "javascript"}:
        raise ValueError("Unknown runtime")
    require_components("python" if language == "python" else "node", "native", "custom-skills")
    return _execute(language, code, working_directory, timeout_seconds)


def _execute(language: str, code: str, working_directory: str, timeout_seconds: int = 60) -> dict:
    root = bundle_root()
    cwd = Path(working_directory).expanduser().resolve(strict=True)
    if not cwd.is_dir() or len(code.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Require an existing working directory and at most 1 MiB of code")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("PYTHON", "_PYI")) or key in {"VIRTUAL_ENV", "NODE_OPTIONS", "NODE_PATH"}:
            env.pop(key, None)
    env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "BREAD_CAPABILITIES_ROOT": str(root),
                "NODE_PATH": str(root / "node/node_modules"),
                "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers")})
    native = [root / "native/poppler/Library/bin", root / "native/poppler/bin"]
    native.append(root / "office/program")
    env["PATH"] = os.pathsep.join(str(p) for p in native if p.is_dir()) + os.pathsep + env.get("PATH", "")
    with _scratch() as work:
        script = Path(work) / ("main.py" if language == "python" else "main.cjs")
        if language == "python":
            script.write_text(code, encoding="utf-8")
            command = [str(root / "python/python.exe"), "-I", "-X", "utf8", "-B", str(script)]
        elif language == "javascript":
            # createRequire provides stable package lookup after relocation.
            prefix = "const {createRequire}=require('node:module'); const breadRequire=createRequire(" + json.dumps(str(root / "node/bread-entry.cjs")) + ");\n"
            script.write_text(prefix + code, encoding="utf-8")
            command = [str(root / "node/bin/node.exe"), str(script)]
        else:
            raise ValueError("Unknown runtime")
        out, err = Path(work) / "stdout", Path(work) / "stderr"
        with out.open("wb") as stdout, err.open("wb") as stderr:
            try:
                exit_code = _run_owned(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                       timeout=max(1, min(int(timeout_seconds), 300)))
                timed_out = False
            except subprocess.TimeoutExpired:
                exit_code, timed_out = None, True
        def bounded(path):
            with path.open("rb") as stream:
                text = stream.read(256 * 1024 + 1)
            return text[:256 * 1024].decode("utf-8", errors="replace"), len(text) > 256 * 1024
        stdout, truncated_out = bounded(out)
        stderr, truncated_err = bounded(err)
        return {"exitCode": exit_code, "timedOut": timed_out, "stdout": stdout, "stderr": stderr,
                "truncated": truncated_out or truncated_err}


def document(operation: str, path: str, content: dict | None = None) -> dict:
    """Portable basic creation/inspection; advanced work uses explicitly enabled scripts."""
    target = Path(path).expanduser().resolve()
    if operation not in {"create", "inspect"} or target.suffix.lower() not in {".docx", ".xlsx", ".pptx", ".pdf"}:
        raise ValueError("Supported: create/inspect DOCX, XLSX, PPTX and PDF")
    payload = {"operation": operation, "path": str(target), "content": content or {}}
    script = '''import json
from pathlib import Path
p=PAYLOAD
path=Path(p['path']); c=p['content']; kind=path.suffix.lower()
if p['operation']=='create':
 if path.exists(): raise ValueError('Target exists; choose a new path or edit explicitly')
 path.parent.mkdir(parents=True,exist_ok=True)
 if kind=='.docx':
  from docx import Document
  d=Document(); d.add_heading(str(c.get('title','')),0)
  for text in c.get('paragraphs',[]): d.add_paragraph(str(text))
  d.save(path)
 elif kind=='.xlsx':
  from openpyxl import Workbook
  d=Workbook(); s=d.active; s.title=str(c.get('sheet','Sheet1'))[:31]
  for row in c.get('rows',[]): s.append(row)
  d.save(path)
 elif kind=='.pptx':
  from pptx import Presentation
  d=Presentation()
  for page in c.get('slides',[]):
   s=d.slides.add_slide(d.slide_layouts[1]);s.shapes.title.text=str(page.get('title',''));s.placeholders[1].text=str(page.get('body',''))
  d.save(path)
 elif kind=='.pdf':
  from reportlab.pdfgen import canvas
  from reportlab.pdfbase import pdfmetrics
  from reportlab.pdfbase.cidfonts import UnicodeCIDFont
  d=canvas.Canvas(str(path)); text=d.beginText(50,790)
  pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'));text.setFont('STSong-Light',12)
  for line in c.get('paragraphs',[]): text.textLine(str(line))
  d.drawText(text);d.save()
 print(json.dumps({'path':str(path),'bytes':path.stat().st_size}))
else:
 if kind=='.docx':
  from docx import Document
  result={'paragraphs':[p.text for p in Document(path).paragraphs]}
 elif kind=='.xlsx':
  from openpyxl import load_workbook
  d=load_workbook(path,read_only=True,data_only=False);result={'sheets':{s.title:list(s.iter_rows(values_only=True))[:1000] for s in d}};d.close()
 elif kind=='.pptx':
  from pptx import Presentation
  result={'slides':[[s.text for s in p.shapes if s.has_text_frame] for p in Presentation(path).slides]}
 else:
  from pypdf import PdfReader
  d=PdfReader(path);result={'pages':len(d.pages),'text':[p.extract_text() for p in d.pages[:100]]}
 print(json.dumps(result,ensure_ascii=False,default=str))
'''.replace("PAYLOAD", repr(payload), 1)
    cwd = target.parent
    while not cwd.exists():
        cwd = cwd.parent
    require_components("python")
    return _execute("python", script, str(cwd))


def render_office(path: str, output_directory: str) -> dict:
    """Convert DOCX/XLSX/PPTX to PDF using application-owned LibreOffice."""
    source = Path(path).expanduser().resolve(strict=True)
    if source.suffix.lower() not in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        raise ValueError("Unsupported office format")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / (source.stem + ".pdf")
    if target.exists():
        raise ValueError("PDF target exists; select a fresh output directory")
    require_components("office")
    office = bundle_root() / "office/program/soffice.exe"
    with tempfile.TemporaryDirectory(prefix="bread-office-") as profile:
        result = subprocess.run([str(office), "-env:UserInstallation=" + Path(profile).as_uri(),
                                 "--headless", "--convert-to", "pdf", "--outdir", str(output), str(source)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"exitCode": result.returncode, "path": str(target), "rendered": target.is_file(),
            "stdout": result.stdout[:16000].decode("utf-8", errors="replace"),
            "stderr": result.stderr[:16000].decode("utf-8", errors="replace")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-code", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verify-execution", action="store_true")
    args = parser.parse_args()
    if args.verify_execution:
        import asyncio
        from .connection_readiness import check_portable_tools
        from .portable_desktop import browser, system_browser
        browser_checked = False
        result = asyncio.run(check_portable_tools())
        if result.status != "ok":
            raise RuntimeError(result.detail)
        with tempfile.TemporaryDirectory(prefix="bread-portable-smoke-") as work:
            source = str(Path(work) / "check.docx")
            if document("create", source, {"title": "Bread 测试"})["exitCode"] != 0:
                raise RuntimeError("Portable DOCX failed")
            if public_status()["features"].get("office", True):
                if not render_office(source, str(Path(work) / "render"))["rendered"]:
                    raise RuntimeError("Portable office conversion failed")
            result = execute("javascript", "console.log(Object.keys(breadRequire('@oai/artifact-tool')).length)", work)
            if result["exitCode"] != 0:
                raise RuntimeError("Portable JavaScript/artifact-tool failed: " + result["stderr"])
            async def browser_check():
                try:
                    page = await browser("open", url="data:text/html,<title>Bread</title><h1>portable</h1>")
                    if page["title"] != "Bread" or "portable" not in page["snapshot"]:
                        raise RuntimeError("Portable Chromium failed")
                    await browser("screenshot")
                finally:
                    await browser("close")
            if public_status()["features"].get("browser", True) or system_browser():
                asyncio.run(browser_check()); browser_checked = True
        checks = ["document-mcp-roundtrip", "node-artifact-tool"]
        if browser_checked: checks.append("browser-snapshot-and-screenshot")
        if public_status()["features"].get("office", True):
            checks.append("office-pdf")
        print(json.dumps({"ok": True, "checks": checks}))
        return
    if args.self_test:
        print(json.dumps(public_status()))
        return
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("Bread portable tools")
    server.tool(name="bread_portable_capabilities")(public_status)
    server.tool(name="bread_document")(document)
    if public_status()["features"].get("office", True):
        server.tool(name="bread_render_office")(render_office)
    if args.allow_code:
        server.tool(name="bread_execute_code")(execute)
        from .portable_desktop import browser, windows
        server.tool(name="bread_browser")(browser)
        server.tool(name="bread_windows")(windows)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
