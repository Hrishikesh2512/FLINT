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
    # Gemini API key baked into the appliance - required for the voice assistant.
    [string]$GeminiApiKey,
    # How Venom addresses you.
    [string]$UserName = "Boss",
    # Extra Wi-Fi networks beyond the one Imager configured, e.g. your phone
    # hotspot: -ExtraWifi "MyPhone=hotspotpass","Office=officepass"
    [string[]]$ExtraWifi = @(),
    # Bluetooth headset. When omitted, the script tries to detect the headset
    # currently paired with THIS laptop and bakes that in.
    [string]$BluetoothMac,
    [string]$BluetoothName,
    # Optional laptop brain (additive, never required).
    [string]$LaptopHost,
    [int]$LaptopPort = 8765,
    # Optional cloud memory backup (Supabase). Bake in all three to have Venom
    # mirror its long-term memory to the cloud so a re-flash / lost Pi doesn't
    # lose it. Re-flash a fresh device with the SAME -MemoryPassphrase to
    # restore. Only the PUBLISHABLE (anon) key belongs here.
    [string]$SupabaseUrl,
    [string]$SupabaseAnonKey,
    [string]$MemoryPassphrase,
    # Git branch the Pi will install Venom from.
    [string]$Branch = "v2/rebuild"
)

$ErrorActionPreference = "Stop"
$payloadSrc = $PSScriptRoot

function Find-BluetoothHeadset {
    # Remote Bluetooth devices known to this laptop appear as PnP devices with
    # InstanceId BTHENUM\DEV_<12-hex-MAC>. Prefer ones that look like audio.
    $found = @()
    $devices = Get-PnpDevice -Class Bluetooth -Status OK -ErrorAction SilentlyContinue
    foreach ($dev in $devices) {
        if ($dev.InstanceId -match 'BTHENUM\\DEV_([0-9A-F]{12})') {
            $raw = $Matches[1]
            $mac = ($raw -split '(?<=\G..)(?=.)') -join ':'
            $found += [pscustomobject]@{ Name = $dev.FriendlyName; Mac = $mac }
        }
    }
    if (-not $found) { return $null }
    $found = @($found | Sort-Object Mac -Unique)
    $audioLike = $found | Where-Object {
        $_.Name -match 'head|ear|bud|air|sound|wh-|wf-|jbl|boat|noise|oneplus|realme'
    }
    if (@($audioLike).Count -ge 1) { return @($audioLike)[0] }
    if (@($found).Count -eq 1) { return @($found)[0] }
    Write-Host "Multiple Bluetooth devices known to this laptop:" -ForegroundColor Yellow
    $found | ForEach-Object { Write-Host ("  {0}  {1}" -f $_.Mac, $_.Name) }
    return $null
}

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

# Imager <= 1.x writes firstrun.sh; Imager 2.x writes cloud-init user-data.
$firstrun = "$BootDrive\firstrun.sh"
$userData = "$BootDrive\user-data"
if (Test-Path $firstrun)      { $bootMode = "firstrun" }
elseif (Test-Path $userData)  { $bootMode = "cloud-init" }
else {
    throw ("Neither firstrun.sh nor user-data found on $BootDrive. Re-flash with " +
           "Raspberry Pi Imager and fill in the OS customisation screen " +
           "(hostname, user, Wi-Fi, SSH) - that generates the first-boot hook Venom rides on.")
}

Write-Host "Boot partition : $BootDrive  (first-boot mechanism: $bootMode)"

# -- 1. copy the payload -------------------------------------------------------
$dest = "$BootDrive\venom"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in "install-firstboot.sh", "provision.sh", "venom.service", "venom-provision.service", "venom.toml") {
    Copy-Item -Force (Join-Path $payloadSrc $f) (Join-Path $dest $f)
}
Write-Host "Payload copied : $dest"

# -- 2. personalise venom.toml ------------------------------------------------
$toml = Get-Content (Join-Path $dest "venom.toml") -Raw
if ($GeminiApiKey) {
    $toml = $toml -replace 'api_key = ""', ('api_key = "' + $GeminiApiKey + '"')
    Write-Host "Gemini key     : baked in (voice assistant enabled)"
} else {
    Write-Host "Gemini key     : NOT set - Venom will boot but stay silent." -ForegroundColor Yellow
    Write-Host "                 Re-run with -GeminiApiKey <key> for the voice assistant."
}
if ($UserName -and $UserName -ne "Boss") {
    $toml = $toml -replace 'user_name = "Boss"', ('user_name = "' + $UserName + '"')
    Write-Host "User name      : $UserName"
}
if ($LaptopHost) {
    $toml = $toml -replace 'host = "192\.168\.1\.50"', ('host = "' + $LaptopHost + '"')
    $toml = $toml -replace 'port = 8765', ('port = ' + $LaptopPort)
    Write-Host "Laptop brain   : ${LaptopHost}:${LaptopPort} (optional additive)"
}

# Bluetooth headset: explicit params win; otherwise detect from this laptop.
if (-not $BluetoothMac -and -not $BluetoothName) {
    $detected = Find-BluetoothHeadset
    if ($detected) {
        $BluetoothMac  = $detected.Mac
        $BluetoothName = $detected.Name
        Write-Host ("BT headset     : detected '" + $detected.Name + "' (" + $detected.Mac + ")")
    }
}
if ($BluetoothMac)  { $toml = $toml -replace 'bluetooth_mac = ""',  ('bluetooth_mac = "' + $BluetoothMac + '"') }
if ($BluetoothName) { $toml = $toml -replace 'bluetooth_name = ""', ('bluetooth_name = "' + $BluetoothName + '"') }
if ($BluetoothMac -or $BluetoothName) {
    Write-Host "BT headset     : baked in - put it in PAIRING MODE near the Pi on first boot (once)"
} else {
    Write-Host "BT headset     : none configured; Venom will use a USB headset if present" -ForegroundColor Yellow
}

# Cloud memory backup: all three needed, else the feature stays off.
if ($SupabaseUrl -and $SupabaseAnonKey -and $MemoryPassphrase) {
    $toml = $toml -replace 'supabase_url = ""',      ('supabase_url = "' + $SupabaseUrl + '"')
    $toml = $toml -replace 'supabase_anon_key = ""', ('supabase_anon_key = "' + $SupabaseAnonKey + '"')
    $toml = $toml -replace 'memory_passphrase = ""', ('memory_passphrase = "' + $MemoryPassphrase + '"')
    Write-Host "Cloud memory   : backup ENABLED - re-flash with the same -MemoryPassphrase to restore"
} elseif ($SupabaseUrl -or $SupabaseAnonKey -or $MemoryPassphrase) {
    Write-Host "Cloud memory   : incomplete - need -SupabaseUrl, -SupabaseAnonKey AND -MemoryPassphrase. Left OFF." -ForegroundColor Yellow
} else {
    Write-Host "Cloud memory   : local-only (no cloud backup configured)"
}
# Shell scripts on the Pi read this file - write it with Unix endings, no BOM.
[IO.File]::WriteAllText((Join-Path $dest "venom.toml"), ($toml -replace "`r`n", "`n"))

# -- 2b. extra Wi-Fi networks (phone hotspot etc.) ------------------------------
if ($ExtraWifi.Count -gt 0) {
    $parsed = New-Object System.Collections.ArrayList
    foreach ($entry in $ExtraWifi) {
        $split = $entry.Split("=", 2)
        if ($split.Count -ne 2 -or -not $split[0] -or -not $split[1]) {
            throw "ExtraWifi entries must be SSID=password, got: $entry"
        }
        [void]$parsed.Add(@($split[0], $split[1]))
    }
    $netConfig = "$BootDrive\network-config"
    if ($bootMode -eq "cloud-init" -and (Test-Path $netConfig)) {
        # Native path: add access points to Imager's netplan-style file.
        $net = [IO.File]::ReadAllText($netConfig)
        foreach ($pair in $parsed) {
            $ssid = $pair[0]; $pass = $pair[1]
            if ($net -notmatch [regex]::Escape("`"$ssid`":")) {
                $ap = "        `"$ssid`":`n          password: `"$pass`"`n"
                $net = $net -replace "(?m)^(      access-points:\r?\n)", ("`$1" + $ap)
            }
        }
        [IO.File]::WriteAllText($netConfig, ($net -replace "`r`n", "`n"))
        Write-Host ("Extra Wi-Fi    : " + (($parsed | ForEach-Object { $_[0] }) -join ", ") +
                    "  (added to cloud-init network-config)")
    } else {
        # Legacy path: NM keyfiles written by install-firstboot.sh.
        $lines = $parsed | ForEach-Object { $_[0] + "`t" + $_[1] }
        [IO.File]::WriteAllText((Join-Path $dest "extra-wifi.tsv"),
                                (($lines -join "`n") + "`n"))
        Write-Host ("Extra Wi-Fi    : " + (($parsed | ForEach-Object { $_[0] }) -join ", "))
    }
}

# -- 3. normalise payload line endings (FAT copy from Windows may carry CRLF) -
foreach ($f in Get-ChildItem $dest -File) {
    $text = [IO.File]::ReadAllText($f.FullName) -replace "`r`n", "`n"
    [IO.File]::WriteAllText($f.FullName, $text)
}

# -- 4. install the first-boot hook --------------------------------------------
$marker = "# --- venom firstboot hook ---"
if ($bootMode -eq "cloud-init") {
    # cloud-init runcmd runs once, as root, late in first boot (network up),
    # so provisioning can start immediately - no extra reboot needed.
    $ud = [IO.File]::ReadAllText($userData)
    if ($ud.Contains($marker)) {
        Write-Host "Hook installed : already present in user-data, skipped"
    } else {
        if ($ud -match "(?m)^runcmd:") {
            throw "user-data already has a runcmd section - merge manually."
        }
        $hook = "`n$marker`n" +
                "runcmd:`n" +
                "- [bash, -c, `"VENOM_REPO_BRANCH='$Branch' bash /boot/firmware/venom/install-firstboot.sh >> /var/log/venom-firstboot.log 2>&1 || true`"]`n"
        [IO.File]::WriteAllText($userData, (($ud.TrimEnd() + $hook) -replace "`r`n", "`n"))
        Write-Host "Hook installed : cloud-init runcmd -> venom/install-firstboot.sh"
    }
} else {
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
        # Imager's firstrun.sh ends with an 'exit 0' after its cleanup; insert
        # the hook before the LAST one so it always executes.
        $insertAt = $lines.Count
        for ($i = $lines.Count - 1; $i -ge 0; $i--) {
            if ($lines[$i].Trim() -eq "exit 0") { $insertAt = $i; break }
        }
        $lines.InsertRange($insertAt, [string[]]$hookLines)
        [IO.File]::WriteAllText($firstrun, (($lines -join "`n")))
        Write-Host "Hook installed : firstrun.sh chained to venom/install-firstboot.sh"
    }
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
