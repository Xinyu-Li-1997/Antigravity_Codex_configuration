# Antigravity & Codex unified configuration installer bootstrap (Windows PowerShell)
$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONDONTWRITEBYTECODE = "1"

function Test-WorkingPython {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    if ($cmd.Source -like "*\Microsoft\WindowsApps\*") { return $false }
    try {
        & $Exe @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (Test-WorkingPython "python") {
    & python -B -X utf8 "$PSScriptRoot\install.py" @args
} elseif (Test-WorkingPython "py" @("-3")) {
    & py -3 -B -X utf8 "$PSScriptRoot\install.py" @args
} else {
    Write-Error "[!] Python 3.9+ not found in PATH."
    exit 1
}
exit $LASTEXITCODE
