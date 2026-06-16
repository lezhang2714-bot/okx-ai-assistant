param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$directory = Split-Path -Parent $OutputPath
if ($directory -and -not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$bitmap = New-Object System.Drawing.Bitmap 64, 64
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(15, 23, 42))

$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(56, 189, 248))
$graphics.FillEllipse($brush, 8, 8, 48, 48)

$pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(226, 232, 240), 4)
$graphics.DrawLine($pen, 32, 18, 32, 34)
$graphics.DrawArc($pen, 18, 24, 28, 28, 130, 220)

$iconHandle = $bitmap.GetHicon()
$icon = [System.Drawing.Icon]::FromHandle($iconHandle)
$fileStream = [System.IO.File]::Create($OutputPath)
try {
    $icon.Save($fileStream)
}
finally {
    $fileStream.Close()
    $icon.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
}

Write-Output $OutputPath
