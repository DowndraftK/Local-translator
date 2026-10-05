param(
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [Parameter(Mandatory=$true)][string]$PythonExe,
 [Parameter(Mandatory=$true)][string]$EvidenceRoot
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
if ($env:OS -ne 'Windows_NT') {throw 'Native Windows required'}
$root=(Get-Item -LiteralPath $ProjectRoot).FullName
$python=(Get-Item -LiteralPath $PythonExe).FullName
$evidence=[IO.Path]::GetFullPath($EvidenceRoot)
$allowed=Join-Path $root 'artifacts'
if (-not $evidence.StartsWith($allowed + '\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Use an evidence directory under this confirmed repository artifacts root'}
if (-not $python.StartsWith($allowed + '\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Use the controlled project-local interpreter'}
if (Test-Path -LiteralPath $evidence) {throw 'Use a new evidence directory'}
New-Item -ItemType Directory -Path $evidence | Out-Null
$fixture=Join-Path $evidence 'junction-fixture'
$external=Join-Path $fixture 'external'
$junction=Join-Path $fixture 'junction'
New-Item -ItemType Directory -Path $external -Force | Out-Null
$oldFixture=$env:B02_NATIVE_JUNCTION_FIXTURE
$oldLocation=Get-Location
try {
 New-Item -ItemType Junction -Path $junction -Target $external | Out-Null
 $env:B02_NATIVE_JUNCTION_FIXTURE=$junction
 Set-Location -LiteralPath $root
 $check=Start-Process -FilePath $python -ArgumentList @('-I','-B','-X','utf8','-m','unittest','discover','-s','prototype\b01\tests','-v') -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $evidence 'shared-tests.stdout.log') -RedirectStandardError (Join-Path $evidence 'shared-tests.log')
 Get-Content -Encoding UTF8 -Tail 9 -LiteralPath (Join-Path $evidence 'shared-tests.log')
 if ($check.ExitCode -ne 0) {throw 'Native shared checks failed; preserve detailed evidence'}
} finally {
 $env:B02_NATIVE_JUNCTION_FIXTURE=$oldFixture
 Set-Location -LiteralPath $oldLocation
 if (Test-Path -LiteralPath $junction) {[IO.Directory]::Delete($junction)}
 # Only this wrapper-owned empty fixture is removed. Keep the test logs.
 [IO.Directory]::Delete($external)
 [IO.Directory]::Delete($fixture)
}
