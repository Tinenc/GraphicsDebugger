"""Execute the Host's exact saved-workspace recipe; never select another tool.

Embedded Python 3.6. Only authenticated first-party Host responses supply recipes.
Input/output/temp paths are private OS-temporary files and never user sources.
"""
import hashlib
import json
import os
import re
import tempfile
import ctypes
import struct


def _spirv_entry(data, requested, stage):
    if len(data)%4 or not data.startswith(b"\x03\x02\x23\x07"):
        raise RuntimeError("Bound compiler returned invalid SPIR-V")
    words=struct.unpack("<%dI"%(len(data)//4),data)
    models={"vertex":(0,),"hull":(1,),"domain":(2,),"geometry":(3,),"fragment":(4,),"compute":(5,),
            "task":(5267,5364),"mesh":(5268,5365),"raygen":(5313,),"intersection":(5314,),
            "anyhit":(5315,),"closesthit":(5316,),"miss":(5317,),"callable":(5318,)}
    names=[];index=5
    while index<len(words):
        count,opcode=words[index]>>16,words[index]&0xffff
        if not count or index+count>len(words):raise RuntimeError("Invalid SPIR-V instruction")
        if opcode==15 and count>=4 and words[index+1] in models.get(stage,()):
            raw=struct.pack("<%dI"%(count-3),*words[index+3:index+count])
            names.append(raw.split(b"\0",1)[0].decode("utf8"))
        index+=count
    if requested in names:return requested
    if len(names)==1:return names[0]
    raise RuntimeError("Bound compiler entry point is missing or ambiguous")


def _d3d_preprocess(dll, source, filename):
    """Use the same pinned D3DCompiler DLL as the DXBC save adapter."""
    if os.name != "nt":
        raise RuntimeError("The bound D3DCompiler requires a Windows replay host")
    library=ctypes.WinDLL(dll)
    function=library.D3DPreprocess
    function.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_void_p)]
    function.restype=ctypes.c_long
    raw=source.encode("utf8");buffer=ctypes.create_string_buffer(raw)
    output,errors=ctypes.c_void_p(),ctypes.c_void_p()
    def blob(pointer):
        if not pointer.value:return b""
        table=ctypes.cast(pointer,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        address=ctypes.WINFUNCTYPE(ctypes.c_void_p,ctypes.c_void_p)(table[3])(pointer)
        size=ctypes.WINFUNCTYPE(ctypes.c_size_t,ctypes.c_void_p)(table[4])(pointer)
        return ctypes.string_at(address,size)
    try:
        status=function(buffer,len(raw),filename.encode("utf8"),None,None,ctypes.byref(output),ctypes.byref(errors))
        if status<0:raise RuntimeError(blob(errors).decode("utf8",errors="replace"))
        raw=blob(output)
        if raw.endswith(b"\0"):raw=raw[:-1]
        if len(raw)>16*1024*1024 or b"\0" in raw:raise RuntimeError("D3DCompiler preprocessed source is invalid or oversized")
        return raw.decode("utf8")
    finally:
        for pointer in (output,errors):
            if pointer.value:
                table=ctypes.cast(pointer,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                ctypes.WINFUNCTYPE(ctypes.c_ulong,ctypes.c_void_p)(table[2])(pointer)


class BoundCompiler(object):
    def __init__(self, runner):
        self.runner = runner
        self._hashes = {}

    def identity(self, recipe):
        if recipe.get("schema") != "bread-shader-compiler-contract/v1":
            raise RuntimeError("Unsupported workspace compiler contract")
        unsigned = dict(recipe)
        expected = unsigned.pop("identitySha256", "")
        actual = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode("utf8")).hexdigest()
        if actual != expected:
            raise RuntimeError("Workspace compiler contract changed")
        files = []
        runtime_files=set()
        for step in recipe["steps"]:
            family=os.path.basename(step["program"]).lower().split(".")[0]
            if family in ("dxc","fxc","glslang","glslangvalidator"):
                runtime_files.update(self.runner._cached_dependencies(step["program"],"glslang" if family=="glslangvalidator" else family))
            for path, expected_sha in step["files"]:
                stat = os.stat(path)
                key = (path, stat.st_size, getattr(stat,"st_mtime_ns",stat.st_mtime),
                       getattr(stat,"st_ctime_ns",stat.st_ctime),stat.st_ino)
                digest = self._hashes.get(key)
                if digest is None:
                    with open(path,"rb") as handle:
                        hasher=hashlib.sha256()
                        for chunk in iter(lambda:handle.read(1024*1024),b""):hasher.update(chunk)
                        digest=hasher.hexdigest()
                    after=os.stat(path)
                    if (after.st_size,after.st_mtime,after.st_ctime,after.st_ino)!=(stat.st_size,stat.st_mtime,stat.st_ctime,stat.st_ino):
                        raise RuntimeError("The workspace compiler changed while it was read: "+path)
                    if len(self._hashes)>128: self._hashes.clear()
                    self._hashes[key] = digest
                if digest != expected_sha:
                    raise RuntimeError("The bound workspace compiler changed: " + path)
                files.append([path,digest])
        pinned={path for path,_ in files}
        for path in sorted(runtime_files-pinned):
            stat=os.stat(path)
            key=(path,stat.st_size,getattr(stat,"st_mtime_ns",stat.st_mtime),getattr(stat,"st_ctime_ns",stat.st_ctime),stat.st_ino)
            digest=self._hashes.get(key)
            if digest is None:
                hasher=hashlib.sha256()
                with open(path,"rb") as handle:
                    for chunk in iter(lambda:handle.read(1024*1024),b""):hasher.update(chunk)
                digest=hasher.hexdigest();self._hashes[key]=digest
            files.append([path,digest])
        return {"contractSha256": expected, "backend": recipe["backend"], "files": files}

    @staticmethod
    def needs_preprocess(recipe):
        for step in recipe["steps"]:
            args=step["arguments"]
            for index,arg in enumerate(args):
                if re.match(r"^[/-][DU][A-Za-z_]",arg): return True
                if arg in ("-D","/D","-U","/U") and index+1<len(args) and not args[index+1].startswith(("-","/","{")):
                    return True
        return False

    @staticmethod
    def _environment(recipe, directory):
        temporary=os.path.join(directory,"temp")
        os.mkdir(temporary)
        return dict(recipe.get("environment") or {}, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", TEMP=temporary, TMP=temporary)

    def compile(self, recipe, source, entry):
        identity=self.identity(recipe)
        data=source.encode("utf8")
        diagnostics=[]
        with tempfile.TemporaryDirectory(prefix="bread-bound-compile-") as root:
            for index,step in enumerate(recipe["steps"]):
                directory=os.path.join(root,str(index));os.mkdir(directory)
                filename=os.path.join(directory,"input."+step["inputFormat"])
                output=os.path.join(directory,"output."+step["outputFormat"])
                with open(filename,"wb") as handle: handle.write(data)
                values=dict(recipe["values"],input=filename,output=output,job_dir=directory,entry_point=entry)
                argv=[step["program"]]+[arg.format_map(values) for arg in step["arguments"]]
                _,messages=self.runner._run(argv,directory,step["timeoutSeconds"],self._environment(recipe,directory))
                diagnostics.append(messages)
                with open(output,"rb") as handle: data=handle.read(64*1024*1024+1)
                if not 0<len(data)<=64*1024*1024: raise RuntimeError("Bound compiler output is empty or oversized")
                if step["outputFormat"] in ("spirv","opengl-spirv") and index+1<len(recipe["steps"]):
                    entry=_spirv_entry(data,entry,recipe["stage"])
        if self.identity(recipe)!=identity: raise RuntimeError("Workspace compiler changed during compilation")
        kind=recipe["outputFormat"]
        expected={"spirv":b"\x03\x02\x23\x07","opengl-spirv":b"\x03\x02\x23\x07","dxbc":b"DXBC","dxil":b"DXBC"}.get(kind)
        if expected and not data.startswith(expected): raise RuntimeError("Bound compiler returned the wrong target encoding")
        return {"bytes":data,"encoding":{"spirv":"SPIRV","opengl-spirv":"OpenGLSPIRV","dxbc":"DXBC","dxil":"DXIL","glsl":"GLSL"}[kind],
                "diagnostics":"\n".join(diagnostics)}

    def preprocess(self, recipe, source, entry, snapshot, mirror_source):
        self.identity(recipe)
        step=recipe["steps"][0]
        program=step["program"]
        family=os.path.basename(program).lower().split(".")[0]
        d3d_adapter="--dll" in step["arguments"] and "--dll-sha256" in step["arguments"]
        if not d3d_adapter and family not in ("dxc","glslang","glslangvalidator","fxc"):
            raise RuntimeError("The bound workspace compiler has no registered preprocessor: "+step["toolId"])
        with tempfile.TemporaryDirectory(prefix="bread-bound-preprocess-") as directory:
            root_name=snapshot["rootName"] if snapshot else "input."+step["inputFormat"]
            filename=os.path.join(directory,root_name);output=os.path.join(directory,"preprocessed.txt")
            files=dict(snapshot["files"]) if snapshot else {root_name:source}
            if snapshot: files[snapshot["editedName"]]=source
            for name,text in files.items():
                if name.replace("\\", "/").split("/")[0] == "__bread_unavailable_includes__":
                    raise RuntimeError("Shader source uses a reserved include-mirror directory")
                path=os.path.abspath(os.path.join(directory,name))
                if os.path.commonpath([directory,path])!=directory: raise RuntimeError("Include mirror escaped its source unit")
                os.makedirs(os.path.dirname(path),exist_ok=True)
                blocked=set((snapshot or {}).get("blockedIncludes",{}).get(name,[]))
                with open(path,"wb") as handle: handle.write(mirror_source(text,blocked,name).encode("utf8"))
            values=dict(recipe["values"],input=filename,output=output,job_dir=directory,entry_point=entry)
            args=[arg.format_map(values) for arg in step["arguments"]]
            if d3d_adapter:
                with open(filename,"rb") as handle:text=handle.read().decode("utf8")
                result=_d3d_preprocess(args[args.index("--dll")+1],text,filename)
                self.identity(recipe)
                return result
            if family.startswith("glslang"):
                selected=[];index=0;client=""
                while index<len(args):
                    arg=args[index]
                    if arg in ("-V","-G"):
                        client="-DVULKAN=100" if arg=="-V" else "-DGL_SPIRV=100";index+=1;continue
                    if arg in ("-o","--target-env","--client"):
                        index+=2;continue
                    selected.append(arg);index+=1
                args=["-E","-I"+directory]+([client] if client else [])+selected
            elif family=="fxc":
                selected=[];index=0
                while index<len(args):
                    arg=args[index]
                    if arg in ("-D","/D","-U","/U","-I","/I"):
                        selected+=args[index:index+2];index+=2;continue
                    if re.match(r"^[/-][DUI].+",arg):selected.append(arg)
                    index+=1
                args=["/P",output,"/I",directory]+selected+[filename]
            else:
                args=["-Fi" if arg in ("-Fo","/Fo") else arg for arg in args]
                args=["-P","-I",directory]+args
            raw,_=self.runner._run([program]+args,directory,step["timeoutSeconds"],self._environment(recipe,directory))
            if not family.startswith("glslang"):
                with open(output,"rb") as handle:raw=handle.read(16*1024*1024+1)
            if len(raw)>16*1024*1024 or b"\0" in raw: raise RuntimeError("Preprocessed source is invalid or oversized")
            self.identity(recipe)
            return raw.decode("utf8")
