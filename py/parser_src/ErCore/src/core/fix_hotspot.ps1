# fix_hotspot.ps1 - er local hotspot preparation (run as Administrator).
# ADAPTIVE network stack:
#   - probe Windows ICS (the native mobile-hotspot engine) first. Healthy ICS
#     (SoftAP got 192.168.137.1 AND a UDP:67 DHCP listener is up) -> use the
#     native stack untouched: we only add the radar firewall rule. This is the
#     normal-device path; nothing is reconfigured.
#   - broken ICS (e.g. this dev machine: COM 0x80040201, no UDP:67, no SoftAP
#     IP; even "Network Reset" cannot fix it) -> build the NAT path ourselves,
#     independent of ICS: manual 192.168.137.1/24 + IP forwarding + WinNAT +
#     bundled dhcp_server.py (own window).
# Steps:
#   1) kill stale er local processes (capture/radar/dhcp) - two-phase:
#      old orchestrators first (they respawn workers every 5s; killing workers
#      first would let them come back behind our back), then the workers.
#      $KeepPid = the ER.exe / python orchestrator that launched this
#      script - NEVER kill it (that was the 0906 flash-exit: the filter
#      matched our own parent and the whole app died with the console).
#   2) best-effort: turn the mobile hotspot ON via WinRT (no-op if already on;
#      must not stop/reconfigure a running hotspot)
#   3) wait for the Wi-Fi Direct (SoftAP) adapter to come Up
#   4) ICS health probe: up to 30s for 192.168.137.1 AND UDP:67
#   5) fallback only: SoftAP IP, forwarding, WinNAT, firewall 67+8888, dhcp
#      (skipped with -NoDhcp: the ER.exe orchestrator spawns its own
#      bundled dhcp worker, no system python needed)
#      ICS path: firewall 8888 only
# Idempotent; safe to run before every session.

param([string]$ModeFile = "", [int]$KeepPid = 0, [switch]$NoDhcp)

$log = "C:\Temp\er_fix_hotspot.log"
# C:\Temp is NOT guaranteed to exist on a fresh machine - create it (we run
# elevated, so C:\ is writable); if that somehow fails, fall back to %TEMP%.
try {
  if (-not (Test-Path "C:\Temp")) { New-Item -ItemType Directory -Path "C:\Temp" -Force | Out-Null }
} catch {
  $log = Join-Path $env:TEMP "er_fix_hotspot.log"
}
function Log($m) { $m | Out-File $log -Append -Encoding utf8 }
Set-Content -Path $log -Value ("fix_hotspot " + (Get-Date)) -Encoding utf8

# 1) stale processes (python sources AND frozen ER.exe workers/parents)
# \b anchors keep short names like "er_app" from matching unrelated commands
# (e.g. user_app.py), and "ER.exe" from matching "Server.exe".
$pat = "nic_capture|\ber_radar\b|dhcp_server|\ber_app\b|\bER\.exe"
$cands = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ER.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match $pat -and $_.ProcessId -ne $KeepPid -and $_.ProcessId -ne $PID }
# phase A: orchestrators (ER.exe without --worker args) - they restart
# their workers on a 5s timer and must go down first
foreach ($p in ($cands | Where-Object { $_.Name -eq "ER.exe" -and $_.CommandLine -notmatch "--worker" })) {
  Log ("kill stale orchestrator " + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1
# phase B: everything left (workers, python.bat-mode scripts, strays)
foreach ($p in $cands) {
  Log ("kill stale " + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 2) best-effort hotspot ON (WinRT; profile pick + async wait as proven in _hotspot_gw)
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
  # 上网出口可能是 Wi-Fi 也可能是以太网，且配置名不一定含 WLAN/Wi-Fi（如
  # "TP-LINK_FCD6"）。只按名字匹配会让自动开热点在正常机器上被误跳过 ——
  # 挑不到就退回系统认定的当前上网配置，再退回任一有网卡的配置。
  $prof = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles() |
    Where-Object { $null -ne $_.NetworkAdapter -and $_.ProfileName -match 'WLAN|Wi-Fi' } | Select-Object -First 1
  if ($null -eq $prof) {
    $prof = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  }
  if ($null -eq $prof) {
    $prof = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles() |
      Where-Object { $null -ne $_.NetworkAdapter } | Select-Object -First 1
  }
  if ($null -eq $prof) { throw "no usable connection profile" }
  $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($prof)
  if ($mgr.TetheringOperationalState -ne "On") {
    Log "hotspot is off -> starting via WinRT"
    function AwaitOp($op) {
      # PS wraps WinRT async objects as System.__ComObject; resolve AsTask by
      # reflected interface, fall back to polling IAsyncInfo.Status.
      $ext = [System.WindowsRuntimeSystemExtensions]
      $tname = $op.GetType().GetInterfaces() |
        Where-Object { $_.Name -like 'IAsyncOperation`1' } | Select-Object -First 1
      if ($tname) {
        $m = $ext.GetMethods() | Where-Object {
          $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
          $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
        $m = $m.MakeGenericMethod($tname.GetGenericArguments())
      } else {
        $m = $ext.GetMethods() | Where-Object {
          $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
          $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' -and
          -not $_.IsGenericMethod } | Select-Object -First 1
      }
      try {
        $task = $m.Invoke($null, @($op))
        [void]$task.Wait(30000)
        return $task.Result
      } catch {
        for ($i = 0; $i -lt 60; $i++) {
          try { $s = $op.Status } catch { break }
          if ($s -ne 0) { break }   # 0 = Started; anything else is terminal
          Start-Sleep -Milliseconds 500
        }
        return $null
      }
    }
    [void](AwaitOp $mgr.StartTetheringAsync())
    Start-Sleep -Seconds 2
  }
  Log ("tethering state: " + $mgr.TetheringOperationalState)
} catch {
  Log ("hotspot autostart skipped: " + $_.Exception.Message + " (turn mobile hotspot on manually)")
}

# 3) wait for the SoftAP adapter (hotspot enable can take a few seconds)
$adapter = $null
for ($i = 0; $i -lt 10 -and -not $adapter; $i++) {
  $adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceDescription -match "Wi-Fi Direct" -and $_.Status -eq "Up" } |
    Select-Object -First 1
  if (-not $adapter) { Start-Sleep -Seconds 2 }
}
if (-not $adapter) {
  Log "ERROR: no active Wi-Fi Direct adapter (hotspot off?). Turn mobile hotspot on and rerun."
  exit 1
}
$idx = $adapter.ifIndex
Log ("SoftAP up ifIndex=" + $idx)

# 4) ICS health probe: 192.168.137.1 on the SoftAP AND a UDP:67 listener.
#    (stale er dhcp was killed in step 1, so :67 can only be native ICS)
$mode = "FALLBACK"
for ($i = 0; $i -lt 15; $i++) {
  $ipok = Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq "192.168.137.1" }
  $dhcpok = Get-NetUDPEndpoint -LocalPort 67 -ErrorAction SilentlyContinue
  if ($ipok -and $dhcpok) { $mode = "ICS"; break }
  Start-Sleep -Seconds 2
}
if ($mode -eq "ICS") {
  Log "mode=ICS: native hotspot stack healthy - using it untouched"
} else {
  Log "mode=FALLBACK: ICS broken/absent - building DHCP+NAT ourselves"
}
if ($ModeFile) {
  try { Set-Content -Path $ModeFile -Value $mode -Encoding ascii } catch { Log ("modewrite failed: " + $_.Exception.Message) }
}

if ($mode -ne "ICS") {
  # 5a) SoftAP IP
  $has = Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq "192.168.137.1" }
  if (-not $has) {
    New-NetIPAddress -InterfaceIndex $idx -IPAddress "192.168.137.1" -PrefixLength 24 -ErrorAction Continue | Out-Null
    Log ("assigned 192.168.137.1/24 to ifIndex=" + $idx)
  } else { Log "SoftAP already has 192.168.137.1" }
  Set-NetIPInterface -InterfaceIndex $idx -Forwarding Enabled
  Log ("SoftAP forwarding=Enabled ifIndex=" + $idx)

  # 5b) uplink forwarding
  $uplink = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
    Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1).InterfaceIndex
  if ($uplink) {
    Set-NetIPInterface -InterfaceIndex $uplink -Forwarding Enabled -ErrorAction SilentlyContinue
    Log ("uplink forwarding=Enabled ifIndex=" + $uplink)
  }

  # 5c) WinNAT (client SKUs allow only ONE nat object; a foreign prefix would break the phone)
  $nat = Get-NetNat -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $nat) {
    New-NetNat -Name DFMHotspot -InternalIPInterfaceAddressPrefix "192.168.137.0/24" | Out-Null
    Log "WinNAT DFMHotspot created"
  } elseif ("$($nat.InternalIPInterfaceAddressPrefix)".StartsWith("192.168.137.")) {
    Log ("nat exists: " + $nat.Name)
  } else {
    Log ("WARNING nat '" + $nat.Name + "' prefix=" + $nat.InternalIPInterfaceAddressPrefix + " (not ours)")
    Log ("WARNING phone NAT may fail. If phone has no internet, run:")
    Log ("  Remove-NetNat -Name " + $nat.Name + " -Confirm:$false ; then re-run this script.")
  }

  # 5d) firewall
  New-NetFirewallRule -DisplayName "er-dhcp-67" -Direction Inbound -Protocol UDP -LocalPort 67 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
  New-NetFirewallRule -DisplayName "er-radar-8888" -Direction Inbound -Protocol TCP -LocalPort 8888 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
  Log "firewall rules ok"

  # 5e) bundled DHCP in its own window (--wait tolerates slow SoftAP bring-up).
  #     -NoDhcp (ER.exe orchestrator): it spawns its own dhcp worker,
  #     and a bare "python" may not even exist on an exe-only machine.
  if (-not $NoDhcp) {
    $root = Split-Path -Parent $PSScriptRoot
    Start-Process -FilePath "cmd.exe" `
      -ArgumentList "/k", "title er-dhcp && python src\core\dhcp_server.py --wait 120" `
      -WorkingDirectory $root
    Log "dhcp_server window spawned (fallback mode)"
  } else {
    Log "dhcp spawn skipped (-NoDhcp): orchestrator manages its own dhcp worker"
  }
} else {
  New-NetFirewallRule -DisplayName "er-radar-8888" -Direction Inbound -Protocol TCP -LocalPort 8888 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
  Log "firewall rule ok (radar port only)"
}
Log "DONE"
