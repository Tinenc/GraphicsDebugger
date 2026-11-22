"""Portable packaged compiler runner for transient visual-debug programs.

Fixed first-party locations take precedence; installed PATH tools are a portable
fallback. Executables stay in place (important for Unix loader/RPATH semantics).
Source stays in an OS-temp directory, subprocesses never use a shell, and no
workspace revision is written. Cache identities include resolved compiler bytes
and loaded compiler-library dependencies, not just a version string or filename.
"""
import hashlib
import bisect
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile


class CompilerError(RuntimeError):
    pass


def _arguments(command):
    """Split RenderDoc's double-quoted command-line metadata without a shell.

    Backslashes are literal except immediately before a double quote, matching
    the argv serialization used by DXIL reflection on every host platform.
    """
    if not isinstance(command, str) or "\0" in command or len(command) > 262144:
        raise CompilerError("Shader compiler options are invalid or oversized")
    result, word, quoted, active, i = [], [], False, False, 0
    while i < len(command):
        char = command[i]
        if char.isspace() and not quoted:
            if active:
                result.append("".join(word))
                word, active = [], False
            i += 1
        elif char == "\\":
            end = i
            while end < len(command) and command[end] == "\\":
                end += 1
            count = end - i
            if end < len(command) and command[end] == '"':
                word.append("\\" * (count // 2))
                if count % 2:
                    word.append('"')
                else:
                    quoted = not quoted
                i = end + 1
            else:
                word.append("\\" * count)
                i = end
            active = True
        elif char == '"':
            quoted, active, i = not quoted, True, i + 1
        else:
            word.append(char)
            active, i = True, i + 1
    if quoted:
        raise CompilerError("Shader compiler options contain an unterminated quote")
    if active:
        result.append("".join(word))
    return result


def compile_options(flag_values):
    """Return safe semantic options, excluding captured paths/output switches.

    Unknown flags fail explicitly: silently dropping an unfamiliar layout or
    language switch could produce a valid Shader with different semantics.
    Entry/profile and all filesystem destinations belong to the preview runner.
    """
    arguments = []
    for item in flag_values or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise CompilerError("Shader compiler flag records are invalid")
        name, value = str(item[0]), str(item[1])
        if name == "@cmdline":
            arguments.extend(_arguments(value))
        elif name == "@spirver":
            # Reflection metadata, not an argv option. The replay driver passes
            # its decoded version separately as spirv_version for codegen.
            if not re.fullmatch(r"spirv1\.[0-6]", value):
                raise CompilerError("Unsupported reflected SPIR-V version: " + value)
            continue
        elif name in {"preferSourceDebug", "preferBinaryDebug"}:
            continue
        elif name.startswith(("-", "/")):
            arguments.append(name)
            if value:
                arguments.append(value)
        else:
            raise CompilerError("Unsupported Shader compiler flag: " + name)
    switches = set(("O0 O1 O2 O3 Od Os Zi Zs Zpc Zpr Zsb Zss Ges Gec Gfa Gfp Gis Gpp Op WX Vd "
                    "enable-16bit-types flegacy-macro-expansion flegacy-resource-reservation "
                    "all-resources-bound res-may-alias enable-unbounded-descriptor-tables "
                    "fvk-auto-shift-bindings fvk-invert-y fvk-support-nonzero-base-instance "
                    "fvk-use-dx-layout fvk-use-gl-layout fvk-use-scalar-layout fvk-use-dx-position-w "
                    "fspv-flatten-resource-arrays fspv-preserve-bindings fspv-preserve-interface "
                    "fspv-reduce-load-size fspv-reflect fspv-use-legacy-buffer-matrix-order "
                    "pack-optimized pack-prefix-stable global-extern-by-default "
                    "Qembed_debug Gfp Gfa no-warnings g").split())
    one_value = {"HV", "denorm", "auto-binding-space", "default-linkage", "rootsig-define"}
    shifts = {"fvk-b-shift": 2, "fvk-s-shift": 2, "fvk-t-shift": 2, "fvk-u-shift": 2,
              "fvk-bind-globals": 2, "fvk-bind-register": 4}
    ignored_value = {"E", "T", "I", "Fo", "Fc", "Fd", "Fe", "Fh", "Fi", "Fre", "Frs", "Fsh", "Fsi", "Fsl", "Fne"}
    ignored_switch = {"P", "Qstrip_debug", "Qstrip_reflect", "Qstrip_rootsignature", "Qstrip_priv", "nologo"}
    replacements = {"all_resources_bound": "all-resources-bound", "res_may_alias": "res-may-alias",
                    "enable_unbounded_descriptor_tables": "enable-unbounded-descriptor-tables"}
    result, i = [], 0
    while i < len(arguments):
        argument = arguments[i]
        if (not argument.startswith("-") and not argument.startswith("@") and
                re.search(r"\.(?:hlsl|hlsli|fx|fxh|glsl|frag|vert)$", argument, re.IGNORECASE)):
            # Captured source filenames are replaced by the owned mirror.
            i += 1
            continue
        if not argument.startswith(("-", "/")) or argument.startswith("@"):
            raise CompilerError("Unrecognized positional Shader compiler argument: " + argument)
        option = argument[1:]
        if option in ("-entry-point", "-source-entrypoint", "-source-entry-point", "-stage"):
            if i + 1 >= len(arguments) or not re.fullmatch(r"[A-Za-z_0-9]+", arguments[i + 1]):
                raise CompilerError("Invalid captured Shader entry/stage option")
            i += 2
            continue
        if option in ("-client", "-target-env"):
            if i + 1 >= len(arguments):
                raise CompilerError("Missing captured Shader target environment")
            value = arguments[i + 1]
            if option == "-client":
                if value not in ("vulkan100", "opengl100"):
                    raise CompilerError("Unknown captured Shader client: " + value)
                # API/encoding chooses this mode; version metadata below still
                # participates in the compiler cache and external target env.
            elif re.fullmatch(r"vulkan1\.[0-4](?:spirv1\.4)?|spirv1\.[0-6]|opengl", value):
                result.append("-fspv-target-env=" + value)
            else:
                raise CompilerError("Unknown captured Shader target environment: " + value)
            i += 2
            continue
        if option in ignored_value:
            if i + 1 >= len(arguments):
                raise CompilerError("Missing value for Shader compiler option " + argument)
            i += 2
            continue
        if any(option.startswith(prefix) and len(option) > len(prefix) for prefix in ignored_value if prefix not in {"E", "T", "I"}):
            i += 1
            continue
        if option.startswith(("E", "T", "I")) and len(option) > 1 and option not in switches:
            i += 1
            continue
        if option in ignored_switch:
            i += 1
            continue
        if option.startswith(("D", "U")):
            value = option[1:]
            if not value:
                i += 1
                if i >= len(arguments):
                    raise CompilerError("Missing macro value for " + argument)
                value = arguments[i]
            if not re.match(r"^[A-Za-z_][A-Za-z_0-9]*(?:=|$)", value) or any(ch in value for ch in "\0\r\n"):
                raise CompilerError("Shader macro definition is invalid")
            result.append("-" + option[0] + value)
        elif option in ("enable_unbounded_descriptor_tables", "enable-unbounded-descriptor-tables"):
            # FXC needs this opt-in for SM 5.1. DXC/SM 6 supports unbounded
            # resource arrays by default and rejects the legacy switch.
            pass
        elif replacements.get(option, option) in switches:
            result.append("-" + replacements.get(option, option))
        elif option in one_value or option in shifts:
            count = shifts.get(option, 1)
            values = arguments[i + 1:i + count + 1]
            if len(values) != count or any(v.startswith(("-", "@")) for v in values):
                raise CompilerError("Missing or invalid value for " + argument)
            if option in shifts and any(not re.fullmatch(r"[0-9]+|all|[bstu][0-9]+", value) for value in values):
                raise CompilerError("Invalid SPIR-V binding mapping")
            if option in one_value and not re.fullmatch(r"[A-Za-z_0-9]+", values[0]):
                raise CompilerError("Invalid value for " + argument)
            result += ["-" + option] + values
            i += count
        elif re.fullmatch(r"HV(?:2016|2017|2018|2021)", option):
            result += ["-HV", option[2:]]
        elif option.startswith(("fspv-target-env=", "fspv-extension=", "fspv-debug=")):
            if not re.fullmatch(r"[A-Za-z_0-9.,+\-=]+", option):
                raise CompilerError("Invalid SPIR-V compiler option")
            result.append("-" + option)
        elif option.startswith("fspv-entrypoint-name="):
            if not re.fullmatch(r"fspv-entrypoint-name=[A-Za-z_][A-Za-z_0-9]*", option):
                raise CompilerError("Invalid captured SPIR-V entry point")
            # The selected pipeline's exported name is supplied independently
            # by the replay driver, just like -E and -T.
        elif re.fullmatch(r"W(?:no-)?[A-Za-z_0-9-]+", option):
            result.append("-" + option)
        elif option == "spirv":
            # Physical API controls this switch; it cannot change target kind.
            pass
        else:
            raise CompilerError("Unsupported semantic Shader compiler option: " + argument)
        i += 1
    return result


def has_preprocessor_options(flag_values):
    return any(value.startswith(("-D", "-U")) for value in compile_options(flag_values))


def effective_options(flag_values, language, api):
    options = compile_options(flag_values)
    # SPIRV-Cross/glslang Vulkan HLSL uses pre-2021 vector logical and
    # conditional operators. A newer private DXC must not silently reinterpret
    # that source under its changing default language version. Explicit captured
    # versions still win; DirectX and GLSL retain their existing compiler policy.
    if language == "hlsl" and api == "vulkan" and "-HV" not in options:
        options += ["-HV", "2018"]
    return options


def _mirror_source(source, blocked, relative="input.hlsl"):
    """Keep compiler conditionals intact while denying out-of-unit includes.

    Rewriting only a literal filename (not the directive) means an inactive
    missing include stays harmless and an active forbidden include fails in
    the actual preprocessor. Strings and comments are not interpreted as code.
    """
    def hide_comment(match):
        value = match.group(0)
        return re.sub(r"[^\r\n]", " ", value) if value.startswith(("//", "/*")) else value
    # C-style line splicing happens before comment/directive recognition.
    # Keep a compact offset map instead of changing physical line numbers
    # (__LINE__ must continue to match the captured source).
    offsets, removed, total = [], [], 0
    for splice in re.finditer(r"\\\r?\n", source):
        offsets.append(splice.start() - total)
        total += splice.end() - splice.start()
        removed.append(total)
    logical = re.sub(r"\\\r?\n", "", source)
    def original_offset(offset):
        count = bisect.bisect_right(offsets, offset)
        return offset + (removed[count - 1] if count else 0)
    masked = re.sub(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"', hide_comment, logical)
    edits = []
    for match in re.finditer(r'(?m)^[ \t]*#[ \t]*include\b([^\r\n]*)', masked):
        literal = re.fullmatch(r'[ \t]*["<]([^"<>\r\n]+)[">][ \t]*', match.group(1))
        if literal:
            name = literal.group(1)
            portable = name.replace("\\", "/")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative.replace("\\", "/")), portable))
            outside = portable.startswith("/") or re.match(r"^[A-Za-z]:", portable) or resolved == ".." or resolved.startswith("../")
            begin, end = match.start(1) + literal.start(1), match.start(1) + literal.end(1)
            unavailable = name in blocked or outside
            reason = name
            replacement = portable
        else:
            # External compiler processes have no include callback sandbox.
            # A macro could expand to an absolute live file not present in the
            # immutable dependency graph. Reject only when that branch is
            # active; an inactive computed include remains harmless.
            begin, end = match.start(1), match.end(1)
            reason, unavailable = "computed:" + match.group(1), True
        if unavailable:
            marker = "__bread_unavailable_includes__/" + ("computed-" if not literal else "")
            marker += hashlib.sha256(reason.encode("utf-8")).hexdigest() + ".missing"
            replacement = marker if literal else ' "' + marker + '"'
        begin, end = original_offset(begin), original_offset(end)
        old = source[begin:end]
        replacement += "".join(re.findall(r"\\\r?\n", old))
        if replacement != old:
            edits.append((begin, end, replacement))
    for begin, end, replacement in reversed(edits):
        source = source[:begin] + replacement + source[end:]
    return source


class Compiler(object):
    compile_options = staticmethod(compile_options)
    has_preprocessor_options = staticmethod(has_preprocessor_options)

    def __init__(self, application_directory=None):
        self.application_directory = os.path.abspath(application_directory or os.path.dirname(sys.executable))
        self._identities = {}
        self._origins = {}
        self._dependency_cache = {}
        self._dependency_search_paths = {}

    def _resolved_tool(self, path, origin):
        path = os.path.realpath(os.path.abspath(path))
        if not os.path.isfile(path) or (os.name != "nt" and not os.access(path, os.X_OK)):
            raise CompilerError("The %s Shader compiler is not executable: %s" % (origin, path))
        self._origins[path] = origin
        return path

    def _tool(self, name):
        suffix = ".exe" if os.name == "nt" else ""
        roots = [os.path.join(self.application_directory, "replaylab", "tools"),
                 os.path.join(os.path.dirname(self.application_directory), "share", "renderdoc", "replaylab", "tools")]
        names = [name + suffix]
        if name == "glslang":
            names.append("glslangValidator" + suffix)
        for root in roots:
            # Visual debugging owns this private bundle. Keep older loose
            # compilers intact: saved workspace routes may pin their paths and
            # hashes. Linux also needs DXC's upstream $ORIGIN/../lib layout.
            # Execute in place, with the matching adjacent Windows DLLs.
            if name == "dxc":
                path = os.path.join(root, "dxc", "bin", "dxc" + suffix)
                if os.path.isfile(path):
                    return self._resolved_tool(path, "packaged")
            if name == "glslang" and os.name != "nt":
                path = os.path.join(root, "glslang", "bin", "glslangValidator")
                if os.path.isfile(path):
                    return self._resolved_tool(path, "packaged")
            for filename in names:
                path = os.path.join(root, filename)
                if os.path.isfile(path):
                    return self._resolved_tool(path, "packaged")
        for filename in names:
            path = shutil.which(filename)
            if path:
                return self._resolved_tool(path, "system-path")
        raise CompilerError("The packaged or installed %s Shader compiler is unavailable" % name)

    def _dependencies(self, path, name):
        files = [path]
        directory = os.path.dirname(path)
        watched = set()
        if os.name == "nt":
            names = (("dxcompiler.dll", "dxil.dll") if name == "dxc" else
                     ("d3dcompiler_47.dll",) if name == "fxc" else
                     ("glslang.dll", "SPIRV.dll", "SPIRV-Tools.dll", "SPIRV-Tools-opt.dll"))
            system = os.environ.get("SystemRoot", "C:/Windows")
            roots = [directory, os.path.join(system, "System32"), system]
            roots += os.environ.get("PATH", "").split(os.pathsep)
            watched.update(roots)
            for filename in names:
                for root in roots:
                    candidate = os.path.join(root, filename)
                    if os.path.isfile(candidate):
                        files.append(candidate)
                        break
        else:
            # Inspect the actual loader resolution. Never relocate a system
            # executable into the source mirror or infer a Linux .so from the
            # Windows DLL layout. ldd includes transitive ELF dependencies.
            inspector = shutil.which("otool" if sys.platform == "darwin" else "ldd")
            if inspector:
                command = [inspector, "-L", path] if sys.platform == "darwin" else [inspector, path]
                environment = dict(os.environ)
                environment["LC_ALL"] = "C"
                # The loader's own trace includes every RUNPATH/RPATH and
                # hardware-capability search directory, including missing
                # paths that could later shadow a resolved dependency.
                if sys.platform != "darwin":
                    environment["LD_DEBUG"] = "libs"
                try:
                    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, timeout=10, env=environment,
                                            creationflags=0x08000000 if os.name == "nt" else 0)
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise CompilerError("Unable to resolve Shader compiler libraries: " + str(error))
                output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                watched.add(inspector)
                for line in output.splitlines():
                    search = re.search(r"search path=(.*?)\s+\(", line)
                    if search:
                        watched.update(search.group(1).split(":"))
                    attempt = re.search(r"trying file=(.+)$", line)
                    if attempt:
                        watched.add(attempt.group(1).strip())
                if "not found" in output and "=>" in output:
                    raise CompilerError("A Shader compiler library is unavailable: " + output[-8192:])
                if result.returncode and "not a dynamic executable" not in output and "statically linked" not in output:
                    raise CompilerError("Unable to inspect Shader compiler libraries: " + output[-8192:])
                for line in output.splitlines():
                    match = re.search(r"(?:=>\s*)?(/[^\n]*?)\s+\(", line.strip())
                    if match and os.path.isfile(match.group(1)):
                        files.append(match.group(1))
                    elif "@loader_path/" in line or "@executable_path/" in line:
                        match = re.search(r"@(?:loader|executable)_path/([^\s]+)", line)
                        candidate = os.path.join(directory, match.group(1)) if match else ""
                        if candidate and os.path.isfile(candidate):
                            files.append(candidate)
                    elif "@rpath/" in line:
                        # otool cannot resolve arbitrary run paths by itself;
                        # refusing an untracked dependency is safer than a
                        # supposedly stable cache key for different compilers.
                        raise CompilerError("The Shader compiler has an unresolved @rpath library")
            else:
                # Small portable installations commonly carry their shader
                # libraries adjacent to the executable or under ../lib.
                roots = [directory, os.path.join(os.path.dirname(directory), "lib")]
                watched.update(roots)
                for root in roots:
                    if not os.path.isdir(root):
                        continue
                    for filename in sorted(os.listdir(root)):
                        if re.match(r"lib(?:dxcompiler|dxil|glslang|SPIRV|LLVM|slang).*\.(?:so(?:\..*)?|dylib)$", filename):
                            candidate = os.path.join(root, filename)
                            if os.path.isfile(candidate):
                                files.append(candidate)
        self._dependency_search_paths[(path, name)] = watched
        # Preserve the loader's spelling so cache invalidation observes a
        # library symlink retargeted without changing the old target bytes.
        return list(dict.fromkeys(os.path.abspath(filename) for filename in files))

    @staticmethod
    def _dependency_fingerprint(paths):
        watched = set()
        for filename in paths:
            if not filename:
                continue
            filename = os.path.abspath(filename)
            watched.add(filename)
            # A missing search path can spring into existence by creating an
            # ancestor; retain every ancestor, including symlink directories.
            parent = os.path.dirname(filename)
            while parent and parent not in watched:
                watched.add(parent)
                next_parent = os.path.dirname(parent)
                if next_parent == parent:
                    break
                parent = next_parent
        values = []
        for filename in sorted(watched):
            try:
                stat, link = os.stat(filename), os.lstat(filename)
                def fields(value):
                    return (value.st_size, getattr(value, "st_mtime_ns", value.st_mtime),
                            getattr(value, "st_ctime_ns", value.st_ctime), value.st_ino, value.st_mode)
                values.append((filename, os.path.realpath(filename), fields(stat), fields(link)))
            except OSError:
                values.append((filename, None))
        return tuple(values)

    def _cached_dependencies(self, path, name):
        variables = {"PATH", "GLIBC_TUNABLES", "SystemRoot"}
        variables.update(key for key in os.environ if key.startswith(("LD_", "DYLD_")))
        environment = tuple((key, os.environ.get(key, "")) for key in sorted(variables))
        key = (path, name)
        cached = self._dependency_cache.get(key)
        if cached and cached[0] == environment and cached[1] == self._dependency_fingerprint(cached[2]):
            return cached[3]
        files = self._dependencies(path, name)
        watched = set(files) | self._dependency_search_paths.get(key, set())
        watched.add(path)
        if os.name != "nt":
            watched.update(("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.preload", "/etc/ld.so.conf.d"))
        self._dependency_cache[key] = (environment, self._dependency_fingerprint(watched), watched, files)
        return files

    def identity(self, language, api, profile):
        name = "dxc" if language == "hlsl" else "glslang"
        try:
            path = self._tool(name)
        except CompilerError:
            return {"backend": "native", "language": language, "api": api, "profile": profile}
        files = list(dict.fromkeys(os.path.realpath(filename) for filename in self._cached_dependencies(path, name)))
        values = []
        for filename in files:
            stat = os.stat(filename)
            key = (filename, stat.st_size, getattr(stat, "st_mtime_ns", stat.st_mtime),
                   getattr(stat, "st_ctime_ns", stat.st_ctime), getattr(stat, "st_ino", 0))
            digest = self._identities.get(key)
            if digest is None:
                hasher = hashlib.sha256()
                with open(filename, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        hasher.update(chunk)
                after = os.stat(filename)
                if (after.st_size != stat.st_size or after.st_mtime != stat.st_mtime or
                        after.st_ctime != stat.st_ctime or after.st_ino != stat.st_ino):
                    raise CompilerError("Shader compiler changed while its identity was read")
                digest = hasher.hexdigest()
                self._identities = {k: v for k, v in self._identities.items() if k[0] != filename}
                self._identities[key] = digest
            values.append((filename, digest))
        return {"backend": name, "origin": self._origins.get(path, "unknown"),
                "files": values, "language": language, "api": api, "profile": profile,
                "optionsPolicy": "vulkan-hlsl-2018-default-v1"}

    @staticmethod
    def _run(arguments, directory, timeout_seconds=90, environment=None):
        kwargs = {"cwd": directory, "stdin": subprocess.DEVNULL,
                  "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout_seconds}
        if environment is not None:
            kwargs["env"] = environment
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            process = subprocess.run(arguments, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CompilerError(str(error))
        if len(process.stdout) > 16 * 1024 * 1024 or len(process.stderr) > 1024 * 1024:
            raise CompilerError("Shader compiler output exceeds the preview size limit")
        if process.returncode:
            raise CompilerError((process.stdout + process.stderr).decode("utf-8", errors="replace")[-32768:])
        return process.stdout, process.stderr.decode("utf-8", errors="replace")[-32768:]

    def preprocess(self, source, language, entry_point, profile="", api="", snapshot=None, flags=None):
        if language not in ("hlsl", "glsl"):
            raise CompilerError("This source language has no visual-debug preprocessor")
        name = "dxc" if language == "hlsl" else "glslang"
        executable = self._tool(name)
        options = effective_options(flags, language, api)
        with tempfile.TemporaryDirectory(prefix="bread-shader-preprocess-") as directory:
            root_name = snapshot["rootName"] if snapshot else ("input.hlsl" if language == "hlsl" else "input.frag")
            filename = os.path.join(directory, root_name)
            output = os.path.join(directory, "preprocessed.txt")
            files = dict(snapshot["files"]) if snapshot else {root_name: source}
            if snapshot:
                files[snapshot["editedName"]] = source
            for relative, text in files.items():
                portable_relative = relative.replace("\\", "/")
                if portable_relative.split("/")[0] == "__bread_unavailable_includes__":
                    raise CompilerError("Shader source uses a reserved include-mirror directory")
                path = os.path.abspath(os.path.join(directory, relative))
                if os.path.commonpath([directory, path]) != directory:
                    raise CompilerError("Shader source mirror path escaped its compilation unit")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    blocked = set(snapshot.get("blockedIncludes", {}).get(relative, [])) if snapshot else set()
                    handle.write(_mirror_source(text, blocked, relative).encode("utf-8"))
            if language == "hlsl":
                args = [executable, "-P", "-Fi", output, "-E", entry_point, "-I", directory, filename]
                args[1:1] = options
                if profile:
                    if not re.fullmatch(r"ps_[4-6]_[0-9]+(?:_level_9_[13])?", profile):
                        raise CompilerError("HLSL preprocessing requires the captured fragment profile")
                    args[1:1] = ["-T", profile]
                if api == "vulkan":
                    args[1:1] = ["-spirv"]
                stdout, diagnostics = self._run(args, directory)
                try:
                    with open(output, "rb") as handle:
                        raw = handle.read(16 * 1024 * 1024 + 1)
                except OSError as error:
                    raise CompilerError("Shader preprocessor did not produce its output: " +
                                        (diagnostics or stdout.decode("utf-8", errors="replace") or str(error)))
            else:
                args = [executable, "-E", "-S", "frag", "-I" + directory, filename]
                # glslang cannot combine -E with target/link switches. Its
                # relevant preprocessor environment is supplied below.
                options = [option for option in options if not option.startswith("-fspv-target-env=")]
                unsupported = [option for option in options if not option.startswith(("-D", "-U", "-W"))]
                if unsupported:
                    raise CompilerError("Unsupported GLSL preprocessing options: " + " ".join(unsupported))
                args[1:1] = options
                # -V implies linking and cannot be combined with -E. Match
                # RenderDoc's EShMsgVulkanRules preamble explicitly instead.
                if api == "vulkan":
                    args[1:1] = ["-DVULKAN=100"]
                elif api == "opengl" and profile == "opengl-spirv":
                    args[1:1] = ["-DGL_SPIRV=100"]
                raw, diagnostics = self._run(args, directory)
            if len(raw) > 16 * 1024 * 1024 or b"\0" in raw:
                raise CompilerError("Preprocessed Shader exceeds its size limit or contains NUL")
            return raw.decode("utf-8", errors="strict")

    def compile(self, source, language, api, entry_point, profile="", spirv_version=0x10000, flags=None,
                target_encoding=None, exported_entry=None):
        if language == "glsl" and api == "opengl" and target_encoding == "OpenGLSPIRV":
            executable = self._tool("glslang")
            options = compile_options(flags)
            if any(option.startswith("-fspv-target-env=") and option != "-fspv-target-env=opengl" for option in options):
                raise CompilerError("Captured target environment disagrees with OpenGL SPIR-V")
            options = [option for option in options if not option.startswith("-fspv-target-env=")]
            unsupported = [option for option in options if not option.startswith(("-D", "-U", "-W")) and option not in ("-Od", "-Os", "-g")]
            if unsupported:
                raise CompilerError("Unsupported OpenGL SPIR-V options: " + " ".join(unsupported))
            with tempfile.TemporaryDirectory(prefix="bread-shader-compile-") as directory:
                filename, output = os.path.join(directory, "input.frag"), os.path.join(directory, "output.spv")
                with open(filename, "wb") as handle:
                    handle.write(source.encode("utf-8"))
                exported = exported_entry or entry_point
                args = [executable, "-G", "--target-env", "opengl", "-S", "frag", "-e", exported,
                        "-o", output, filename] + options
                if exported != entry_point:
                    args[1:1] = ["--source-entrypoint", entry_point]
                stdout, diagnostics = self._run(args, directory)
                try:
                    with open(output, "rb") as handle:
                        raw = handle.read(64 * 1024 * 1024 + 1)
                except OSError as error:
                    raise CompilerError("Shader compiler did not produce its output: " + str(error))
                if not raw.startswith(b"\x03\x02\x23\x07") or len(raw) > 64 * 1024 * 1024:
                    raise CompilerError("Shader compiler returned an invalid or oversized OpenGL SPIR-V module")
                return {"bytes": raw, "encoding": "OpenGLSPIRV", "diagnostics": diagnostics}
        if language != "hlsl" or api not in ("d3d12", "vulkan"):
            raise CompilerError("This source uses its replay-native compiler")
        executable = self._tool("dxc")
        options = effective_options(flags, language, api)
        target_profile = profile
        if api == "vulkan" and not re.fullmatch(r"ps_6_[0-9]+", target_profile or ""):
            target_profile = "ps_6_0"
        if not re.fullmatch(r"ps_6_[0-9]+", target_profile or ""):
            raise CompilerError("DXC requires the captured Shader Model 6 profile")
        with tempfile.TemporaryDirectory(prefix="bread-shader-compile-") as directory:
            filename, output = os.path.join(directory, "input.hlsl"), os.path.join(directory, "output.bin")
            with open(filename, "wb") as handle:
                handle.write(source.encode("utf-8"))
            args = [executable, "-T", target_profile, "-E", entry_point, "-Fo", output, filename]
            args[1:1] = options
            if api == "vulkan":
                version = int(spirv_version)
                environment = ("vulkan1.3" if version >= 0x10600 else "vulkan1.2" if version >= 0x10500
                               else "vulkan1.1spirv1.4" if version >= 0x10400 else "vulkan1.1" if version >= 0x10300 else "vulkan1.0")
                args[1:1] = ["-spirv"] + ([] if any(option.startswith("-fspv-target-env=") for option in options) else ["-fspv-target-env=" + environment])
                if exported_entry:
                    args[1:1] = ["-fspv-entrypoint-name=" + exported_entry]
            stdout, diagnostics = self._run(args, directory)
            try:
                with open(output, "rb") as handle:
                    raw = handle.read(64 * 1024 * 1024 + 1)
            except OSError as error:
                raise CompilerError("Shader compiler did not produce its output: " + str(error))
            expected = b"\x03\x02\x23\x07" if api == "vulkan" else b"DXBC"
            if not raw.startswith(expected) or len(raw) > 64 * 1024 * 1024:
                raise CompilerError("Shader compiler returned an invalid or oversized container")
            return {"bytes": raw, "encoding": "SPIRV" if api == "vulkan" else "DXIL",
                    "diagnostics": diagnostics}
