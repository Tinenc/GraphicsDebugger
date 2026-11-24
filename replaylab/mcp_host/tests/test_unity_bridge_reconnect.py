"""Execute the real Editor watchdog class against small Editor/transport doubles."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_editor_watchdog_restarts_stopped_transport_without_restarting_healthy_one(tmp_path):
    dotnet = shutil.which("dotnet")
    if not dotnet:
        pytest.skip(".NET SDK required for the isolated C# watchdog test")
    source = Path(__file__).parents[2] / "unity/com.bread.unity-connection/Editor/BreadUnityConnection.cs"
    watchdog = source.read_text(encoding="utf-8").split('    [McpForUnityTool("bread_connection_identity"', 1)[0] + "}\n"
    (tmp_path / "Watchdog.cs").write_text(watchdog, encoding="utf-8")
    (tmp_path / "Probe.csproj").write_text('''<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>
<OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework><UseAppHost>false</UseAppHost>
<NuGetAudit>false</NuGetAudit></PropertyGroup></Project>''', encoding="utf-8")
    (tmp_path / "Program.cs").write_text(r'''
using System;
using System.IO;
using System.Collections.Generic;
using System.Text.Json;
using Bread.UnityConnection;
using MCPForUnity.Editor.Services.Transport.Transports;

namespace Newtonsoft.Json.Linq {
 public class JObject {
  JsonElement value;
  public static JObject Parse(string data) => new JObject { value = JsonDocument.Parse(data).RootElement.Clone() };
  public T Value<T>(string key) {
   if (!value.TryGetProperty(key, out var item)) return default;
   object result = item.ValueKind == JsonValueKind.String ? (object)item.GetString() : item.GetBoolean();
   return (T)result;
  }
 }
}
namespace UnityEngine {
 public static class Application { public static string dataPath; }
 public static class Debug { public static void LogWarning(object data) { throw new Exception(data.ToString()); } }
}
namespace UnityEngine.Rendering { public class Fixture {} }
namespace UnityEditor.PackageManager { public class Fixture {} }
namespace UnityEditor.PackageManager.Requests { public class Fixture {} }
namespace MCPForUnity.Editor.Tools { public class Fixture {} }
namespace UnityEditor {
 public class InitializeOnLoadAttribute : Attribute {}
 public static class EditorApplication {
  public static double timeSinceStartup = 100;
  public static bool isCompiling, isUpdating;
  public static event Action update;
  public static void Tick() { timeSinceStartup += 3; update?.Invoke(); }
 }
 public static class SessionState {
  static Dictionary<string,string> values = new Dictionary<string,string>();
  public static string GetString(string key, string fallback) => values.TryGetValue(key, out var found) ? found : fallback;
  public static void SetString(string key, string value) => values[key] = value;
 }
 public enum ImportAssetOptions { ForceSynchronousImport }
 public static class AssetDatabase { public static void Refresh(ImportAssetOptions options) {} }
}
namespace MCPForUnity.Editor.Services.Transport.Transports {
 public static class StdioBridgeHost { public static bool IsRunning; }
}
namespace MCPForUnity.Editor {
 public static class McpCiBoot {
  public static int Starts;
  public static void StartStdioForCi() { Starts++; StdioBridgeHost.IsRunning = true; }
 }
}
public static class Program {
 static string config;
 static void Write(bool enabled, string request) => File.WriteAllText(config,
  JsonSerializer.Serialize(new { enabled, nonce = "stable-nonce", packageRevision = "sdk-v1", reconnectRequest = request }));
 static void Check(bool result, string failure) { if (!result) throw new Exception(failure); }
 public static void Main(string[] args) {
  UnityEngine.Application.dataPath = Path.Combine(args[0], "Project", "Assets");
  Directory.CreateDirectory(UnityEngine.Application.dataPath);
  config = Path.Combine(args[0], "Project", "Library", "Bread", "unity-connection.json");
  Directory.CreateDirectory(Path.GetDirectoryName(config));
  Write(true, "r1"); BreadUnityConnection.Configuration();
  UnityEditor.EditorApplication.Tick(); UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 1, "initial connection");
  Check(BreadUnityConnection.ActiveReconnectRequest == "r1", "initial reconnect acknowledgement");
  UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 1, "healthy transport was restarted");
  StdioBridgeHost.IsRunning = false; UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 2, "stopped transport did not restart");
  Check(BreadUnityConnection.ActiveNonce == "stable-nonce", "reconnect changed identity");
  Write(true, "r2"); UnityEditor.EditorApplication.Tick(); UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 3, "reconnect request must run once");
  Check(BreadUnityConnection.ActiveReconnectRequest == "r2", "reconnect request was not acknowledged");
  Write(false, "r2"); StdioBridgeHost.IsRunning = false; UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 3, "disabled connection restarted");
  Write(true, "r2"); UnityEditor.EditorApplication.isCompiling = true; UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 3, "connection restarted during compilation");
  UnityEditor.EditorApplication.isCompiling = false; UnityEditor.EditorApplication.Tick();
  Check(MCPForUnity.Editor.McpCiBoot.Starts == 4, "connection did not resume after compilation");
  Console.WriteLine("watchdog lifecycle passed");
 }
}
''', encoding="utf-8")
    result = subprocess.run([dotnet, "run", "--project", str(tmp_path / "Probe.csproj"),
        "--verbosity", "quiet", "-p:RestoreIgnoreFailedSources=true", "--", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "watchdog lifecycle passed" in result.stdout
