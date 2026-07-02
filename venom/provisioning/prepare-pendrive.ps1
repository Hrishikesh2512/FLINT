<#
.SYNOPSIS
    Turns a freshly flashed Raspberry Pi OS pendrive into a Venom appliance installer.

.DESCRIPTION
    Run this AFTER flashing Raspberry Pi OS Lite (64-bit) to the pendrive with
    Raspberry Pi Imager, WITH Imager's OS customisation filled in (hostname,
    username/password, Wi-Fi, SSH). Imager's customisation writes a firstrun.sh
    to the pendrive's boot partition; this script:

      1. copies the Venom provisioning payload to <boot>\venom\
      2. chains the Venom firstboot hook onto the end of Imager's firstrun.sh
      3. (optional) writes your laptop's address into the bundled venom.toml

    On the Pi's first boots this installs and starts the Venom appliance
    automatically - no keyboard, monitor, or SD card ever needed.

.EXAMPLE
    .\prepare-pendrive.ps1 -LaptopHost 192.168.1.50

.EXAMPLE
    .\prepare-pendrive.ps1 -BootDrive E: -LaptopHost 100.101.102.103 -Branch v2/rebuild
#>
[CmdletBinding()]
param(
    # Drive letter of the pendrive's boot partition (auto-detected when omitted).
    [string]$BootDrive,
    # Your laptop's LAN or Tailscale IP - written into venom.toml as the preferred brain.
    [string]$LaptopHost,
    # Port of the FLINT brain service on the laptop.
    [int]$LaptopPort = 8765,
    # Git branch the Pi will install Venom from.
    [string]$Branch = "v2/rebuild"
)

$ErrorActionPreference = "Stop"
$payloadSrc = $PSScriptRoot

function Find-BootPartition {
    $candidates = Get-Volume -ErrorAction SilentlyContinue |
        Where-Object { $_.DriveLetter -and (Test-Path "$($_.DriveLetter):\cmdline.txt") -and (Test-Path "$($_.DriveLetter):\config.txt") }
    if (-not $candidates) {
        throw ("No Raspberry Pi boot partition found. Flash Raspberry Pi OS Lite (64-bit) " +
               "with Raspberry Pi Imager first, keep the pendrive plugged in, then re-run. " +
               "Or pass -BootDrive X: explicitly.")
    }
    if (@($candidates).Count -gt 1) {
        throw ("Multiple boot partitions found (" + (($candidates | ForEach-Object { "$($_.DriveLetter):" }) -join ", ") +
               "). Pass -BootDrive to pick one.")
    }
    return "$(@($candidates)[0].DriveLetter):"
}

if (-not $BootDrive) { $BootDrive = Find-BootPartition }
$BootDrive = $BootDrive.TrimEnd('\')
if (-not (Test-Path "$BootDrive\cmdline.txt")) {
    throw "$BootDrive does not look like a Raspberry Pi boot partition (no cmdline.txt)."
}

$firstrun = "$BootDrive\firstrun.sh"
if (-not (Test-Path $firstrun)) {
    throw ("$firstrun not found. Re-flash with Raspberry Pi Imager and fill in the OS " +
           "customisation screen (hostname, user, Wi-Fi, SSH) - that is what generates " +
           "firstrun.sh, which Venom chains onto.")
}

Write-Host "Boot partition : $BootDrive"

# -- 1. copy the payload -------------------------------------------------------
$dest = "$BootDrive\venom"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in "install-firstboot.sh", "provision.sh", "venom.service", "venom-provision.service", "venom.toml") {
    Copy-Item -Force (Join-Path $payloadSrc $f) (Join-Path $dest $f)
}
Write-Host "Payload copied : $dest"

# -- 2. personalise venom.toml ------------------------------------------------
if ($LaptopHost) {
    $toml = Get-Content (Join-Path $dest "venom.toml") -Raw
    $toml = $toml -replace 'host = "192\.168\.1\.50"', ('host = "' + $LaptopHost + '"')
    $toml = $toml -replace 'port = 8765', ('port = ' + $LaptopPort)
    # Shell scripts on the Pi read this file - write it with Unix endings, no BOM.
    [IO.File]::WriteAllText((Join-Path $dest "venom.toml"), ($toml -replace "`r`n", "`n"))
    Write-Host "Laptop brain   : ${LaptopHost}:${LaptopPort}"
} else {
    Write-Host "Laptop brain   : not set (edit /etc/venom/venom.toml on the Pi later)"
}

# -- 3. normalise payload line endings (FAT copy from Windows may carry CRLF) -
foreach ($f in Get-ChildItem $dest -File) {
    $text = [IO.File]::ReadAllText($f.FullName) -replace "`r`n", "`n"
    [IO.File]::WriteAllText($f.FullName, $text)
}

# -- 4. chain onto Imager's firstrun.sh ---------------------------------------
$marker = "# --- venom firstboot hook ---"
$firstrunText = [IO.File]::ReadAllText($firstrun)
if ($firstrunText.Contains($marker)) {
    Write-Host "Hook installed : already present, skipped"
} else {
    $hookLines = @(
        $marker,
        "export VENOM_REPO_BRANCH='$Branch'",
        'BOOTMNT=$(dirname "$(realpath "$0")")',
        'bash "$BOOTMNT/venom/install-firstboot.sh" || echo ''[venom] firstboot hook failed'''
    )
    $lines = [System.Collections.Generic.List[string]]($firstrunText -split "`r?`n")
    # Imager's firstrun.sh ends with an 'exit 0' after its cleanup; insert the
    # hook before the LAST one so it always executes.
    $insertAt = $lines.Count
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        if ($lines[$i].Trim() -eq "exit 0") { $insertAt = $i; break }
    }
    $lines.InsertRange($insertAt, [string[]]$hookLines)
    [IO.File]::WriteAllText($firstrun, (($lines -join "`n")))
    Write-Host "Hook installed : firstrun.sh chained to venom/install-firstboot.sh"
}

Write-Host ""
Write-Host "Done. Safely eject the pendrive, plug it into the Pi 4, and power on."
Write-Host "First boot sequence (allow ~10 minutes with Wi-Fi in range):"
Write-Host "  boot 1  filesystem expands + Imager applies user/Wi-Fi/SSH + Venom hook installs"
Write-Host "  boot 2  venom-provision downloads and installs the appliance, then starts it"
Write-Host "Check from your laptop:   ssh <user>@venom.local   then:"
Write-Host "  systemctl status venom          # daemon state"
Write-Host "  cat /run/venom/status.json      # live appliance status"
Write-Host "  journalctl -u venom-provision   # provisioning log if something failed"
