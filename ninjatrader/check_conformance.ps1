# check_conformance.ps1 — the gate between the Python oracle and the C# port.
#
# Compiles AofCore.cs + AofGoldenRunner.cs with the .NET Framework compiler
# that ships with every Windows install (no Visual Studio needed), runs the
# C# engine over the same bars the Python goldens were generated from, and
# diffs the outputs. The port may not be trusted on a chart until this
# prints CONFORMANT.
#
# Usage (from the repo root, after generating Python goldens):
#   python golden_vectors.py generate bars.csv out\goldens
#   powershell -ExecutionPolicy Bypass -File ninjatrader\check_conformance.ps1 `
#       -Bars bars.csv -Golden out\goldens\states_es.csv
#
# Optional: -Zone grid  -Spec ES

param(
    [Parameter(Mandatory = $true)][string]$Bars,
    [Parameter(Mandatory = $true)][string]$Golden,
    [string]$Zone = "off",
    [string]$Spec = "MES"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (-not (Test-Path $csc)) {
    Write-Error ".NET Framework csc.exe not found — install .NET Framework 4.8"
}

$outDir = Join-Path $here "bin"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$exe = Join-Path $outDir "AofGoldenRunner.exe"

Write-Host "compiling C# engine..."
& $csc /nologo /optimize+ /out:$exe `
    (Join-Path $here "AofCore.cs") (Join-Path $here "AofGoldenRunner.cs")
if ($LASTEXITCODE -ne 0) { Write-Error "compile failed" }

$candidate = Join-Path $outDir "states_csharp.csv"
Write-Host "running C# engine over $Bars ..."
& $exe $Bars $candidate $Zone $Spec
if ($LASTEXITCODE -ne 0) { Write-Error "runner failed" }

Write-Host "comparing against Python goldens..."
python (Join-Path (Split-Path -Parent $here) "golden_vectors.py") `
    compare $Golden $candidate
exit $LASTEXITCODE
