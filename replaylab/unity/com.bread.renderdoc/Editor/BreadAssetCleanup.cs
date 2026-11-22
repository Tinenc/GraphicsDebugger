using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;

namespace Bread.Editor
{
    [McpForUnityTool("bread_cleanup_empty_asset_folders", Description="Plan/apply removal of empty generated folders only, inside an explicit restoration asset_root and managed_folders from the actual delivery manifest. Preserves nonempty, referenced, labeled and asset-bundle folders; never deletes asset files or follows junctions. Use bread_apply_asset_layout first for GUID-preserving moves; recheck actual emptiness before each deletion. dry_run defaults true.")]
    public static class BreadCleanupEmptyAssetFolders
    {
        static string AssetPath(string path)
        {
            path=(path??"").Replace('\\','/').TrimEnd('/');
            if(!path.StartsWith("Assets/",StringComparison.Ordinal)||path.Split('/').Any(part=>String.IsNullOrEmpty(part)||part=="."||part==".."))
                throw new ArgumentException("Use a project-local restoration subtree below Assets, not Assets itself.");
            return path;
        }
        static bool RegularDirectory(string path)
        {
            var current=new DirectoryInfo(Path.GetFullPath(path));
            while(current!=null)
            {if(current.Exists && (current.Attributes&FileAttributes.ReparsePoint)!=0)return false;current=current.Parent;}
            return Directory.Exists(path);
        }
        public static object HandleCommand(JObject p)
        {
            try
            {
                string root=AssetPath((string)p["asset_root"]);
                var owned=(p["managed_folders"] as JArray)?.Values<string>().Select(AssetPath).Distinct().ToArray();
                if(owned==null||owned.Length==0||owned.Length>256)throw new ArgumentException("Supply 1..256 actually generated folders from this delivery's manifest.");
                if(owned.Any(path=>!path.StartsWith(root+"/",StringComparison.Ordinal)))throw new ArgumentException("Every managed folder must be inside the selected asset_root; the root itself is retained.");
                var ownedSet=new HashSet<string>(owned);
                bool dry=p.Value<bool?>("dry_run")??true;
                var skipped=new JArray();var planned=new List<string>();var seen=new HashSet<string>();
                var owners=AssetDatabase.GetAllAssetPaths().Where(path=>path.StartsWith("Assets/",StringComparison.Ordinal)&&!AssetDatabase.IsValidFolder(path)).ToArray();
                if(owners.Length>100000)return new {success=true,data=new {dryRun=dry,deletedEmptyFolders=Array.Empty<string>(),
                    diagnostic="Project reference inventory exceeds the bounded cleanup query; no folders removed. Asset import and rendering remain available."}};
                var referenced=new HashSet<string>(AssetDatabase.GetDependencies(owners,false));
                Func<string,bool> visit=null;
                visit=path=>
                {
                    if(!ownedSet.Contains(path)){skipped.Add(new JObject{["path"]=path,["reason"]="unowned-folder"});return false;}
                    if(seen.Contains(path))return planned.Contains(path);
                    if(seen.Count>=2048)throw new ArgumentException("Cleanup inspection exceeds 2048 folders; use a smaller explicit managed scope.");
                    seen.Add(path);
                    if(!RegularDirectory(path)){skipped.Add(new JObject{["path"]=path,["reason"]="missing-or-redirected"});return false;}
                    var asset=AssetDatabase.LoadMainAssetAtPath(path);var importer=AssetImporter.GetAtPath(path);
                    if(referenced.Contains(path)||(asset && AssetDatabase.GetLabels(asset).Length>0)||!String.IsNullOrEmpty(importer?.assetBundleName))
                    {skipped.Add(new JObject{["path"]=path,["reason"]="referenced-or-annotated-folder"});return false;}
                    var entries=Directory.GetFileSystemEntries(path);bool empty=true;
                    foreach(string entry in entries.Where(Directory.Exists))if(!visit(entry.Replace('\\','/')))empty=false;
                    foreach(string entry in entries.Where(value=>!Directory.Exists(value)))
                    {
                        string normalized=entry.Replace('\\','/');
                        // A child folder's meta goes away with that planned
                        // folder; unrelated/orphan metadata is preserved.
                        if(!normalized.EndsWith(".meta",StringComparison.Ordinal)||!planned.Contains(normalized.Substring(0,normalized.Length-5)))empty=false;
                    }
                    if(empty)planned.Add(path);
                    return empty;
                };
                foreach(string path in owned)visit(path);
                var deleted=new JArray();var retained=new JArray();
                if(!dry)foreach(string path in planned)
                {
                    // Main-thread AssetDatabase operation; no recursive shell
                    // deletion. New contents or annotations retain the folder.
                    var asset=AssetDatabase.LoadMainAssetAtPath(path);var importer=AssetImporter.GetAtPath(path);
                    if(!RegularDirectory(path)||Directory.GetFileSystemEntries(path).Length!=0||referenced.Contains(path)||
                       (asset&&AssetDatabase.GetLabels(asset).Length>0)||!String.IsNullOrEmpty(importer?.assetBundleName))
                    {retained.Add(new JObject{["path"]=path,["reason"]="changed-or-nonempty"});continue;}
                    if(AssetDatabase.DeleteAsset(path))deleted.Add(path);
                    else retained.Add(new JObject{["path"]=path,["reason"]="delete-not-performed"});
                }
                return new {success=true,data=new {schema="bread-empty-folder-cleanup/v1",dryRun=dry,assetRoot=root,
                    managedFolders=owned,plannedEmptyFolders=planned,deletedEmptyFolders=deleted,skipped,retained,
                    assetContentsDeleted=false,folderMetadataRemovedWithFolder=true,referenceScope="Current AssetDatabase file dependencies; labeled/bundle folders retained",
                    nextAction="Keep source-to-asset mappings, verify GUIDs and scene/prefab/material references after moves, then compare final output again. Empty-folder cleanup is not visual acceptance."}};
            }
            catch(Exception e){return new {success=false,error=e.ToString()};}
        }
    }
}
