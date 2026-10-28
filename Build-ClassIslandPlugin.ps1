$ErrorActionPreference = 'Stop'
$project = Join-Path $PSScriptRoot 'ClassIslandPlugin\RemoteHelper.ClassIsland.csproj'
& dotnet build $project -c Release -p:GenerateHashSummary=false
if ($LASTEXITCODE -ne 0) { throw 'ClassIsland 2.1 plugin build failed.' }
$dist = Join-Path $PSScriptRoot 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$package = Join-Path $dist 'RemoteHelper.ClassIsland-1.0.0.cipx'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ClassIslandPlugin\cipx\RemoteHelper.ClassIsland.cipx') -Destination $package -Force
Get-FileHash -Algorithm SHA256 -LiteralPath $package
Write-Host "Install in ClassIsland 2.1: $package"
