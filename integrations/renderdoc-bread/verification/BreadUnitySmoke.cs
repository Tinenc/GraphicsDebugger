using System;
using System.IO;
using UnityEditor;
using UnityEngine;

public static class BreadUnitySmoke
{
    public static void Run()
    {
        AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
        int verified = 0;
        foreach (string extension in new[] { "obj", "fbx" })
        {
            string path = "Assets/BreadTriangle." + extension;
            var meshObject = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (meshObject == null) throw new Exception("Unity cannot import " + path);
            var filter = meshObject.GetComponentInChildren<MeshFilter>();
            if (filter == null || filter.sharedMesh.vertexCount != 3 || filter.sharedMesh.triangles.Length != 3)
                throw new Exception("Unexpected exported mesh topology: " + path);
            if (filter.sharedMesh.uv.Length != 3 || filter.sharedMesh.normals.Length != 3)
                throw new Exception("Export lost UVs or normals: " + path);
            verified++;
        }
        File.WriteAllText(Path.Combine(Application.dataPath, "../unity-verification.json"),
            "{\"status\":\"passed\",\"formats\":\"OBJ,FBX\",\"meshes\":" + verified +
            ",\"verticesPerMesh\":3,\"indicesPerMesh\":3,\"normals\":true,\"uvs\":true}");
        Debug.Log("BREAD_UNITY_IMPORT_PASS");
    }
}
