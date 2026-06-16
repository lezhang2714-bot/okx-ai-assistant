param(
    [Parameter(Mandatory = $true)]
    [string]$PngPath,
    [Parameter(Mandatory = $true)]
    [string]$IcoPath
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

if (-not (Test-Path -LiteralPath $PngPath)) {
    throw "Source PNG not found: $PngPath"
}

$directory = Split-Path -Parent $IcoPath
if ($directory -and -not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

function New-SquareBitmap {
    param(
        [System.Drawing.Image]$Source,
        [int]$Size
    )

    $bitmap = New-Object System.Drawing.Bitmap $Size, $Size
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $graphics.DrawImage($Source, 0, 0, $Size, $Size)
    $graphics.Dispose()
    return $bitmap
}

$source = [System.Drawing.Image]::FromFile($PngPath)
try {
    $sizes = @(16, 32, 48, 64, 128, 256)
    $bitmaps = @()
    foreach ($size in $sizes) {
        $bitmaps += New-SquareBitmap -Source $source -Size $size
    }

    $iconHandle = $bitmaps[$bitmaps.Count - 1].GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($iconHandle)
    $stream = [System.IO.File]::Create($IcoPath)
    try {
        $icon.Save($stream)
    }
    finally {
        $stream.Close()
        $icon.Dispose()
    }

    foreach ($bitmap in $bitmaps) {
        $bitmap.Dispose()
    }
}
finally {
    $source.Dispose()
}

Write-Output $IcoPath
