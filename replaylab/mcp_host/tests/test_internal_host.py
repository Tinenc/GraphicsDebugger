import asyncio
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from src import internal_host
from src import replaylab_host


def _request(operation="list-projects", parameters=None, request_id="a" * 32):
    return json.dumps(
        {
            "schema": internal_host.PROJECT_OPERATION_SCHEMA,
            "requestId": request_id,
            "operation": operation,
            "parameters": {} if parameters is None else parameters,
        },
        separators=(",", ":"),
    ).encode("utf-8")


class _ProjectService:
    def __init__(self):
        self.calls = []

    def list_projects(self):
        self.calls.append("list")
        return {"schema": "replaylab-project-list/v1", "projects": []}


class ReplayLabInternalHostTests(unittest.TestCase):
    def test_detached_frozen_service_owns_extraction_environment(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), mock.patch.object(
                    internal_host.sys, "frozen", frozen, create=True), mock.patch.dict(
                    os.environ, {"PYINSTALLER_RESET_ENVIRONMENT": "0",
                                 "_PYI_PARENT_PROCESS_LEVEL": "1",
                                 "REPLAYLAB_INTERNAL_RUNTIME_DIR": "D:/runtime-test"}), mock.patch.object(
                    internal_host.subprocess, "Popen") as launch:
                original = dict(os.environ)
                command = [str(Path(sys.executable).resolve()), "serve"]
                internal_host._spawn_detached(command)
                args, options = launch.call_args
                self.assertEqual((command,), args)
                expected = dict(original)
                if frozen:
                    expected["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                self.assertEqual(expected, options["env"])
                self.assertEqual(original, dict(os.environ))
                self.assertIsNot(options["env"], os.environ)
                self.assertFalse(options["shell"])
                self.assertTrue(options["close_fds"])
                self.assertEqual(str(Path(sys.executable).resolve().parent), options["cwd"])
                for stream in ("stdin", "stdout", "stderr"):
                    self.assertEqual(subprocess.DEVNULL, options[stream])
                if os.name == "nt":
                    # DETACHED_PROCESS overrides CREATE_NO_WINDOW and can let
                    # the frozen bootloader's child allocate a visible console.
                    self.assertEqual(subprocess.CREATE_NEW_PROCESS_GROUP
                                     | subprocess.CREATE_NO_WINDOW, options["creationflags"])

    def test_loopback_health_explicitly_bypasses_ambient_proxies(self):
        self.assertEqual({}, internal_host._LOOPBACK_PROXY_HANDLER.proxies)

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "service": internal_host.SERVICE_NAME,
        }).encode("utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://proxy.invalid:8080",
                "HTTPS_PROXY": "http://proxy.invalid:8080",
            },
        ), mock.patch.object(
            internal_host._LOOPBACK_OPENER, "open", return_value=response
        ) as opened:
            value = internal_host.health(49743)

        self.assertEqual(internal_host.SERVICE_NAME, value["service"])
        request = opened.call_args.args[0]
        self.assertEqual("http://127.0.0.1:49743/health", request.full_url)

    def test_internal_lifecycle_is_disjoint_from_external_mcp_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal = internal_host._paths(root)
            external = replaylab_host._paths(root)
            self.assertNotEqual(internal["state"], external["state"])
            self.assertNotEqual(internal["token"], external["token"])
            self.assertNotEqual(
                internal_host.DEFAULT_PORT, replaylab_host.DEFAULT_PORT)
            self.assertEqual("internal-host.json", internal["state"].name)
            self.assertEqual("internal-host.token", internal["token"].name)
        self.assertEqual("ReplayLabInternal", internal_host.SERVICE_NAME)

    def test_generation_token_rotates_and_is_private_wire_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = internal_host._write_generation_token(root)
            second = internal_host._write_generation_token(root)
            self.assertNotEqual(first, second)
            self.assertRegex(second, internal_host.TOKEN_PATTERN)
            self.assertEqual(
                second,
                internal_host._paths(root)["token"].read_text(
                    encoding="ascii").strip(),
            )

    def test_project_request_is_exact_bounded_and_duplicate_safe(self):
        value = internal_host.parse_project_operation_request(_request())
        self.assertEqual("list-projects", value["operation"])
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            internal_host.parse_project_operation_request(
                b'{"schema":"a","schema":"b"}')
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            internal_host.parse_project_operation_request(_request()[:-1] + b',"x":1}')
        with self.assertRaises(OverflowError):
            internal_host.parse_project_operation_request(
                b"x" * (internal_host.MAX_PROJECT_REQUEST_BYTES + 1))

    def test_project_request_carries_typed_paths_not_cli_arguments(self):
        parameters = {
            "capturePath": os.path.abspath("capture.rdc"),
            "associationLease": "",
            "associationToken": "",
        }
        value = internal_host.parse_project_operation_request(
            _request("match-capture", parameters))
        self.assertEqual(parameters["capturePath"], value["parameters"]["capturePath"])
        self.assertNotIn("arguments", value)
        with self.assertRaisesRegex(ValueError, "parameters are invalid"):
            internal_host.parse_project_operation_request(
                _request("match-capture", {**parameters, "command": "stdio"}))

    def test_operation_registry_deduplicates_request_id_and_rejects_reuse(self):
        service = _ProjectService()
        registry = internal_host.ProjectOperationRegistry(service)
        request = internal_host.parse_project_operation_request(_request())
        first_status, first = registry.execute(request)
        second_status, second = registry.execute(request)
        self.assertEqual(200, first_status)
        self.assertEqual((first_status, first), (second_status, second))
        self.assertEqual(["list"], service.calls)

        conflict = dict(request)
        conflict["operation"] = "activate-project"
        conflict["parameters"] = {"projectRoot": os.path.abspath("project")}
        status, value = registry.execute(conflict)
        self.assertEqual(409, status)
        self.assertEqual("request-id-conflict", value["error"]["code"])

    def test_bearer_gate_protects_every_non_health_route(self):
        calls = []

        async def application(scope, receive, send):
            del receive
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def invoke(path, authorization=b""):
            sent = []
            headers = []
            if authorization:
                headers.append((b"authorization", authorization))
            scope = {"type": "http", "path": path, "headers": headers}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(value):
                sent.append(value)

            await internal_host.BearerAuthApp(application, "T" * 48)(
                scope, receive, send)
            return sent

        denied = asyncio.run(invoke("/replaylab/internal/project-operation"))
        self.assertEqual(401, denied[0]["status"])
        allowed = asyncio.run(invoke(
            "/replaylab/internal/project-operation", b"Bearer " + b"T" * 48))
        self.assertEqual(204, allowed[0]["status"])
        health = asyncio.run(invoke("/health"))
        self.assertEqual(204, health[0]["status"])
        self.assertEqual(
            ["/replaylab/internal/project-operation", "/health"], calls)

    def test_internal_app_does_not_mount_general_mcp(self):
        source = Path(internal_host.__file__).read_text(encoding="utf-8")
        self.assertNotIn("streamable_http_app()", source)
        self.assertNotIn('Route("/mcp"', source)
        self.assertNotIn("from .server import mcp", source)
        self.assertNotIn("from .replaylab_host import", source)
        self.assertNotIn("from .server import _shader_live_edit_manager", source)
        self.assertIn("This is intentionally a plain Router", source)

    def test_restore_delete_route_is_inside_the_bearer_protected_router(self):
        source = Path(internal_host.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        delete_routes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Name) or function.id != "Route":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            methods = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "methods"),
                None,
            )
            if (
                node.args[0].value == "/replaylab/project-restore/{job_id}"
                and isinstance(methods, ast.List)
                and [item.value for item in methods.elts] == ["DELETE"]
            ):
                delete_routes.append(node)
        self.assertEqual(1, len(delete_routes))
        self.assertIn("protected = BearerAuthApp(app, token)", source)

    def test_import_is_transport_and_external_host_free(self):
        package_root = Path(internal_host.__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(package_root), environment.get("PYTHONPATH", "")]
        )
        script = (
            "import json, sys; import src.internal_host; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name == 'src.replaylab_host' or name == 'src.server' "
            "or name == 'mcp' or name.startswith('mcp.'))))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(package_root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        self.assertEqual([], json.loads(completed.stdout))

    def test_external_mcp_application_is_built_only_on_transport_request(self):
        from src import server

        created = []

        class _FastMCP:
            def __init__(self, *args, **kwargs):
                self._mcp_server = types.SimpleNamespace(version="sdk")
                self.tools = []
                created.append((args, kwargs, self))

            def tool(self, *args, **kwargs):
                def register(function):
                    self.tools.append((args, kwargs, function))
                    return function

                return register

            def streamable_http_app(self):
                return "external-http"

            def run(self, **kwargs):
                return kwargs

        mcp_package = types.ModuleType("mcp")
        mcp_package.__path__ = []
        mcp_server = types.ModuleType("mcp.server")
        mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = _FastMCP
        proxy = server._DeferredFastMCP()

        @proxy.tool()
        def sample_tool():
            return "ok"

        self.assertEqual([], created)
        with mock.patch.dict(
            sys.modules,
            {
                "mcp": mcp_package,
                "mcp.server": mcp_server,
                "mcp.server.fastmcp": fastmcp,
            },
        ):
            self.assertEqual("external-http", proxy.streamable_http_app())
        self.assertEqual(1, len(created))
        self.assertEqual(1, len(created[0][2].tools))
        self.assertEqual(server.SERVICE_VERSION, created[0][2]._mcp_server.version)

    def test_internal_routes_are_exactly_the_bounded_high_level_surface(self):
        source = Path(internal_host.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=internal_host.__file__)
        serve = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_serve"
        )
        routes = {
            node.value for node in ast.walk(serve)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/")
        }
        self.assertEqual(
            {
                "/health",
                "/replaylab/internal/project-operation",
                "/replaylab/project-restore",
                "/replaylab/project-restore/{job_id}",
                "/replaylab/shader-source/save",
                "/replaylab/shader-source/check",
                "/replaylab/shader-live-edit",
                "/replaylab/asset-index",
            },
            routes,
        )


if __name__ == "__main__":
    unittest.main()


class ProjectOperationRegistryBusyTests(unittest.TestCase):
    """A list must open the project window while a mutation is still running."""

    def test_list_runs_while_a_mutation_holds_the_lock_and_mutations_say_busy(self):
        import threading
        import time

        started = threading.Event()
        release = threading.Event()

        class Service:
            def list_projects(self):
                return {"schema": "replaylab-project-list/v1", "projects": [], "rejected": []}

            def activate(self, project_root):
                started.set()
                release.wait(5.0)
                return {"schema": "x"}

        registry = internal_host.ProjectOperationRegistry(Service())
        registry.LOCK_WAIT_SECONDS = 0.2
        activate = internal_host.parse_project_operation_request(
            _request("activate-project", {"projectRoot": os.path.abspath("project")},
                     request_id="b" * 32))
        worker = threading.Thread(target=registry.execute, args=(activate,))
        worker.start()
        self.assertTrue(started.wait(2.0))
        try:
            began = time.monotonic()
            status, value = registry.execute(
                internal_host.parse_project_operation_request(_request()))
            self.assertEqual(200, status)
            self.assertLess(time.monotonic() - began, 2.0)
            second = internal_host.parse_project_operation_request(
                _request("activate-project", {"projectRoot": os.path.abspath("other")},
                         request_id="c" * 32))
            status, value = registry.execute(second)
            self.assertEqual(503, status)
            self.assertEqual("project-service-busy", value["error"]["code"])
            self.assertIn("activate-project", value["error"]["message"])
        finally:
            release.set()
            worker.join(5.0)
