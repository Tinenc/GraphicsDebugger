using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using Bread.Editor;
using Bread;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

[InitializeOnLoad]
public static class BreadEffectEditorSmoke
{
    private static double next;
    private static string Root => Path.GetFullPath(Path.Combine(Application.dataPath,".."));
    private static string StatePath => Path.Combine(Root,"effect-state.json");
    static BreadEffectEditorSmoke(){EditorApplication.update+=Tick;}
    public static void Run(){if(!File.Exists(StatePath)) Save(new JObject{["phase"]="import"});}
    private static void Save(JObject state){File.WriteAllText(StatePath,state.ToString());}
    private static string Hash(string path){using(var hash=SHA256.Create())return BitConverter.ToString(hash.ComputeHash(File.ReadAllBytes(path))).Replace("-","").ToLowerInvariant();}
    private static JObject Obj(object value)=>value as JObject??JObject.FromObject(value);
    private static void Require(bool value,string message){if(!value)throw new Exception(message);}
    private static JObject Render(string request,string route)
    {
        var rendered=Obj(BreadRenderCapture.HandleCommand(new JObject{["request_path"]=request,["width"]=64,["height"]=64}));
        File.WriteAllText(Path.Combine(Root,"render-"+route+".json"),rendered.ToString());
        Require(rendered.Value<bool>("success"),rendered.ToString());
        var data=rendered["data"];
        Require((string)data["executionKind"]=="effect" && (bool)data["geometryApplicable"]==false,"Dispatch fabricated mesh semantics.");
        Require((bool)data["checks"]["shaderCompile"] && (bool)data["checks"]["runtimeBindingsApplied"],data.ToString());
        Require((string)data["effectExecution"]["renderExecutionId"]==(string)data["renderExecutionId"],"Effect execution identity mismatch.");
        Require((int)data["effectExecution"]["executedIterations"]==4 && ((JArray)data["passExecution"]).Count==1,"History warmup/pass evidence mismatch.");
        Require((string)data["runtimeBindings"]["readback"][0]["scope"]=="compute-upload" && (bool)data["runtimeBindings"]["readback"][0]["uploadVerified"],"Actual upload missing.");
        var image=new Texture2D(2,2,TextureFormat.RGBA32,false,true);ImageConversion.LoadImage(image,File.ReadAllBytes((string)data["image"]["path"]));
        var pixel=image.GetPixel(32,32);UnityEngine.Object.DestroyImmediate(image);
        Require(Mathf.Abs(pixel.r-0.1875f)<0.01f && Mathf.Abs(pixel.g-0.375f)<0.01f && Mathf.Abs(pixel.b-0.5625f)<0.01f,"Temporal algorithm/history output incorrect: "+pixel);
        if(route=="copy") Require(((JArray)data["files"]).Any(file=>(string)file["assetPath"]=="Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs"),"Copy implementation omitted from actual asset dependency mapping.");
        else if(route!="compute") Require(((JArray)data["files"]).Any(file=>((string)file["path"]).Replace('\\','/').Contains("/Assets/BreadRecovery/")),"Recovery implementation omitted from dependency hashes.");
        return (JObject)data;
    }
    private static void Tick()
    {
        if(!File.Exists(StatePath)||EditorApplication.isCompiling||EditorApplication.isUpdating||EditorApplication.timeSinceStartup<next)return;
        next=EditorApplication.timeSinceStartup+1;
        var state=JObject.Parse(File.ReadAllText(StatePath)); if((string)state["phase"]=="done" || (string)state["phase"]=="failed")return;
        try {
            var config=JObject.Parse(File.ReadAllText(Path.Combine(Root,"effect-config.json"))); var request=(string)config["request"];
            var args=new JObject{["request_path"]=request,["request_file_sha256"]=Hash(request)};
            if((string)state["phase"]=="import") {
                var result=Obj(BreadCaptureImporter.HandleCommand(args)); Require((string)result["status"]!="failed",result.ToString());
                File.WriteAllText(Path.Combine(Root,"import-start.json"),result.ToString()); state["phase"]="poll";Save(state);return;
            }
            if((string)state["phase"]=="poll") {
                args["action"]="status";var result=Obj(BreadCaptureImporter.HandleCommand(args));var status=(string)result["status"];
                if(status=="pending")return; if(status=="interrupted"){state["phase"]="import";Save(state);return;}
                Require(status=="imported"||status=="partial",result.ToString());File.WriteAllText(Path.Combine(Root,"import.json"),result.ToString());
                state["phase"]="validate";Save(state);EditorUtility.RequestScriptReload();return;
            }
            Render(request,"compute");
            var counterObject=new GameObject("Synthetic counter verification");
            try {
                var binder=counterObject.AddComponent<BreadCaptureBindings>();binder.parameterOnly=true;
                binder.bindings=new[]{new BreadParameterBinding{name="Items",type="buffer",stride=4,bufferType="append",bufferBase64=Convert.ToBase64String(new byte[32]),initialCounter=2,counterProvenance="inferred synthetic seed"},
                    new BreadParameterBinding{name="Counted",type="buffer",stride=4,bufferType="counter",bufferBase64=Convert.ToBase64String(new byte[32]),initialCounter=3,counterProvenance="inferred synthetic seed"}};
                binder.ResetInputs();Require(binder.lastError==null,binder.lastError);
                Require(binder.ReadCounter("Items")==2 && binder.ReadCounter("Counted")==3,"GPU CopyCount did not confirm initial append/counter state.");
                File.WriteAllText(Path.Combine(Root,"counter-readback.json"),new JObject{["append"]=binder.ReadCounter("Items"),["counter"]=binder.ReadCounter("Counted"),["readback"]="ComputeBuffer.CopyCount/GetData"}.ToString());
            } finally {UnityEngine.Object.DestroyImmediate(counterObject);}
            foreach(var route in new[]{"external-compute","fullscreen","cpu"}) {
                var effect=(JObject)config["effect"].DeepClone();var pass=(JObject)effect["passes"][0];
                var path="Assets/BreadRecovery/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"+(route=="external-compute"?"Temporal.compute":route=="fullscreen"?"Temporal.shader":"BreadCpuTemporal.cs");
                pass["asset"]=path;pass["kind"]=route=="external-compute"?"compute":route=="fullscreen"?"fullscreen":"custom";
                pass["entryPoint"]=route=="external-compute"?"CSMain":route=="fullscreen"?"frag":"BreadCpuTemporal.Execute";
                if(route=="cpu")pass["componentType"]="BreadCpuTemporal";
                var plan=(JObject)config["renderPlan"].DeepClone();plan["implementationAssets"]=new JArray(path);
                plan["passes"][0]["implementation"]=new JObject{["kind"]=route=="fullscreen"?"draw":(string)pass["kind"],["asset"]=path,["entryPoint"]=pass["entryPoint"]};
                var applied=Obj(BreadApplyCaptureConfiguration.HandleCommand(new JObject{["request_path"]=request,["configuration"]=new JObject{["effect"]=effect,["renderPlan"]=plan}}));
                Require(applied.Value<bool>("success"),applied.ToString());
                var data=Render(request,route);Require((string)data["passExecution"][0]["implementationAsset"]==path,"Recorded implementation asset differs from actual recovery asset.");
            }
            var invalid=(JObject)config["effect"].DeepClone();invalid["passes"][0]["asset"]="../../Escape.compute";
            var rejected=Obj(BreadApplyCaptureConfiguration.HandleCommand(new JObject{["request_path"]=request,["configuration"]=new JObject{["effect"]=invalid}}));
            Require(!rejected.Value<bool>("success"),"Escaping asset path accepted.");
            var early=(JObject)config["effect"].DeepClone();early["passes"][0]["insertionPoint"]="BeforeRendering";
            var earlyRejected=Obj(BreadApplyCaptureConfiguration.HandleCommand(new JObject{["request_path"]=request,["configuration"]=new JObject{["effect"]=early}}));
            Require(!earlyRejected.Value<bool>("success") && ((string)earlyRejected["data"]["detail"]).Contains("RendererFeature or CustomPass"),"Unexecuted BeforeRendering insertion point was accepted.");
            File.WriteAllText(Path.Combine(Root,"before-rendering-rejected.json"),earlyRejected.ToString());
            var copy=(JObject)config["effect"].DeepClone();copy["resources"][0]["initialization"]["value"]=new JArray(0.1875,0.375,0.5625,1);
            var copyPass=(JObject)copy["passes"][0];copyPass["kind"]="copy";copyPass["source"]="history";copyPass.Remove("asset");copyPass["entryPoint"]="BreadCaptureEffect.Execute";
            var copyPlan=(JObject)config["renderPlan"].DeepClone();var copyAsset="Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs";
            copyPlan["implementationAssets"]=new JArray(copyAsset);copyPlan["passes"][0]["implementation"]=new JObject{["kind"]="custom",["asset"]=copyAsset,["entryPoint"]="BreadCaptureEffect.Execute"};
            var copyApplied=Obj(BreadApplyCaptureConfiguration.HandleCommand(new JObject{["request_path"]=request,["configuration"]=new JObject{["effect"]=copy,["renderPlan"]=copyPlan}}));
            Require(copyApplied.Value<bool>("success"),copyApplied.ToString());
            var copied=Render(request,"copy");Require((string)copied["passExecution"][0]["implementationAsset"]==copyAsset && (string)copied["passExecution"][0]["insertionPoint"]=="AfterRendering","Copy execution does not identify the actual runtime implementation and insertion point.");
            var refresh=Obj(BreadRefreshAssets.HandleCommand(new JObject{["check_only"]=true}));Require((string)refresh["data"]["status"]=="ready",refresh.ToString());
            File.WriteAllText(Path.Combine(Root,"refresh.json"),refresh.ToString());state["phase"]="done";Save(state);EditorApplication.Exit(0);
        } catch(Exception error){state["phase"]="failed";state["error"]=error.ToString();Save(state);Debug.LogException(error);EditorApplication.Exit(1);}
    }
}
