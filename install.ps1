# Akana bootstrap (Windows) — the friendly first run.
#
# EASIER: run `.\install.cmd` instead. It is a tiny batch wrapper that invokes
# this script with the -ExecutionPolicy Bypass flag already set, so you do not
# have to change Windows' script execution policy. Pass the same arguments:
#
#   .\install.cmd
#   .\install.cmd --yes
#   .\install.cmd --repair
#   .\install.cmd --lang tr
#
# Finds a Python 3.11+ interpreter (telling you how to install one if it's
# missing), then hands off to the real setup wizard. If you prefer to invoke
# PowerShell directly (for example, from an existing PowerShell session where
# you have already handled the execution policy), these work too:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 --yes
#   powershell -ExecutionPolicy Bypass -File install.ps1 --repair
#   powershell -ExecutionPolicy Bypass -File install.ps1 --lang tr
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# The few bootstrap lines run BEFORE the wizard, so they may print Turkish (ç ş ı …).
# Force UTF-8 output so those characters render instead of turning into mojibake.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# Language for the bootstrap's OWN messages. A language isn't chosen yet, so: honor an
# explicit --lang (also forwarded to the wizard), otherwise show BOTH languages — the
# same "show both before a choice" rule the wizard's language picker uses.
function Get-LangSel([string[]]$a) {
    for ($i = 0; $i -lt $a.Count; $i++) {
        if ($a[$i] -eq "--lang" -and ($i + 1) -lt $a.Count) { return $a[$i + 1].ToLower() }
        if ($a[$i] -like "--lang=*") { return ($a[$i] -replace "^--lang=", "").ToLower() }
    }
    return ""
}
$LangSel = Get-LangSel $args

function Say([string]$en, [string]$tr) {
    switch ($LangSel) {
        "tr" { Write-Host $tr }
        "en" { Write-Host $en }
        default { Write-Host $en; Write-Host $tr }  # bilingual until a language is picked
    }
}

function Test-PyVersion([string[]]$prefix) {
    $rest = @()
    if ($prefix.Count -gt 1) { $rest = $prefix[1..($prefix.Count - 1)] }
    # $ErrorActionPreference = "Stop" (top of file) turns a launch-level failure into a
    # terminating error: a Windows Store python.exe alias stub, a broken py-launcher shim,
    # or an access-denied exec all throw here even though Get-Command found the name. Catch
    # it so this candidate is just "not usable" and Find-Python tries the next one, instead
    # of aborting the whole bootstrap with a stack trace.
    try {
        & $prefix[0] @rest "-c" "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    # Prefer the py launcher with an explicit version, then its default-3.x, then
    # generic commands. `py -3` (and bare `py`) launch the newest installed Python,
    # so a machine with only 3.14+ (no versioned tag below matches) is still found.
    # Test-PyVersion enforces the >= 3.11 floor, so a too-old default is skipped —
    # no version CEILING here, so a future 3.15 keeps working without a code change.
    $candidates = @(
        , @("py", "-3.14")
        , @("py", "-3.13")
        , @("py", "-3.12")
        , @("py", "-3.11")
        , @("py", "-3")
        , @("py")
        , @("python3")
        , @("python")
    )
    foreach ($c in $candidates) {
        if (Get-Command $c[0] -ErrorAction SilentlyContinue) {
            # `,$c` (comma operator) prevents PowerShell from unrolling a single-element
            # array on return. Without it, returning @("python3") from this function
            # arrives at the caller as the bare string "python3", and `$py[0]` at the
            # call site then indexes into the STRING and yields the character 'p'
            # — the bootstrap tries to run "p akana.py setup" and dies. The trap only
            # fires on the single-element candidates ("python3", "python"); the
            # `py -3.x` case has two elements and looks fine, which is why the bug
            # hid on machines that had the py launcher.
            if (Test-PyVersion $c) { return ,$c }
        }
    }
    return $null
}

$py = Find-Python
if ($null -eq $py) {
    Write-Host ""
    Say "Akana needs Python 3.11 or newer, but none was found." "Akana için Python 3.11 veya üstü gerekli, ancak bulunamadı."
    Write-Host ""
    Write-Host "  winget:  winget install Python.Python.3.12"
    Write-Host "  choco:   choco install python"
    Say "  ...or download from https://www.python.org/downloads/ (tick 'Add to PATH')." "  ...ya da indir: https://www.python.org/downloads/ ('Add to PATH' kutusunu işaretle)."
    Write-Host ""
    Say "Then re-run:  powershell -ExecutionPolicy Bypass -File install.ps1" "Sonra tekrar çalıştır:  powershell -ExecutionPolicy Bypass -File install.ps1"
    exit 1
}

# Belt-and-suspenders: even with Find-Python's `,$c` return, wrap $py in @() here so
# a future edit to Find-Python that drops the comma cannot silently reintroduce the
# character-indexing bug. `@("python3")` stays a 1-element array; `@(@("py","-3.11"))`
# stays a 2-element array; `@($null)` becomes `@()` (empty) — but the null case was
# already caught above, so that path is unreachable here.
$py = @($py)
$exe = [string]$py[0]
$rest = @()
if ($py.Count -gt 1) { $rest = $py[1..($py.Count - 1)] }

if ([string]::IsNullOrWhiteSpace($exe)) {
    # Paranoid: if the resolved executable name is somehow empty/whitespace, refuse
    # to invoke `& ''` (which throws a confusing "Cannot bind argument to parameter
    # 'Path' because it is an empty string.") and give the user an actionable error.
    Say "Akana bootstrap: could not resolve a Python executable name." "Akana bootstrap: Python çalıştırılabilir adı çözümlenemedi."
    exit 1
}

Say "Using Python: $($py -join ' ')" "Python kullanılıyor: $($py -join ' ')"
& $exe @rest "akana.py" "setup" @args
exit $LASTEXITCODE
