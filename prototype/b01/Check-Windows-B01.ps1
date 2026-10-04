param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedSourceCommit,
    [string]$Branch = 'codex/cross-platform-b01',
    [string]$ExternalResourceRoot
)
$ErrorActionPreference = 'Stop'
# Read-only preflight. Run only after normal Windows login. Never modifies SSH,
# Codex services/startup entries, Python/Conda, repositories or external resources.
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'B01 requires native Windows x64, not WSL or a Mac execution host.'
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$top = (& git -C $root rev-parse --show-toplevel)
if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($top).TrimEnd('\','/') -ine $root.TrimEnd('\','/')) {
    throw 'Confirm the actual Windows repository root first.'
}
$currentBranch = (& git -C $root branch --show-current)
$commit = (& git -C $root rev-parse HEAD)
if ($currentBranch -ne $Branch -or $commit -ne $ExpectedSourceCommit) {
    throw 'Local continuation branch/source identity mismatch. Preserve both workspaces; do not reset.'
}
$origin = (& git -C $root config --get remote.origin.url)
if ($origin -notmatch 'github\.com[:/]DowndraftK/Local-translator(\.git)?/?$') {
    throw 'This is not the confirmed local repository. No remote SHA query is performed.'
}
$documents = @('README.md','runtime/README.md',
    'docs/下一阶段-统一跨平台架构与Windows适配任务书.md',
    'docs/下一阶段-跨平台分批任务安排.md','docs/跨设备接续与Windows测试协作说明.md',
    'docs/跨平台-B01开发记录.md','docs/跨平台-B01架构决策.md','docs/跨平台-共同功能矩阵.md')
$documentHashes = @{}
foreach ($relative in $documents) {
    $path = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Latest handoff file missing: $relative" }
    $null = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    $documentHashes[$relative] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}
$errors = @()
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor
$ram = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$physicalDisks = @(Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,MediaType)
$volumes = @(Get-Volume | Where-Object { $_.DriveLetter } | Select-Object DriveLetter,FileSystem,Size,SizeRemaining)
$adapters = @(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion)
$nvidia = $null
$smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($smi) {
    try {
        $raw = & $smi.Source '--query-gpu=name,memory.total,driver_version' '--format=csv,noheader,nounits'
        if ($LASTEXITCODE -ne 0) { throw 'nvidia-smi query failed' }
        $nvidia = @($raw | ForEach-Object {
            $parts = $_ -split ','
            [ordered]@{ name=$parts[0].Trim(); dedicated_memory_bytes=([int64]$parts[1].Trim())*1MB; driver=$parts[2].Trim() }
        })
    } catch { $errors += $_.Exception.Message }
} else { $errors += 'nvidia-smi unavailable; dedicated VRAM not verified. Win32 AdapterRAM is not used as a substitute.' }
$tools = @{}
foreach ($name in @('git.exe','python.exe','py.exe','cl.exe','cargo.exe','node.exe','nvidia-smi.exe')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    $tools[$name] = if ($found) { $found.Source } else { $null }
}
$resources = [ordered]@{ confirmed_local_root=$ExternalResourceRoot; exists=$null; hashes_verified=$false }
if ($ExternalResourceRoot) { $resources.exists = Test-Path -LiteralPath $ExternalResourceRoot -PathType Container }
$report = [ordered]@{
    schema=1; batch='B01'; checked_at=(Get-Date).ToUniversalTime().ToString('o')
    native_windows=$true; windows_project_root=$root; source_commit=$commit; branch=$currentBranch
    working_tree= @(& git -C $root status --porcelain); task_document_hashes=$documentHashes
    os=[ordered]@{caption=$os.Caption; version=$os.Version; build=$os.BuildNumber; architecture=$os.OSArchitecture}
    cpu=@($cpu | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors); ram_bytes=$ram
    physical_disks=$physicalDisks; volumes=$volumes; display_adapters=$adapters; nvidia=$nvidia
    development_tools=$tools; external_resources=$resources; missing_evidence=$errors
    interactive_desktop=[ordered]@{ session_name=$env:SESSIONNAME; user_interactive=[Environment]::UserInteractive; gui_verified=$false }
    product_checks=[ordered]@{ cuda_verified=$false; gui_verified=$false; installer_verified=$false; inference_run=$false }
    services_or_login_configuration_modified=$false
}
$out = Join-Path $root ('artifacts\b01-windows-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$null = New-Item -ItemType Directory -Path $out
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $out 'preflight-private.json') -Encoding UTF8
Write-Output "Read-only native Windows preflight saved locally: $out"
Write-Output 'Hardware query is not CUDA, GUI or installer acceptance. Validate the preview on the actual logged-in desktop separately.'
