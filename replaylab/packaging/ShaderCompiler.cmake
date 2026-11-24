# Optional, explicit first-party HLSL/GLSL acquisition and install component.
# Keep ordinary developer builds offline unless this option is requested.
option(BREAD_BUNDLE_SHADER_COMPILERS "Bundle pinned and capability-tested HLSL/GLSL compilers" OFF)
option(BREAD_BUNDLE_DXC "Compatibility alias for BREAD_BUNDLE_SHADER_COMPILERS" OFF)
set(BREAD_DXC_ARCHIVE "" CACHE FILEPATH "Offline official DXC release archive (SHA-256 checked)")
set(BREAD_GLSLANG_ARCHIVE "" CACHE FILEPATH "Offline official Ubuntu glslang-tools deb (SHA-256 checked)")
if(BREAD_BUNDLE_DXC OR BREAD_BUNDLE_SHADER_COMPILERS)
    if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux" OR CMAKE_CROSSCOMPILING OR
       NOT CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|amd64|AMD64)$")
        message(FATAL_ERROR "Bundled Shader compilers currently target verified Linux x86-64 hosts only")
    endif()
    find_program(BREAD_DXC_PYTHON_EXECUTABLE NAMES python3)
    if(NOT BREAD_DXC_PYTHON_EXECUTABLE)
        message(FATAL_ERROR "BREAD_BUNDLE_DXC requires Python 3 for pinned acquisition and verification")
    endif()
    if(DEFINED ENV{XDG_CACHE_HOME} AND NOT "$ENV{XDG_CACHE_HOME}" STREQUAL "")
        set(_bread_dxc_cache_default "$ENV{XDG_CACHE_HOME}/bread/dxc/v1.8.2407/linux-x64")
        set(_bread_glslang_cache_default "$ENV{XDG_CACHE_HOME}/bread/glslang/11.8.0-1/linux-x64")
    else()
        set(_bread_dxc_cache_default "$ENV{HOME}/.cache/bread/dxc/v1.8.2407/linux-x64")
        set(_bread_glslang_cache_default "$ENV{HOME}/.cache/bread/glslang/11.8.0-1/linux-x64")
    endif()
    set(BREAD_DXC_CACHE_DIRECTORY "${_bread_dxc_cache_default}" CACHE PATH "Pinned official DXC download cache")
    set(BREAD_GLSLANG_CACHE_DIRECTORY "${_bread_glslang_cache_default}" CACHE PATH "Pinned official glslang download cache")
    set(_bread_dxc_script "${CMAKE_CURRENT_LIST_DIR}/fetch_shader_dxc.py")
    set(_bread_glslang_script "${CMAKE_CURRENT_LIST_DIR}/fetch_shader_glslang.py")
    set(_bread_dxc_build_bundle "${CMAKE_BINARY_DIR}/bin/replaylab/tools/dxc")
    set(_bread_glslang_build_bundle "${CMAKE_BINARY_DIR}/bin/replaylab/tools/glslang")
    set(_bread_dxc_archive_args)
    set(_bread_glslang_archive_args)
    if(BREAD_DXC_ARCHIVE)
        list(APPEND _bread_dxc_archive_args --archive "${BREAD_DXC_ARCHIVE}")
    endif()
    if(BREAD_GLSLANG_ARCHIVE)
        list(APPEND _bread_glslang_archive_args --archive "${BREAD_GLSLANG_ARCHIVE}")
    endif()
    add_custom_target(bundle-bread-dxc
        COMMAND "${BREAD_DXC_PYTHON_EXECUTABLE}" "${_bread_dxc_script}"
            --platform linux-x64 --ensure-cache "${BREAD_DXC_CACHE_DIRECTORY}"
            --stage-directory "${_bread_dxc_build_bundle}" ${_bread_dxc_archive_args}
        COMMENT "Verify and stage pinned Bread DXC with its original bin/lib layout"
        VERBATIM)
    add_custom_target(bundle-bread-glslang
        COMMAND "${BREAD_DXC_PYTHON_EXECUTABLE}" "${_bread_glslang_script}"
            --ensure-cache "${BREAD_GLSLANG_CACHE_DIRECTORY}"
            --stage-directory "${_bread_glslang_build_bundle}" ${_bread_glslang_archive_args}
        COMMENT "Verify and stage pinned Bread glslang with its copyright notices"
        VERBATIM)
    add_custom_target(bundle-bread-shader-compilers ALL DEPENDS bundle-bread-dxc bundle-bread-glslang)
    if(TARGET build-qrenderdoc)
        add_dependencies(build-qrenderdoc bundle-bread-shader-compilers)
    endif()
    install(CODE "
        execute_process(COMMAND \"${BREAD_DXC_PYTHON_EXECUTABLE}\" \"${_bread_dxc_script}\"
            --platform linux-x64 --verify-directory \"${_bread_dxc_build_bundle}\"
            RESULT_VARIABLE _bread_dxc_result)
        if(NOT _bread_dxc_result EQUAL 0)
            message(FATAL_ERROR \"Pinned DXC is absent or invalid. Build bundle-bread-shader-compilers before installing.\")
        endif()
        execute_process(COMMAND \"${BREAD_DXC_PYTHON_EXECUTABLE}\" \"${_bread_glslang_script}\"
            --verify-directory \"${_bread_glslang_build_bundle}\"
            RESULT_VARIABLE _bread_glslang_result)
        if(NOT _bread_glslang_result EQUAL 0)
            message(FATAL_ERROR \"Pinned glslang is absent or invalid. Build bundle-bread-shader-compilers before installing.\")
        endif()"
        COMPONENT bread-shader-compilers)
    install(DIRECTORY "${_bread_dxc_build_bundle}/"
        DESTINATION share/renderdoc/replaylab/tools/dxc
        USE_SOURCE_PERMISSIONS COMPONENT bread-shader-compilers)
    install(DIRECTORY "${_bread_glslang_build_bundle}/"
        DESTINATION share/renderdoc/replaylab/tools/glslang
        USE_SOURCE_PERMISSIONS COMPONENT bread-shader-compilers)
endif()
