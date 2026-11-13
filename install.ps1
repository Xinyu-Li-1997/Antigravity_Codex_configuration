# Antigravity & Codex 安装入口，兼容 Windows PowerShell 5.1 和 PowerShell 7。
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONDONTWRITEBYTECODE = "1"

function Find-WorkingPython {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    # 跳过应用商店别名，逐个尝试 PATH 中的真实程序。
    $commands = Get-Command $Exe -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\Microsoft\WindowsApps\*" }
    foreach ($commandInfo in $commands) {
        $process = [System.Diagnostics.Process]::new()
        try {
            $process.StartInfo.FileName = $commandInfo.Source
            $process.StartInfo.Arguments = (@($PrefixArgs) + @(
                '-I', '-B', '-c',
                '"import sys; print(''user-guard-python-ok''); raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"'
            )) -join ' '
            $process.StartInfo.UseShellExecute = $false
            $process.StartInfo.CreateNoWindow = $true
            $process.StartInfo.RedirectStandardOutput = $true
            $process.StartInfo.RedirectStandardError = $true
            $null = $process.Start()
            if (-not $process.WaitForExit(5000)) {
                $process.Kill()
                $null = $process.WaitForExit(1000)
                continue
            }
            $probeOutput = $process.StandardOutput.ReadToEnd().Trim()
            if ($process.ExitCode -eq 0 -and $probeOutput -eq 'user-guard-python-ok') {
                return $commandInfo.Source
            }
        } catch [System.ComponentModel.Win32Exception] {
            continue
        } catch [System.InvalidOperationException] {
            continue
        } finally {
            $process.Dispose()
        }
    }
}

$python = Find-WorkingPython "python.exe"
$launcher = if (-not $python) { Find-WorkingPython "py.exe" @("-3") }
if (-not $python -and -not $launcher) {
    Write-Error "[!] Python 3.9+ not found in PATH." -ErrorAction Continue
    exit 1
}

# 原样透传参数，并把 Python 的退出码交还调用方。
try {
    if ($python) {
        & $python -B -X utf8 "$PSScriptRoot\install.py" @args
    } else {
        & $launcher -3 -B -X utf8 "$PSScriptRoot\install.py" @args
    }
    $installerExitCode = $LASTEXITCODE
} catch [System.Management.Automation.RuntimeException] {
    Write-Error "[!] 无法启动 Python 安装器。$($_.Exception.Message)" -ErrorAction Continue
    $installerExitCode = 1
}
exit $installerExitCode
