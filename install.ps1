# Antigravity & Codex unified configuration installer bootstrap (Windows PowerShell)
$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONDONTWRITEBYTECODE = "1"

function Find-WorkingPython {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    $cmd = Get-Command $Exe -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\Microsoft\WindowsApps\*" } |
        Select-Object -First 1
    if (-not $cmd) { return }
    try {
        & $cmd.Source @PrefixArgs -I -B -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $cmd.Source }
    } catch {
        return
    }
}

$python = Find-WorkingPython "python.exe"
$launcher = if (-not $python) { Find-WorkingPython "py.exe" @("-3") }
if ($python) {
    & $python -B -X utf8 "$PSScriptRoot\install.py" @args
} elseif ($launcher) {
    & $launcher -3 -B -X utf8 "$PSScriptRoot\install.py" @args
} else {
    Write-Error "[!] Python 3.9+ not found in PATH."
    exit 1
}
exit $LASTEXITCODE
