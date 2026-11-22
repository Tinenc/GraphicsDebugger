import ast
import json
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]


class IntegrationManifestTests(unittest.TestCase):
    def test_integration_and_ipc_protocol_versions_are_declared(self) -> None:
        integration = json.loads(
            (REPOSITORY / "renderdoc_mcp_bridge" / "integration.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(integration["schema"], "renderdoc-builtin-integration/v1")
        self.assertEqual(integration["module"], "renderdoc_mcp_bridge")
        self.assertEqual(integration["entrypoint"], "start")
        self.assertEqual(integration["shutdown"], "stop")
        self.assertRegex(integration["version"], r"^1\.\d+\.\d+$")
        self.assertEqual(integration["minimum_renderdoc"], "1.44")
        self.assertEqual(
            integration["host_contract"],
            {
                "schema": "replaylab-internal-host-capabilities/v1",
                "protocol_version": 1,
                "capability_versions": {
                    "project-service": 1,
                    "project-restore": 1,
                    "shader-workspace-prepare": 1,
                    "shader-source-save": 1,
                    "shader-live-edit": 1,
                    "asset-index": 1,
                },
            },
        )

        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance((target := node.targets[0]), ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        self.assertEqual(constants["BRIDGE_PROTOCOL_VERSION"], 2)
        self.assertEqual(constants["BRIDGE_INTEGRATION_VERSION"], integration["version"])

        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue(
            {
                "start",
                "stop",
                "available_native_shader_stages",
                "current_native_context_json",
                "begin_native_context_freeze_json",
                "poll_native_context_freeze_json",
                "abandon_native_context_freeze_json",
            }.issubset(functions)
        )
        self.assertNotIn("register", functions)
        self.assertNotIn("unregister", functions)
        self.assertNotIn("_register_renderdoc_ai_ui", functions)
        bridge_source = bridge_path.read_text(encoding="utf-8")
        self.assertIn("ReplayLabRestoreHostIncompatible", bridge_source)
        self.assertIn('"host-runtime-incompatible"', bridge_source)

    def test_replaylab_application_version_matches_integration(self) -> None:
        integration = json.loads(
            (REPOSITORY / "renderdoc_mcp_bridge" / "integration.json").read_text(
                encoding="utf-8"
            )
        )
        version_path = REPOSITORY / "mcp_host" / "src" / "version.py"
        tree = ast.parse(version_path.read_text(encoding="utf-8"))
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance((target := node.targets[0]), ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        # GUI clients keep the bundled integration version as their minimum
        # Host package. Newer same-major Hosts remain compatible when their
        # protocol and individual capability schemas still match.
        service_version = tuple(map(int, constants["SERVICE_VERSION"].split(".")))
        minimum_version = tuple(map(int, integration["version"].split(".")))
        self.assertEqual(service_version[0], minimum_version[0])
        self.assertGreaterEqual(service_version, minimum_version)
        self.assertEqual(constants["HOST_STATE_PROTOCOL_VERSION"], 2)
        capability_assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "INTERNAL_HOST_CAPABILITY_VERSIONS"
        )
        self.assertEqual(
            ast.literal_eval(capability_assignment.value),
            integration["host_contract"]["capability_versions"],
        )
        self.assertEqual(
            constants["INTERNAL_HOST_CAPABILITY_SCHEMA"],
            integration["host_contract"]["schema"],
        )

        agent_path = REPOSITORY / "src" / "replaylab_agent" / "__init__.py"
        agent_tree = ast.parse(agent_path.read_text(encoding="utf-8"))
        agent_constants = {
            target.id: node.value.value
            for node in agent_tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance((target := node.targets[0]), ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        self.assertEqual(
            agent_constants["__version__"], constants["SERVICE_VERSION"]
        )

        for client_name, expected_protocol in (
            ("project_restore_client.py", 1),
            ("shader_editor_client.py", 1),
        ):
            client_tree = ast.parse(
                (REPOSITORY / "renderdoc_mcp_bridge" / client_name).read_text(
                    encoding="utf-8"
                )
            )
            client_constants = {
                target.id: node.value.value
                for node in client_tree.body
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance((target := node.targets[0]), ast.Name)
                and isinstance(node.value, ast.Constant)
            }
            self.assertEqual(client_constants["EXPECTED_HOST_VERSION"], integration["version"])
            self.assertEqual(
                client_constants["INTERNAL_HOST_STATE_PROTOCOL_VERSION"],
                expected_protocol,
            )

        server_source = (
            REPOSITORY / "mcp_host" / "src" / "server.py"
        ).read_text(encoding="utf-8")
        host_source = (
            REPOSITORY / "mcp_host" / "src" / "replaylab_host.py"
        ).read_text(encoding="utf-8")
        self.assertIn("from .version import SERVICE_VERSION", server_source)
        self.assertIn("class _DeferredFastMCP", server_source)
        self.assertIn(
            "application._mcp_server.version = SERVICE_VERSION", server_source
        )
        self.assertNotIn(
            "from mcp.server.fastmcp import FastMCP\n",
            server_source.split("class _DeferredFastMCP", 1)[0],
        )
        self.assertIn(
            "from .version import HOST_STATE_PROTOCOL_VERSION, SERVICE_VERSION",
            host_source,
        )
        version_source = (
            REPOSITORY / "mcp_host" / "src" / "version.py"
        ).read_text(encoding="utf-8")
        self.assertIn("HOST_STATE_PROTOCOL_VERSION = 2", version_source)
        self.assertIn(
            "INTERNAL_HOST_STATE_PROTOCOL_VERSION = 1", version_source)
        internal_host_source = (
            REPOSITORY / "mcp_host" / "src" / "internal_host.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"capabilitySchema": INTERNAL_HOST_CAPABILITY_SCHEMA',
                      internal_host_source)
        self.assertIn('"capabilityVersions": dict(INTERNAL_HOST_CAPABILITY_VERSIONS)',
                      internal_host_source)

    def test_mcp_runtime_is_bounded_to_the_compatible_major_version(self) -> None:
        pyproject = (REPOSITORY / "mcp_host" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('"mcp[cli]>=1.0.0,<2.0.0"', pyproject)


if __name__ == "__main__":
    unittest.main()
