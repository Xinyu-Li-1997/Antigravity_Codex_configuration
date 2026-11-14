# Antigravity & Codex Windows 安装入口。
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
                '"import sys; print(''user-guard-python-ok''); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"'
            )) -join ' '
            $process.StartInfo.UseShellExecute = $false
            $process.StartInfo.CreateNoWindow = $true
            $process.StartInfo.RedirectStandardOutput = $true
            $process.StartInfo.RedirectStandardError = $true
            $null = $process.Start()
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit(5000)) {
                $process.Kill()
                $null = $process.WaitForExit(1000)
                continue
            }
            if (-not $stdoutTask.Wait(1000) -or -not $stderrTask.Wait(1000)) {
                continue
            }
            $probeOutput = $stdoutTask.Result.Trim()
            if ($process.ExitCode -eq 0 -and $probeOutput -eq 'user-guard-python-ok') {
                return $commandInfo.Source
            }
        } catch [System.ComponentModel.Win32Exception] {
            continue
        } catch [System.InvalidOperationException] {
            continue
        } catch [System.AggregateException] {
            continue
        } finally {
            $process.Dispose()
        }
    }
}

$python = Find-WorkingPython "python.exe"
$launcher = if (-not $python) { Find-WorkingPython "py.exe" @("-3") }
if (-not $python -and -not $launcher) {
    Write-Error "[!] 错误：未在 PATH 中找到可执行的 Python 3.11+。" -ErrorAction Continue
    exit 1
}

function Quote-WindowsArg {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

# 直接继承控制台标准流启动 Python：既保留无换行交互提示即时输出，又避免 PowerShell 5.1 将原生 stderr 误判为终止异常。
$exePath = if ($python) { $python } else { $launcher }
$fullArgs = @(if (-not $python) { '-3' }) + @('-B', '-X', 'utf8', "$PSScriptRoot\install.py") + @($args)
$installerExitCode = 1
$proc = [System.Diagnostics.Process]::new()
try {
    $proc.StartInfo.FileName = $exePath
    $proc.StartInfo.Arguments = ($fullArgs | ForEach-Object { Quote-WindowsArg ([string]$_) }) -join ' '
    $proc.StartInfo.UseShellExecute = $false
    $null = $proc.Start()
    $proc.WaitForExit()
    $installerExitCode = $proc.ExitCode
} catch [System.ComponentModel.Win32Exception] {
    Write-Error "[!] 无法启动 Python 安装器。$($_.Exception.Message)" -ErrorAction Continue
    $installerExitCode = 1
} catch [System.InvalidOperationException] {
    Write-Error "[!] 无法启动 Python 安装器。$($_.Exception.Message)" -ErrorAction Continue
    $installerExitCode = 1
} finally {
    $proc.Dispose()
}
exit $installerExitCode
