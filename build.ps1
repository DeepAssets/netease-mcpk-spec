param([string]$PythonPath = 'python')
$ErrorActionPreference = 'Stop'
$toolRoot = $PSScriptRoot
& $PythonPath -m PyInstaller --noconfirm --onefile --windowed --noupx `
    --name 'nmbb_MCPK解包封包工具' `
    --distpath (Join-Path $toolRoot 'dist') `
    --workpath (Join-Path $toolRoot 'build') `
    --specpath $toolRoot `
    --version-file (Join-Path $toolRoot 'version_info.txt') `
    --add-data ((Join-Path $toolRoot 'RUNTIME_NOTICES.txt') + ':licenses') `
    (Join-Path $toolRoot 'main.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
