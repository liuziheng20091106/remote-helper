$ErrorActionPreference = 'Stop'
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
        $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -Wait -PassThru
        exit $elevated.ExitCode
    }
    Set-Location -LiteralPath $PSScriptRoot
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv (Join-Path $PSScriptRoot '.venv')
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv (Join-Path $PSScriptRoot '.venv')
        } else {
            throw 'Python 3.12 or later is required. Install Python with its launcher first.'
        }
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create .venv.' }
    }
    $requirements = Join-Path $PSScriptRoot 'requirements.txt'
    $stamp = Join-Path $PSScriptRoot '.venv\requirements.sha256'
    $digest = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $installed = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { '' }
    if ($installed -ne $digest) {
        & $python -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r $requirements
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
        Set-Content -LiteralPath $stamp -Value $digest -Encoding ASCII
    }
    $pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    $main = Join-Path $PSScriptRoot 'main.py'
    Start-Process -FilePath $pythonw -ArgumentList ('"{0}"' -f $main) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    exit 0
} catch {
    Write-Host $_ -ForegroundColor Red
    $_ | Out-String | Add-Content -LiteralPath (Join-Path $PSScriptRoot 'startup-error.log') -Encoding UTF8
    exit 1
}
