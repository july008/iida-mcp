@echo off
setlocal

if not defined MSVC (
    set VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe
    if exist "%VSWHERE%" (
        for /f "usebackq delims=" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VS_INSTALL=%%I
    )
    if defined VS_INSTALL (
        for /f "delims=" %%I in ('dir /b /ad /o-n "%VS_INSTALL%\VC\Tools\MSVC" 2^>nul') do (
            if not defined MSVC set MSVC=%VS_INSTALL%\VC\Tools\MSVC\%%I
        )
    )
)

if not defined WDK set WDK=%ProgramFiles(x86)%\Windows Kits\10

if not defined SDK_VER (
    for /f "delims=" %%I in ('dir /b /ad /o-n "%WDK%\Include" 2^>nul') do (
        if exist "%WDK%\Include\%%I\km" if not defined SDK_VER set SDK_VER=%%I
    )
)

if not defined MSVC (
    echo MSVC path not found. Install Visual Studio Build Tools or set MSVC before running this script.
    exit /b 1
)

if not defined SDK_VER (
    echo WDK SDK version not found. Install WDK or set WDK and SDK_VER before running this script.
    exit /b 1
)

set CL_EXE=%MSVC%\bin\Hostx64\x64\cl.exe
set LINK_EXE=%MSVC%\bin\Hostx64\x64\link.exe

set INCLUDE=%WDK%\Include\%SDK_VER%\km;%WDK%\Include\%SDK_VER%\shared;%WDK%\Include\%SDK_VER%\ucrt;%MSVC%\include
set LIB=%WDK%\Lib\%SDK_VER%\km\x64;%MSVC%\lib\x64

echo [1/2] Compiling driver.c ...
"%CL_EXE%" /nologo /c /kernel /GS- /Gz /W4 /WX /Ox /D_AMD64_ /DAMD64 /DNDEBUG /Zp8 /Zl /Fo"driver.obj" driver.c
if errorlevel 1 (
    echo Compile failed.
    exit /b 1
)

echo [2/2] Linking iida-mcp-ioctl.sys ...
"%LINK_EXE%" /nologo /DRIVER /SUBSYSTEM:NATIVE /ENTRY:DriverEntry /OUT:iida-mcp-ioctl.sys /RELEASE /OPT:REF /OPT:ICF /MERGE:.rdata=.text driver.obj ntoskrnl.lib hal.lib wdm.lib BufferOverflowK.lib ntstrsafe.lib
if errorlevel 1 (
    echo Link failed.
    exit /b 1
)

echo Done: iida-mcp-ioctl.sys
del /q driver.obj 2>nul
endlocal
