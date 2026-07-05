@echo off
setlocal
rem Akana bootstrap wrapper for Windows.
rem
rem PowerShell's default execution policy (Restricted or RemoteSigned) blocks
rem unsigned .ps1 scripts, which stops `.\install.ps1` on a fresh Windows box
rem with "running scripts is disabled on this system." This batch file is NOT
rem subject to execution policy, so `.\install.cmd` works on any machine. It
rem launches install.ps1 with -ExecutionPolicy Bypass for this one process,
rem without changing the system-wide policy.
rem
rem All command-line arguments are forwarded to install.ps1, so these work too:
rem
rem   .\install.cmd
rem   .\install.cmd --yes
rem   .\install.cmd --repair
rem   .\install.cmd --lang tr
rem
rem %~dp0 expands to the directory containing THIS .cmd (with trailing "\"),
rem so install.ps1 is found next to install.cmd regardless of where the user
rem invoked the script from.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
