[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$InnoCompiler = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildRoot = Join-Path $root "build\windows"
$distRoot = Join-Path $root "dist\windows"
$spec = Join-Path $PSScriptRoot "qqchatlog.spec"
$installerScript = Join-Path $PSScriptRoot "installer.iss"

if ([string]::IsNullOrWhiteSpace($Python)) {
    if (-not [string]::IsNullOrWhiteSpace($env:PYTHON)) {
        $Python = $env:PYTHON
    } else {
        $Python = "python"
    }
}

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "找不到 Python。请安装 Python 3.10+，或使用 -Python 指定 python.exe。"
}

$projectText = Get-Content -Raw (Join-Path $root "pyproject.toml")
$versionMatch = [regex]::Match($projectText, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "无法从 pyproject.toml 读取项目版本。"
}
$version = $versionMatch.Groups[1].Value

Write-Host "构建 QQChatLog Windows 包，版本 $version"

if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $distRoot) {
    Remove-Item -LiteralPath $distRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $buildRoot, $distRoot | Out-Null

& $Python -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "运行期依赖安装失败。"
}
& $Python -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "构建依赖安装失败。"
}
& $Python -m pip install --no-deps $root
if ($LASTEXITCODE -ne 0) {
    throw "项目安装失败，无法收集版本元数据。"
}

& $Python -m PyInstaller --noconfirm --clean --workpath (Join-Path $buildRoot "pyinstaller") --distpath $distRoot $spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败。"
}

& $Python (Join-Path $root "tools\verify_windows_package.py") (Join-Path $distRoot "QQChatLog")
if ($LASTEXITCODE -ne 0) {
    throw "冻结目录校验失败。"
}

if ([string]::IsNullOrWhiteSpace($InnoCompiler)) {
    $isccCommand = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($null -ne $isccCommand) {
        $InnoCompiler = $isccCommand.Source
    } else {
        $candidates = @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles} "Inno Setup 6\ISCC.exe")
        )
        $InnoCompiler = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
if ([string]::IsNullOrWhiteSpace($InnoCompiler) -or -not (Test-Path -LiteralPath $InnoCompiler)) {
    throw "找不到 Inno Setup 编译器 ISCC.exe。请安装 Inno Setup 6，或用 -InnoCompiler 指定路径。"
}

& $InnoCompiler "/DMyAppVersion=$version" $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup 构建失败。"
}

$bundle = Join-Path $distRoot "QQChatLog"
$portable = Join-Path $distRoot "QQChatLog-$version-windows-x64-portable.zip"
if (Test-Path -LiteralPath $portable) {
    Remove-Item -LiteralPath $portable -Force
}
Compress-Archive -Path $bundle -DestinationPath $portable -CompressionLevel Optimal

$installer = Join-Path $distRoot "QQChatLog-$version-windows-x64-Setup.exe"
$hashFile = Join-Path $distRoot "SHA256SUMS.txt"
$artifacts = @($installer, $portable)
$hashLines = foreach ($artifact in $artifacts) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($artifact))"
}
Set-Content -LiteralPath $hashFile -Value $hashLines -Encoding UTF8

Write-Host "构建完成："
Write-Host "  $installer"
Write-Host "  $portable"
Write-Host "  $hashFile"
