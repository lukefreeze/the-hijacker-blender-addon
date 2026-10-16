@echo off
REM =============================================================================
REM build_warnings.bat - compile the engine files at /W4 and save the warnings.
REM Compile only: nothing is linked and your .pyd files are not touched.
REM Run it from the "x64 Native Tools Command Prompt for VS", then open
REM build\warnings.txt (or paste its contents back to Claude).
REM =============================================================================
set TOOL_DIR=C:\Users\lukeb\Documents\BlenderTool
set SRC=%TOOL_DIR%\src
set INC=%TOOL_DIR%\include
set DEPS=%TOOL_DIR%\deps
if not exist "%TOOL_DIR%\build\warn" mkdir "%TOOL_DIR%\build\warn"

cl /c /W4 /O2 /EHsc /std:c++17 /nologo ^
    /Fo"%TOOL_DIR%\build\warn\\" ^
    /I "%INC%" ^
    /I "%INC%\audaspace_stub" ^
    /I "%INC%\blender-4.5\extern\audaspace\include" ^
    /I "%INC%\blender-4.5\extern\audaspace\include\fx" ^
    /I "%INC%\blender-4.5\extern\audaspace\include\respec" ^
    /I "%INC%\blender-4.5\extern\audaspace\include\util" ^
    /I "%INC%\blender-4.5\extern\audaspace\include\devices" ^
    /I "%INC%\blender-4.5\extern\audaspace\include\file" ^
    /I "%INC%\blender-4.5\extern\audaspace\include\sequence" ^
    /I "%SRC%" ^
    /I "%SRC%\imgui" ^
    /I "%DEPS%\python\include" ^
    /I "%DEPS%\pybind11\include" ^
    /I "%DEPS%\portaudio\include" ^
    "%SRC%\wrapper.cpp" ^
    "%SRC%\hijacker_processor.cpp" ^
    "%SRC%\hijacker_audio_engine.cpp" > "%TOOL_DIR%\build\warnings.txt" 2>&1

findstr /C:"warning C" "%TOOL_DIR%\build\warnings.txt" | findstr /V /C:"deps\\" > "%TOOL_DIR%\build\warnings_ours.txt"
for /f %%A in ('find /c /v "" ^< "%TOOL_DIR%\build\warnings_ours.txt"') do echo Warnings in our own files: %%A
echo Full output: %TOOL_DIR%\build\warnings.txt
