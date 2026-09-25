@echo off
REM =============================================================================
REM build_py313.bat -- The Hijacker audio engine build script, Python 3.13 target
REM Requires: MSVC (cl.exe), Python 3.13, pybind11, PortAudio
REM
REM Blender switched its bundled Python from 3.11 to 3.13 in Blender 5.1
REM ("matching VFX Platform 2026" -- see Blender's own 5.1 Python API release
REM notes), which is why Blender 5.1/5.2 log "DLL load failed while importing
REM hijacker_engine" against the build.bat output. This script compiles a
REM second build against Python 3.13 so both ABI lines keep working side by
REM side. Unlike an earlier version of this script, the output is NOT pinned
REM to one specific Blender version -- it lands at
REM blender_sync\hijacker_native\win_amd64_py313\hijacker_engine.pyd, and
REM core/engine.py picks the right subfolder at runtime by checking the
REM *actual running Python's* version, not Blender's version number. That
REM means this same build keeps working for Blender 5.3, 5.4, etc. for as
REM long as they stay on Python 3.13 -- a new build here is only needed again
REM if/when Blender's bundled Python major.minor changes again.
REM
REM Blender doesn't ship its C++ source (audaspace headers) alongside the
REM Python runtime it bundles, so this uses the same minimal audaspace stub
REM headers already proven in .github/workflows/build.yml's CI builds instead
REM of real Blender source headers -- hijacker_processor.cpp only ever needs
REM aud::sample_t and opaque ISound/IReader types, never any of their actual
REM methods, so the stubs are a drop-in replacement.
REM =============================================================================

set TOOL_DIR=C:\Users\lukeb\Documents\BlenderTool
set SRC=%TOOL_DIR%\src
set INC=%TOOL_DIR%\include
set DEPS=%TOOL_DIR%\deps
set OUT=%TOOL_DIR%\blender_sync
set NATIVE_OUT=%OUT%\hijacker_native\win_amd64_py313

REM Same PortAudio static lib build.bat uses -- not Python-version-specific.
set PA_INC=%DEPS%\portaudio\include
set PA_LIB=%DEPS%\portaudio\lib\portaudio_static_x64.lib

if not exist "%NATIVE_OUT%" mkdir "%NATIVE_OUT%"

cl /O2 /LD /EHsc /std:c++17 ^
    /I "%INC%" ^
    /I "%INC%\audaspace_stub" ^
    /I "%SRC%" ^
    /I "%SRC%\imgui" ^
    /I "%DEPS%\python313\include" ^
    /I "%DEPS%\pybind11\include" ^
    /I "%PA_INC%" ^
    "%SRC%\wrapper.cpp" ^
    "%SRC%\hijacker_processor.cpp" ^
    "%SRC%\hijacker_audio_engine.cpp" ^
    "%SRC%\mixer_ui.cpp" ^
    "%SRC%\imgui\imgui.cpp" ^
    "%SRC%\imgui\imgui_draw.cpp" ^
    "%SRC%\imgui\imgui_widgets.cpp" ^
    "%SRC%\imgui\imgui_tables.cpp" ^
    /link ^
    /OUT:"%NATIVE_OUT%\hijacker_engine.pyd" ^
    /LIBPATH:"%DEPS%\python313\libs" ^
    "%PA_LIB%" ^
    winmm.lib ^
    ole32.lib ^
    uuid.lib ^
    advapi32.lib

echo.
echo Build complete: %NATIVE_OUT%\hijacker_engine.pyd
