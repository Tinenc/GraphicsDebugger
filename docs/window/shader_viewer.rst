Shader Viewer
=============

The Shader Viewer is used for displaying, editing, and debugging shaders.

Information about editing shaders can be found in other documents such as :doc:`../how/how_edit_shader` and :doc:`../how/how_custom_visualisation`.

Debugging shaders is documented in :doc:`../how/how_debug_shader`.

Overview
--------

.. figure:: ../imgs/Screenshots/ShaderViewer.png

	The shader viewer displaying a D3D11 shader

When opened from the :doc:`pipeline_state` for a given shader stage, the shader viewer display the shader itself as well as the input and output signatures. These signatures form the interface between this shader stage and the previous and next parts of the rendering pipeline. For example a vertex shader will list the fixed function vertex inputs, and the interpolated values that are passed to the next stage.

The shader viewer displays the source code in the formats that are available. When debugging information has embedded it there will be tabs for each original source file. For APIs using a bytecode IR the disassembly of that bytecode will also be displayed in a separate tab.

Several different disassembly forms are possible, depending on the API and available plugins and hardware support. Above the disassembly tab will be a drop-down listing the possible representations including hardware specific ISA formats like AMD GCN.

You can configure shader processing tools :ref:`in the settings window <shader-processing-tools-config>` which allows for various customised disassemblers such as SPIRV-Cross and spirv-dis for SPIR-V.

Code editing and navigation
---------------------------

Shader source tabs provide IDE-style editing assistance. :kbd:`Ctrl-Space` opens completion,
typing an identifier updates suggestions, and typing ``(`` or ``,`` displays parameter information.
Structural errors and language-server diagnostics are underlined; hovering an underline displays
its message. Press :kbd:`F12` to navigate to a definition in another source tab. Definitions in an
external include are opened in a read-only tab so they cannot accidentally become part of the
shader replacement.

The editor maintains a local symbol index and lightweight structural diagnostics, so these features
remain available without extra software. For compiler-aware HLSL, GLSL, or Slang results, configure
``shader-language-server`` or ``slangd`` using the :guilabel:`Language server` setting. The
integration uses LSP over a bounded, out-of-process connection and does not execute commands sent
by a server.

The Shader Workspace uses the same native editor. Declared variable and function names have
distinct colours, and the identifier under the cursor highlights matching occurrences outside
comments and strings. A diagnostic count below the editor jumps to its first reported location.
Without a language server, live checks cover structural problems such as unmatched delimiters and
unfinished strings; they do not provide full HLSL type or binding validation. Saving runs the
workspace compiler and displays its actual diagnostics while keeping the last successful replacement.
Diagnostics for an older saved revision are not underlined on newly edited text.

The right-hand flow graph initially fits the available area without changing its aspect ratio.
Resizing keeps that automatic fit until you zoom or pan. Manual views are retained when switching
documents or refreshing the same graph. :guilabel:`Fit` restores automatic fitting and
:guilabel:`100%` shows readable actual-size nodes with scrolling available.

Pressing :kbd:`Ctrl-F` opens an inline search bar in the upper-right of the active source tab. Search
results update while typing and show the current/total match count. :kbd:`Enter` or :kbd:`F3` moves
forward, :kbd:`Shift-Enter` or :kbd:`Shift-F3` moves backward, :kbd:`Ctrl-H` expands replacement,
and :kbd:`Escape` closes the bar. Case, whole-word, regular-expression, current-file, and all-source
search modes are available without leaving the editor.

.. figure:: ../imgs/Screenshots/ShaderDisassembly.png

	The disassembly options for a Vulkan SPIR-V shader
