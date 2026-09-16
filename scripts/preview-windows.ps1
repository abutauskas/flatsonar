# Preview the GTK client on Windows with a simulated flatpak.
#
#   .\scripts\preview-windows.ps1            # start server if needed, launch client
#   .\scripts\preview-windows.ps1 -Reset     # start again from the seeded scenario
#   .\scripts\preview-windows.ps1 -NoServer  # client only (the Installed page still works offline)
#
# Needs MSYS2 UCRT64 with GTK4 / libadwaita / PyGObject / python-httpx / python-yaml:
#   pacman -S mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-libadwaita `
#             mingw-w64-ucrt-x86_64-python-httpx mingw-w64-ucrt-x86_64-python-yaml
# and the server venv from the README (python -m venv .venv; pip install -e core -e server).
#
# Nothing is installed for real: FLATSONAR_FAKE_FLATPAK swaps flatpak for an in-memory
# simulation (client/flatsonar/install/fake_flatpak.py). Its state lives in
# ~\.cache\flatsonar\fake-flatpak.json; edit it, or pass -Reset.
#
# The client is started with Start-Process -NoNewWindow so that its log output (which
# goes to stderr) is printed as-is. Windows PowerShell 5.1 would otherwise turn every
# stderr line of a native program into an error record.

param(
    [switch]$Reset,
    [switch]$NoServer,
    [int]$Port = 8000
)

$root = Split-Path $PSScriptRoot -Parent

$py = $env:MSYS2_PYTHON
if (-not $py) { $py = "C:\msys64\ucrt64\bin\python3.exe" }
if (-not (Test-Path $py)) {
    Write-Host "MSYS2 UCRT64 Python not found at $py. Install MSYS2 or set MSYS2_PYTHON." -ForegroundColor Red
    exit 1
}

function Test-Port([int]$p) {
    return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

if (-not $NoServer) {
    if (Test-Port $Port) {
        Write-Host "Server already listening on port $Port."
    } else {
        $venvPy = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPy)) {
            Write-Host "No server venv at $venvPy. See README: python -m venv .venv; pip install -e core -e server" -ForegroundColor Red
            exit 1
        }
        Write-Host "Starting the Flatsonar server on port $Port (minimised window)..."
        $server = Start-Process -FilePath $venvPy -PassThru -WindowStyle Minimized `
            -ArgumentList "-m", "uvicorn", "flatsonar_server.main:app", "--port", "$Port" `
            -WorkingDirectory (Join-Path $root "server")
        $deadline = (Get-Date).AddSeconds(20)
        while (-not (Test-Port $Port) -and -not $server.HasExited -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
        }
        if ($server.HasExited) {
            Write-Host "The server exited with code $($server.ExitCode). Run it by hand to see why:" -ForegroundColor Red
            Write-Host "  cd server; ..\.venv\Scripts\python.exe -m uvicorn flatsonar_server.main:app --port $Port"
            exit 1
        }
        if (-not (Test-Port $Port)) {
            Write-Host "The server did not start listening on port $Port within 20s; continuing anyway." -ForegroundColor Yellow
        }
    }
}

$env:PYTHONPATH = "$root\core;$root\client"
$env:FLATSONAR_API = "http://localhost:$Port"
$env:FLATSONAR_FAKE_FLATPAK = "1"
if ($Reset) { $env:FLATSONAR_FAKE_FLATPAK = "reset" }

Write-Host "Launching the client with a simulated flatpak (FLATSONAR_FAKE_FLATPAK=$($env:FLATSONAR_FAKE_FLATPAK))."
Write-Host "Press the Installed button (Ctrl+I): Jan updates silently, Cantara warns twice, Stockpile rebuilds."
$client = Start-Process -FilePath $py -ArgumentList "-m", "flatsonar" -NoNewWindow -Wait -PassThru -WorkingDirectory $root
exit $client.ExitCode
