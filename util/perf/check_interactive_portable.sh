#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
mkdir -p output/linux-interactive
find qrenderdoc/Windows qrenderdoc/Widgets -name '*.ui' -print | while IFS= read -r source; do
  name=$(basename "$source" .ui)
  if [ ! -f "output/linux-interactive/ui_$name.h" ]; then
    uic "$source" -o "output/linux-interactive/ui_$name.h"
  fi
done
for source in qrenderdoc/Code/CaptureContext.cpp qrenderdoc/Widgets/ResourcePreview.cpp qrenderdoc/Widgets/ThumbnailStrip.cpp qrenderdoc/Windows/RenderTargetHistoryView.cpp qrenderdoc/Windows/TextureViewer.cpp qrenderdoc/Windows/BufferViewer.cpp qrenderdoc/Windows/ShaderViewer.cpp qrenderdoc/Windows/APIInspector.cpp; do
  name=$(basename "$source" .cpp)
  g++ -std=c++14 -fPIC -fsyntax-only -Ioutput/linux-interactive -Ioutput/linux-integration -I. -Iqrenderdoc -Iqrenderdoc/3rdparty -Irenderdoc -Irenderdoc/api/replay -Iqrenderdoc/3rdparty/scintilla/include -Iqrenderdoc/3rdparty/scintilla/qt/ScintillaEdit -Iqrenderdoc/3rdparty/scintilla/qt/ScintillaEditBase -DRENDERDOC_PLATFORM_LINUX $(pkg-config --cflags Qt5Widgets Qt5Network Qt5Xml Qt5Concurrent Qt5X11Extras) "$source" > "output/linux-interactive/$name.log" 2>&1
done
