# Creates "Fast Downloader" shortcuts on the Desktop and in the Start Menu.
# Re-run it if you move this folder or change Python installs.
#   powershell -ExecutionPolicy Bypass -File create_shortcut.ps1

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "fast_downloader.py"
$icon   = Join-Path $here "fast_downloader.ico"

# pythonw.exe runs the app without a console window
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python not found on PATH." }
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in [Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs")) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "Fast Downloader.lnk"))
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = "`"$script`""
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation     = "$icon,0"
    $lnk.Description      = "Segmented multi-route downloader"
    $lnk.Save()
    Write-Output "Created $($lnk.FullName)"
}
