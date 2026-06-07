#requires -Version 5.1
<#
One-click launcher for FewFeed and Reply Bot.
- Reads settings from bot-launcher.config.json (same folder as this script)
- Launches FewFeed, sends option and accounts
- Optionally creates/switches to a new Virtual Desktop
- Launches ReplyBot, runs option sequence and sends accounts for each step

Run via:
  powershell -ExecutionPolicy Bypass -File "${PSScriptRoot}\start-bots.ps1"
Create a desktop shortcut if desired.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-Config {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { throw "Config file not found: $Path" }
  (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
}

function Start-FewFeedInstance {
  param(
    [Parameter(Mandatory)] [string] $ExePath,
    [Parameter(Mandatory)] [string] $MenuOption,
    [Parameter(Mandatory)] [string] $AccountsCsv,
    [int] $PostWaitSeconds = 10,
    [switch] $CaptureOutput
  )
  $proc = Start-ConsoleApp -ExePath $ExePath -CaptureOutput:$CaptureOutput
  Send-Line -Process $proc -Text $MenuOption -DelayMs 800
  Send-Line -Process $proc -Text $AccountsCsv -DelayMs 2000
  if ($PostWaitSeconds -gt 0) { Start-Sleep -Seconds $PostWaitSeconds }
  return $proc
}

function Start-ConsoleApp {
  param(
    [Parameter(Mandatory)] [string] $ExePath,
    [string] $WorkingDirectory,
    [switch] $CaptureOutput
  )
  # Expand environment variables and support relative paths from script directory
  $expanded = [System.Environment]::ExpandEnvironmentVariables($ExePath)
  # Normalize forward slashes to backslashes for Windows APIs
  $expanded = $expanded -replace '/', '\\'
  if (-not [System.IO.Path]::IsPathRooted($expanded)) {
    if ($scriptDir) {
      $expanded = Join-Path -Path $scriptDir -ChildPath $expanded
    }
  }
  if (-not (Test-Path -LiteralPath $expanded)) {
    throw "Executable not found: $expanded (from '$ExePath')"
  }
  $ExePath = $expanded
  if (-not $WorkingDirectory) {
    $WorkingDirectory = Split-Path -Path $ExePath -Parent
  }
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  if ($ExePath -like "*.py") {
    $psi.FileName = "python"
    $psi.Arguments = "`"$ExePath`""
  } else {
    $psi.FileName = $ExePath
  }
  $psi.WorkingDirectory = $WorkingDirectory
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = [bool]$CaptureOutput
  $psi.RedirectStandardError = $false
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $false

  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  [void]$p.Start()
  # Give the console a moment to initialize
  Start-Sleep -Milliseconds 800
  return $p
}

function Wait-ChromeProcesses {
  param(
    [Parameter(Mandatory)] [int] $ExpectedCount,
    [int] $TimeoutSeconds = 300,
    [int] $QuietPeriodSeconds = 15
  )
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $procs = @(Get-Process -Name chrome -ErrorAction SilentlyContinue)
  $initialCount = $procs.Length
  Write-Host ("Waiting for {0} new Chrome processes (initial: {1})..." -f $ExpectedCount, $initialCount) -ForegroundColor DarkYellow
  
  $lastCount = $initialCount
  $quietSw = [System.Diagnostics.Stopwatch]::StartNew()
  $minReached = $false
  
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    $currentCount = @(Get-Process -Name chrome -ErrorAction SilentlyContinue).Length
    $delta = $currentCount - $initialCount
    
    # Track changes
    if ($currentCount -ne $lastCount) {
      $quietSw.Restart()
      $lastCount = $currentCount
      Write-Host ("Chrome activity detected: current count {0} (+{1})" -f $currentCount, $delta) -ForegroundColor DarkGray
    }
    
    if (-not $minReached -and $delta -ge $ExpectedCount) {
      $minReached = $true
      Write-Host ("Target reached (+{0}). Now waiting {1}s for stability (no more changes)..." -f $delta, $QuietPeriodSeconds) -ForegroundColor DarkYellow
      $quietSw.Restart()
    }
    
    if ($minReached -and $quietSw.Elapsed.TotalSeconds -ge $QuietPeriodSeconds) {
      Write-Host ("Chrome processes stabilized at {0} total (+{1})." -f $currentCount, $delta) -ForegroundColor Green
      return $true
    }
    
    Start-Sleep -Seconds 1
  }
  
  Write-Host "Wait-ChromeProcesses timeout reached." -ForegroundColor Yellow
  return $minReached
}

function Send-Line {
  param(
    [Parameter(Mandatory)] [System.Diagnostics.Process] $Process,
    [Parameter(Mandatory)] [string] $Text,
    [int] $DelayMs = 500
  )
  if ($Process.HasExited) { throw "Process has already exited: $($Process.StartInfo.FileName)" }
  $Process.StandardInput.WriteLine($Text)
  if ($DelayMs -gt 0) { Start-Sleep -Milliseconds $DelayMs }
}

function Get-RotatedAccountsForOption {
  param(
    [Parameter(Mandatory)] [object] $CommentsReplyBotConfig,
    [Parameter(Mandatory)] [string] $Option
  )
  # Returns a CSV string of accounts based on daily rotation settings, or $null if rotation is not enabled/possible.
  try {
    # Check RotationEnabled toggle (new simplified approach)
    $rotationEnabled = $false
    if ($CommentsReplyBotConfig.PSObject.Properties.Name -contains 'RotationEnabled') {
      $rotationEnabled = [bool]$CommentsReplyBotConfig.RotationEnabled
    }
    # Fallback to old RotateAccountsDaily for backward compatibility
    elseif ($CommentsReplyBotConfig.PSObject.Properties.Name -contains 'RotateAccountsDaily') {
      $rotationEnabled = [bool]$CommentsReplyBotConfig.RotateAccountsDaily
    }
    else { return $null }
    
    if (-not $rotationEnabled) { return $null }
    
    # Get settings from RotationPreset if available
    $preset = $null
    if ($CommentsReplyBotConfig.PSObject.Properties.Name -contains 'RotationPreset') {
      $preset = $CommentsReplyBotConfig.RotationPreset
    }
    
    # Get AccountPool (from preset or old location)
    $poolRaw = ''
    if ($preset -and $preset.PSObject.Properties.Name -contains 'AccountPool') {
      $poolRaw = [string]$preset.AccountPool
    }
    elseif ($CommentsReplyBotConfig.PSObject.Properties.Name -contains 'AccountPool') {
      $poolRaw = [string]$CommentsReplyBotConfig.AccountPool
    }
    if (-not $poolRaw) { return $null }
    
    $pool = $poolRaw.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
    if ($pool.Count -eq 0) { return $null }

    # Determine batch size for this option (default 1)
    $batchSize = 1
    $batchPropName = ("BatchSizeForOption{0}" -f $Option)
    
    if ($preset -and $preset.PSObject.Properties.Name -contains $batchPropName) {
      $tryVal = 1
      try { $tryVal = [int]($preset.$batchPropName) } catch {}
      if ($tryVal -gt 0) { $batchSize = $tryVal }
    }
    elseif ($CommentsReplyBotConfig.PSObject.Properties.Name -contains $batchPropName) {
      $tryVal = 1
      try { $tryVal = [int]($CommentsReplyBotConfig.$batchPropName) } catch {}
      if ($tryVal -gt 0) { $batchSize = $tryVal }
    }

    # Determine rotation anchor: DayOfYear (default) or DayOfWeek
    $rotationMode = 'DayOfYear'
    if ($preset -and $preset.PSObject.Properties.Name -contains 'RotationMode') {
      $rotationMode = [string]$preset.RotationMode
    }
    elseif ($CommentsReplyBotConfig.PSObject.Properties.Name -contains 'RotationMode') {
      $rotationMode = [string]$CommentsReplyBotConfig.RotationMode
    }
    
    $now = Get-Date
    $startIndex = 0
    if ($rotationMode -eq 'DayOfWeek') {
      # Monday=1..Sunday=7 in .NET; map Monday->0 ... Sunday->6
      $dow = [int]$now.DayOfWeek  # Sunday=0..Saturday=6
      # Convert to Monday=0..Sunday=6
      $mondayZero = ($dow - 1)
      if ($mondayZero -lt 0) { $mondayZero = 6 }
      $startIndex = $mondayZero % $pool.Count
    } else {
      $dayIndex = $now.DayOfYear
      $startIndex = $dayIndex % $pool.Count
    }

    $selection = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $batchSize; $i++) {
      $idx = ($startIndex + $i) % $pool.Count
      $selection.Add($pool[$idx]) | Out-Null
    }
    return ($selection -join ',')
  } catch {
    return $null
  }
}

function Ensure-VirtualDesktopModule {
  param(
    [bool] $AutoInstall = $true
  )
  try {
    if (Get-Module -ListAvailable -Name VirtualDesktop) {
      Import-Module VirtualDesktop -ErrorAction Stop
      return $true
    }
    if ($AutoInstall) {
      Write-Host 'VirtualDesktop module not found. Attempting installation from PSGallery...' -ForegroundColor DarkYellow
      # Suppress errors for repository registration
      try {
        $prev = Get-PSRepository -Name 'PSGallery' -ErrorAction SilentlyContinue
        if (-not $prev) { Register-PSRepository -Default -ErrorAction SilentlyContinue }
      } catch {}
      Install-Module -Name VirtualDesktop -Scope CurrentUser -Force -AllowClobber -ErrorAction Stop
      Import-Module VirtualDesktop -ErrorAction Stop
      Write-Host 'VirtualDesktop module installed and imported.' -ForegroundColor Green
      return $true
    }
  } catch {
    Write-Warning ("Failed to load/install VirtualDesktop module: {0}" -f $_.Exception.Message)
  }
  return $false
}

function Get-DesktopCountSafe {
  try { return [int](Get-DesktopCount) } catch { return $null }
}

function Ensure-DesktopCount {
  param([int]$Count)
  $existing = Get-DesktopCountSafe
  if ($existing -eq $null) { return $false }
  while ($existing -lt $Count) {
    try { New-Desktop | Out-Null } catch {
      Write-Host "Module creation failed, using keystroke fallback (Win+Ctrl+D)..." -ForegroundColor Yellow
      Send-DesktopKeystroke -Action 'Create' -DelayAfterMs 1200
    }
    $existing = Get-DesktopCountSafe
    if ($existing -eq $null) { break }
  }
  return $true
}

function Set-DesktopNameSafe {
  param([int]$Index, [string]$Name)
  try {
    if (Get-Command Set-DesktopName -ErrorAction SilentlyContinue) {
      Set-DesktopName -Index ($Index - 1) -Name $Name
      Write-Host "Desktop $Index identified as: $Name" -ForegroundColor DarkCyan
    }
  } catch {}
}

function Try-SwitchDesktopModule {
  param(
    [int]$Index,
    [switch]$VerboseLog
  )
  $dc = Get-DesktopCountSafe
  if ($dc -eq $null) {
    Write-Debug "Try-SwitchDesktopModule: desktop count is null (module not loaded)"
    return $false
  }
  $zeroBased = [Math]::Max(0, $Index - 1)
  if ($zeroBased -ge $dc) {
    Write-Debug ("Try-SwitchDesktopModule: index {0} >= count {1}" -f $zeroBased, $dc)
    return $false
  }
  try {
    $desktop = Get-Desktop -Index $zeroBased
    if ($desktop) {
      Switch-Desktop -Desktop $desktop
      Write-Debug ("Try-SwitchDesktopModule: switched to desktop index {0} via Get-Desktop/Switch-Desktop" -f $zeroBased)
      return $true
    }
  } catch {
    Write-Debug ("Try-SwitchDesktopModule: Get-Desktop failed: {0}" -f $_.Exception.Message)
  }
  try {
    Switch-Desktop -Index $zeroBased
    Write-Debug ("Try-SwitchDesktopModule: switched via Switch-Desktop -Index {0}" -f $zeroBased)
    return $true
  } catch {
    Write-Debug ("Try-SwitchDesktopModule: Switch-Desktop -Index failed: {0}" -f $_.Exception.Message)
  }
  return $false
}

# Global tracker for desktop index when module is not used
$Global:CurrentDesktopIndex = 1

function Get-CurrentDesktopIndexSafe {
  try {
    if (Get-Command Get-CurrentDesktop -ErrorAction SilentlyContinue) {
      $cd = Get-CurrentDesktop
      if ($cd.PSObject.Properties.Name -contains 'Number') { return $cd.Number + 1 }
      if ($cd.PSObject.Properties.Name -contains 'Index') { return $cd.Index + 1 }
      return [int]$cd + 1
    }
  } catch {}
  return $Global:CurrentDesktopIndex
}

function Move-LauncherToDesktop {
  param([int]$Index)
  try {
    $hwnd = (Get-Process -Id $PID).MainWindowHandle
    if ($hwnd -eq 0) { return $false }
    if (Get-Command Move-WindowToDesktop -ErrorAction SilentlyContinue) {
      $desktop = Get-Desktop -Index ($Index - 1)
      if ($desktop) {
        Move-WindowToDesktop -Window $hwnd -Desktop $desktop
        Write-Host ("Moved launcher window to Desktop {0}." -f $Index) -ForegroundColor DarkCyan
        return $true
      }
    }
  } catch {}
  return $false
}

function Switch-ToDesktopIndex {
  param(
    [int]$Index,
    [string]$Name = $null
  )
  if ($Index -lt 1) { $Index = 1 }
  
  Write-Host ("Switching to Desktop {0}{1}..." -f $Index, $(if ($Name) { " ($Name)" } else { "" })) -ForegroundColor Cyan
  
  # Ensure we have the module loaded if possible
  $moduleLoaded = Ensure-VirtualDesktopModule -AutoInstall:([bool]$config.NewDesktop.AutoInstallIfMissing)

  # Ensure enough desktops exist
  [void](Ensure-DesktopCount -Count $Index)
  if ($Name) { Set-DesktopNameSafe -Index $Index -Name $Name }

  $switched = $false

  # --- Method 1: Module-based switch ---
  if ($moduleLoaded) {
    Move-LauncherToDesktop -Index $Index
    for ($retry = 0; $retry -lt 3; $retry++) {
      if (Try-SwitchDesktopModule -Index $Index) {
        $switched = $true
        break
      }
      Start-Sleep -Milliseconds 500
    }
  }

  # --- Method 2: Keystroke fallback with SendInput ---
  if (-not $switched) {
    $current = Get-CurrentDesktopIndexSafe
    $diff = $Index - $current
    if ($diff -ne 0) {
      $action = if ($diff -gt 0) { 'Right' } else { 'Left' }
      $count = [Math]::Abs($diff)
      Write-Host ("Using keystroke navigation ({0} x {1})" -f $action, $count) -ForegroundColor DarkYellow
      Focus-PowerShellWindow -DelayMs 500
      Start-Sleep -Milliseconds 200
      for ($i = 0; $i -lt $count; $i++) {
        Send-DesktopKeystroke -Action $action -DelayAfterMs 800
      }
    }
    $switched = $true
  }

  # --- Finalize: focus, sleep, and move launcher window if not already moved ---
  Focus-PowerShellWindow -DelayMs 500
  if ($moduleLoaded) { [void](Move-LauncherToDesktop -Index $Index) }

  Start-Sleep -Milliseconds 1500

  $Global:CurrentDesktopIndex = $Index
  return $true
}

function Focus-PowerShellWindow {
  param([int]$DelayMs = 200)
  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue | Out-Null
    $hwnd = (Get-Process -Id $PID).MainWindowHandle
    if ($hwnd -ne 0) {
      $apiSrc = @'
using System; using System.Runtime.InteropServices;
public class WinAPI {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  public const int SW_RESTORE = 9;
}
'@
      if (-not ([System.Management.Automation.PSTypeName]'WinAPI').Type) {
        Add-Type -TypeDefinition $apiSrc -Language CSharp -ErrorAction SilentlyContinue | Out-Null
      }
      if ([WinAPI]::IsIconic($hwnd)) { [void][WinAPI]::ShowWindow($hwnd, [WinAPI]::SW_RESTORE) }
      [void][WinAPI]::SetForegroundWindow($hwnd)
    }
    if ($DelayMs -gt 0) { Start-Sleep -Milliseconds $DelayMs }
  } catch {}
}

function Send-DesktopKeystroke {
  param(
    [Parameter(Mandatory)] [string]$Action,
    [int]$DelayAfterMs = 800
  )
  $kbdSrc = @'
using System; using System.Runtime.InteropServices;
public static class KbdSender {
  [StructLayout(LayoutKind.Sequential)]
  private struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)]
  private struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)]
  private struct HARDWAREINPUT { public uint uMsg; public ushort wParamL; public ushort wParamH; }
  [StructLayout(LayoutKind.Explicit)]
  private struct INPUTUNION {
    [FieldOffset(0)] public MOUSEINPUT mi;
    [FieldOffset(0)] public KEYBDINPUT ki;
    [FieldOffset(0)] public HARDWAREINPUT hi;
  }
  [StructLayout(LayoutKind.Sequential)]
  private struct INPUT {
    public uint type;
    public INPUTUNION u;
  }
  [DllImport("user32.dll", SetLastError = true)]
  private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
  private const uint INPUT_KEYBOARD = 1;
  private const uint KEYEVENTF_KEYUP = 0x0002;
  private static INPUT MakeKey(ushort vk, bool up) {
    INPUT inp = new INPUT();
    inp.type = INPUT_KEYBOARD;
    inp.u.ki.wVk = vk;
    inp.u.ki.dwFlags = up ? KEYEVENTF_KEYUP : 0u;
    return inp;
  }
  public static void PressChord(ushort[] keys) {
    INPUT[] downs = new INPUT[keys.Length];
    for (int i = 0; i < keys.Length; i++) downs[i] = MakeKey(keys[i], false);
    SendInput((uint)downs.Length, downs, System.Runtime.InteropServices.Marshal.SizeOf(typeof(INPUT)));
    System.Threading.Thread.Sleep(30);
    INPUT[] ups = new INPUT[keys.Length];
    for (int i = 0; i < keys.Length; i++) ups[i] = MakeKey(keys[keys.Length - 1 - i], true);
    SendInput((uint)ups.Length, ups, System.Runtime.InteropServices.Marshal.SizeOf(typeof(INPUT)));
  }
}
'@
  if (-not ([System.Management.Automation.PSTypeName]'KbdSender').Type) {
    Add-Type -TypeDefinition $kbdSrc -Language CSharp -ErrorAction SilentlyContinue | Out-Null
  }
  switch ($Action) {
    'Create' { [KbdSender]::PressChord(@(0x5B,0x11,0x44)) }
    'Right'  { [KbdSender]::PressChord(@(0x5B,0x11,0x27)) }
    'Left'   { [KbdSender]::PressChord(@(0x5B,0x11,0x25)) }
  }
  if ($DelayAfterMs -gt 0) { Start-Sleep -Milliseconds $DelayAfterMs }
}

# Main
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $scriptDir 'bot-launcher.config.json'
$config = Read-Config -Path $configPath

# Start Central Telemetry Broker in the background
Write-Host "Starting Central Telemetry Broker..." -ForegroundColor Cyan
try {
  $brokerPy = Join-Path $scriptDir "telemetry_broker.py"
  if (Test-Path -LiteralPath $brokerPy) {
    Start-Process "python" -ArgumentList "`"$brokerPy`" --parent-pid $PID" -WorkingDirectory $scriptDir -WindowStyle Hidden
    Write-Host "Central Telemetry Broker started." -ForegroundColor Green
  } else {
    Write-Warning "telemetry_broker.py not found at $brokerPy"
  }
} catch {
  Write-Warning "Could not start Central Telemetry Broker background process: $_"
}

# Start Web Dashboard in the background (optional — cloud dashboard at Render URL replaces this)
# $dashboardPy = Join-Path $scriptDir "dashboard.py"
# if (Test-Path -LiteralPath $dashboardPy) {
#   try {
#     Start-Process "python" -ArgumentList "`"$dashboardPy`"" -WorkingDirectory $scriptDir -WindowStyle Hidden
#     Write-Host "Web Dashboard started on http://127.0.0.1:8765" -ForegroundColor Green
#   } catch {
#     Write-Warning "Could not start Web Dashboard: $_"
#   }
# }

# Initialize variables used later even if bots are disabled (StrictMode-safe)
$ffInstances = @()
$rb = $null

# AutoJoinBot (launch first if configured and enabled)
if ($config.PSObject.Properties.Name -contains 'AutoJoinBot') {
  $ajEnabled = $true
  if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'Enabled') {
    try { $ajEnabled = [bool]$config.AutoJoinBot.Enabled } catch {}
  }
  if ($ajEnabled) {
    # Switch to assigned Desktop for AutoJoinBot
    if ($config.NewDesktop.UseNewVirtualDesktop) {
      $ajIdx = 1
      if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'DesktopIndex') { $ajIdx = [int]$config.AutoJoinBot.DesktopIndex }
      Switch-ToDesktopIndex -Index $ajIdx -Name "AutoJoinBot"
    }
    
    $ajExe = [string]$config.AutoJoinBot.ExePath
    $aj = Start-ConsoleApp -ExePath $ajExe
    $ajAccountsRaw = ''
    
    # Check RotationEnabled toggle (new simplified approach)
    $ajRotationEnabled = $false
    if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'RotationEnabled') {
      try { $ajRotationEnabled = [bool]$config.AutoJoinBot.RotationEnabled } catch {}
    }
    # Fallback to old RotateAccountsDaily for backward compatibility
    elseif ($config.AutoJoinBot.PSObject.Properties.Name -contains 'RotateAccountsDaily') {
      try { $ajRotationEnabled = [bool]$config.AutoJoinBot.RotateAccountsDaily } catch {}
    }
    
    if ($ajRotationEnabled) {
      # Use RotationPreset settings
      $preset = $null
      if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'RotationPreset') { $preset = $config.AutoJoinBot.RotationPreset }
      
      $poolRawAJ = ''
      $ajBatch = 1
      $ajRotationMode = 'DayOfYear'
      
      if ($preset) {
        if ($preset.PSObject.Properties.Name -contains 'AccountPool') { $poolRawAJ = [string]$preset.AccountPool }
        if ($preset.PSObject.Properties.Name -contains 'RotationBatchSize') { try { $ajBatch = [int]$preset.RotationBatchSize } catch {} }
        if ($preset.PSObject.Properties.Name -contains 'RotationMode') { $ajRotationMode = [string]$preset.RotationMode }
      }
      # Fallback to old settings for backward compatibility
      elseif ($config.AutoJoinBot.PSObject.Properties.Name -contains 'AccountPool') {
        $poolRawAJ = [string]$config.AutoJoinBot.AccountPool
        if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'RotationBatchSize') { try { $ajBatch = [int]$config.AutoJoinBot.RotationBatchSize } catch {} }
        if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'RotationMode') { $ajRotationMode = [string]$config.AutoJoinBot.RotationMode }
      }
      
      if ($poolRawAJ) {
        $poolAJ = $poolRawAJ.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
        if ($poolAJ.Count -gt 0) {
          $nowAJ = Get-Date
          $startIndexAJ = 0
          if ($ajRotationMode -eq 'DayOfWeek') {
            $dow = [int]$nowAJ.DayOfWeek; $mondayZero = ($dow - 1); if ($mondayZero -lt 0) { $mondayZero = 6 }
            $startIndexAJ = $mondayZero % $poolAJ.Count
          } else {
            $startIndexAJ = $nowAJ.DayOfYear % $poolAJ.Count
          }
          $selAJ = New-Object System.Collections.Generic.List[string]
          for ($i=0; $i -lt $ajBatch; $i++) { $idx = ($startIndexAJ + $i) % $poolAJ.Count; $selAJ.Add($poolAJ[$idx]) | Out-Null }
          $ajRotateCsv = ($selAJ -join ',')
          $ajAccountsRaw = $ajRotateCsv
          Write-Host ("AutoJoinBot using rotated accounts: {0}" -f $ajAccountsRaw) -ForegroundColor DarkCyan
        }
      }
    } else {
      # Use manual Accounts list
      if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'Accounts') { $ajAccountsRaw = [string]$config.AutoJoinBot.Accounts }
    }
  if ([string]::IsNullOrWhiteSpace($ajAccountsRaw)) { throw 'AutoJoinBot: No accounts specified. Provide AutoJoinBot.Accounts or enable AutoJoinBot.RotateAccountsDaily with a non-empty AccountPool.' }
  Send-Line -Process $aj -Text $ajAccountsRaw -DelayMs 2000
  $ajWait = 5
  if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'WaitAfterLaunchSeconds') { try { $ajWait = [int]$config.AutoJoinBot.WaitAfterLaunchSeconds } catch {} }
  if ($ajWait -gt 0) { Start-Sleep -Seconds $ajWait }
  
  # Wait for AutoJoinBot Chrome launches
  $ajExpectedCount = ($ajAccountsRaw.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }).Count
  if ($ajExpectedCount -gt 0) {
    Write-Host ("Waiting for AutoJoinBot Chrome launches (expected: {0})..." -f $ajExpectedCount) -ForegroundColor DarkYellow
    [void](Wait-ChromeProcesses -ExpectedCount $ajExpectedCount -TimeoutSeconds 180 -QuietPeriodSeconds 15)
  }
  }
}

# 0) Launch CommentsReplyBot first (if configured and enabled)
if ($config.PSObject.Properties.Name -contains 'CommentsReplyBot') {
  $crbEnabled = $true
  if ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'Enabled') {
    try { $crbEnabled = [bool]$config.CommentsReplyBot.Enabled } catch {}
  }
  if ($crbEnabled) {
    # Switch to assigned Desktop for CommentsReplyBot
    if ($config.NewDesktop.UseNewVirtualDesktop) {
      $crbIdx = 2
      if ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'DesktopIndex') { $crbIdx = [int]$config.CommentsReplyBot.DesktopIndex }
      Switch-ToDesktopIndex -Index $crbIdx -Name "CommentsReplyBot"
    }
    
    Write-Host 'Starting CommentsReplyBot (pre-FewFeed)...' -ForegroundColor Cyan
    $crbExe = [string]$config.CommentsReplyBot.ExePath
    $crb = Start-ConsoleApp -ExePath $crbExe

    # Check RotationEnabled toggle
    $crbRotationEnabled = $false
    if ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'RotationEnabled') {
      try { $crbRotationEnabled = [bool]$config.CommentsReplyBot.RotationEnabled } catch {}
    }
    elseif ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'RotateAccountsDaily') {
      try { $crbRotationEnabled = [bool]$config.CommentsReplyBot.RotateAccountsDaily } catch {}
    }

    # Build dynamic accounts map: AccountsForOptionX -> string
    $crbAccountsMap = @{}
    
    if (-not $crbRotationEnabled -and $config.CommentsReplyBot.PSObject.Properties.Name -contains 'ManualAccounts') {
      # Use ManualAccounts when rotation is disabled
      $manualAccts = $config.CommentsReplyBot.ManualAccounts
      foreach ($prop in $manualAccts.PSObject.Properties) {
        if ($prop.Name -like 'AccountsForOption*') {
          $optKey = ($prop.Name -replace '^AccountsForOption','')
          if ($optKey) { $crbAccountsMap[$optKey] = [string]$prop.Value }
        }
      }
    }
    else {
      # Use top-level AccountsForOptionX (backward compatibility)
      foreach ($prop in $config.CommentsReplyBot.PSObject.Properties) {
        if ($prop.Name -like 'AccountsForOption*') {
          $optKey = ($prop.Name -replace '^AccountsForOption','')
          if ($optKey) { $crbAccountsMap[$optKey] = [string]$prop.Value }
        }
      }
    }

  $crbWaitStep = 5
  if ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'WaitAfterEachStepSeconds') {
    $crbWaitStep = [int]$config.CommentsReplyBot.WaitAfterEachStepSeconds
  }

  # Send configured option sequence
  # Optional: list of options to ignore/skip
  $ignoreOptions = @()
  if ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'IgnoreOptions') {
    $ignoreOptions = @($config.CommentsReplyBot.IgnoreOptions | ForEach-Object { [string]$_ })
  }

  foreach ($opt in $config.CommentsReplyBot.OptionSequence) {
    $optStr = [string]$opt
    if ($ignoreOptions -and ($ignoreOptions -contains $optStr)) {
      Write-Host ("Skipping CommentsReplyBot option {0} due to IgnoreOptions config..." -f $optStr) -ForegroundColor Yellow
      continue
    }
    Send-Line -Process $crb -Text $optStr -DelayMs 800
    $accountsToSend = $null
    if ($crbAccountsMap.ContainsKey($optStr)) {
      $accountsToSend = $crbAccountsMap[$optStr]
    } else {
      # Try rotation if enabled
      $accountsToSend = Get-RotatedAccountsForOption -CommentsReplyBotConfig $config.CommentsReplyBot -Option $optStr
      if ($accountsToSend) {
        Write-Host ("Using rotated accounts for option {0}: {1}" -f $optStr, $accountsToSend) -ForegroundColor DarkCyan
      }
    }
    if ($accountsToSend) { Send-Line -Process $crb -Text $accountsToSend -DelayMs 2000 }
    if ($crbWaitStep -gt 0) { Start-Sleep -Seconds $crbWaitStep }
  }

  # Determine expected Chrome windows from accounts referenced in the sequence
  $expectedAccounts = New-Object System.Collections.ArrayList
  foreach ($opt in $config.CommentsReplyBot.OptionSequence) {
    $key = [string]$opt
    if ($crbAccountsMap.ContainsKey($key)) {
      $crbAccountsMap[$key].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' } | ForEach-Object {
        if (-not $expectedAccounts.Contains($_)) { [void]$expectedAccounts.Add($_) }
      }
    }
  }
  $expectedChrome = [Math]::Max(1, $expectedAccounts.Count)
  Write-Host ("Waiting for CommentsReplyBot Chrome launches (expected: {0})..." -f $expectedChrome) -ForegroundColor DarkYellow
  [void](Wait-ChromeProcesses -ExpectedCount $expectedChrome -TimeoutSeconds 240 -QuietPeriodSeconds 20)

  Write-Host 'CommentsReplyBot finished launching accounts.' -ForegroundColor Green



# Close outer CommentsReplyBot section block
}
}

# 1) Start FewFeed (after CommentsReplyBot and desktop switch) if configured and enabled
if ($config.PSObject.Properties.Name -contains 'FewFeed') {
  $ffEnabled = $true
  if ($config.FewFeed.PSObject.Properties.Name -contains 'Enabled') {
    try { $ffEnabled = [bool]$config.FewFeed.Enabled } catch {}
  }
  if ($ffEnabled) {
    # Switch to assigned Desktop for FewFeed
    if ($config.NewDesktop.UseNewVirtualDesktop) {
      $ffIdx = 3
      if ($config.FewFeed.PSObject.Properties.Name -contains 'DesktopIndex') { $ffIdx = [int]$config.FewFeed.DesktopIndex }
      Switch-ToDesktopIndex -Index $ffIdx -Name "FewFeed"
    }

    Write-Host 'Starting FewFeed...' -ForegroundColor Cyan
    $ffExe = $config.FewFeed.ExePath

    $ffMenu = [string]$config.FewFeed.MenuOptionToLaunch
    $ffAccountsRaw = ''
    
    # Check RotationEnabled toggle (new simplified approach)
    $ffRotationEnabled = $false
    if ($config.FewFeed.PSObject.Properties.Name -contains 'RotationEnabled') {
      try { $ffRotationEnabled = [bool]$config.FewFeed.RotationEnabled } catch {}
    }
    # Fallback to old RotateAccountsDaily for backward compatibility
    elseif ($config.FewFeed.PSObject.Properties.Name -contains 'RotateAccountsDaily') {
      try { $ffRotationEnabled = [bool]$config.FewFeed.RotateAccountsDaily } catch {}
    }
    
    if ($ffRotationEnabled) {
      # Use RotationPreset settings
      $preset = $null
      if ($config.FewFeed.PSObject.Properties.Name -contains 'RotationPreset') { $preset = $config.FewFeed.RotationPreset }
      
      $poolRawFF = ''
      $ffBatch = 1
      $ffRotationMode = 'DayOfYear'
      
      if ($preset) {
        if ($preset.PSObject.Properties.Name -contains 'AccountPool') { $poolRawFF = [string]$preset.AccountPool }
        if ($preset.PSObject.Properties.Name -contains 'RotationBatchSize') { try { $ffBatch = [int]$preset.RotationBatchSize } catch {} }
        if ($preset.PSObject.Properties.Name -contains 'RotationMode') { $ffRotationMode = [string]$preset.RotationMode }
      }
      # Fallback to old settings for backward compatibility
      elseif ($config.FewFeed.PSObject.Properties.Name -contains 'AccountPool') {
        $poolRawFF = [string]$config.FewFeed.AccountPool
        if ($config.FewFeed.PSObject.Properties.Name -contains 'RotationBatchSize') { try { $ffBatch = [int]$config.FewFeed.RotationBatchSize } catch {} }
        if ($config.FewFeed.PSObject.Properties.Name -contains 'RotationMode') { $ffRotationMode = [string]$config.FewFeed.RotationMode }
      }
      
      if ($poolRawFF) {
        $poolFF = $poolRawFF.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
        if ($poolFF.Count -gt 0) {
          $nowFF = Get-Date
          $startIndexFF = 0
          if ($ffRotationMode -eq 'DayOfWeek') {
            $dow = [int]$nowFF.DayOfWeek
            $mondayZero = ($dow - 1); if ($mondayZero -lt 0) { $mondayZero = 6 }
            $startIndexFF = $mondayZero % $poolFF.Count
          } else {
            $startIndexFF = $nowFF.DayOfYear % $poolFF.Count
          }
          $sel = New-Object System.Collections.Generic.List[string]
          for ($i=0; $i -lt $ffBatch; $i++) { $idx=($startIndexFF+$i)%$poolFF.Count; $sel.Add($poolFF[$idx])|Out-Null }
          $ffRotateCsv = ($sel -join ',')
          Write-Host ("FewFeed using rotated accounts: {0}" -f $ffRotateCsv) -ForegroundColor DarkCyan
          $ffAccountsRaw = $ffRotateCsv
        }
      }
    } else {
      # Use manual Accounts list
      if ($config.FewFeed.PSObject.Properties.Name -contains 'Accounts') {
        $ffAccountsRaw = [string]$config.FewFeed.Accounts
      }
    }
    $ffWait = [int]$config.FewFeed.WaitAfterLaunchSeconds

    # Validate we have accounts for FewFeed (either rotation produced some, or Accounts was provided)
    if ([string]::IsNullOrWhiteSpace($ffAccountsRaw)) {
      throw 'FewFeed: No accounts specified. Provide FewFeed.Accounts or enable FewFeed.RotateAccountsDaily with a non-empty AccountPool.'
    }
    $launchMode = 'single'
    if ($config.FewFeed.PSObject.Properties.Name -contains 'LaunchMode') { $launchMode = $config.FewFeed.LaunchMode }
    $seqDelay = 15
    if ($config.FewFeed.PSObject.Properties.Name -contains 'SequentialDelaySeconds') { $seqDelay = [int]$config.FewFeed.SequentialDelaySeconds }
    $batchSize = 1
    if ($config.FewFeed.PSObject.Properties.Name -contains 'BatchSize') { $batchSize = [int]$config.FewFeed.BatchSize }

    # Capture Chrome baseline BEFORE launching FewFeed so we can wait reliably afterwards
    $ffBaselineChrome = @(Get-Process -Name chrome -ErrorAction SilentlyContinue).Length

    if ($launchMode -in @('sequential','batch')) {
      $accountsList = $ffAccountsRaw.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
      if ($accountsList.Count -eq 0) { throw 'FewFeed.Accounts is empty.' }
      # Group into batches
      for ($i=0; $i -lt $accountsList.Count; $i+=$batchSize) {
        $chunk = $accountsList[$i..([Math]::Min($i+$batchSize-1, $accountsList.Count-1))]
        $csv = ($chunk -join ',')
        Write-Host ("Launching FewFeed instance for accounts: {0}" -f $csv) -ForegroundColor DarkCyan
        $isLastBatch = ($i + $batchSize -ge $accountsList.Count)
        if ($isLastBatch) {
          $ffProc = Start-FewFeedInstance -ExePath $ffExe -MenuOption $ffMenu -AccountsCsv $csv -PostWaitSeconds $ffWait -CaptureOutput
          $lastFF = $ffProc
        } else {
          $ffProc = Start-FewFeedInstance -ExePath $ffExe -MenuOption $ffMenu -AccountsCsv $csv -PostWaitSeconds $ffWait
        }
        $ffInstances += $ffProc
        if ($i + $batchSize -lt $accountsList.Count) {
          Write-Host ("Waiting {0}s before next FewFeed instance to reduce load..." -f $seqDelay) -ForegroundColor DarkYellow
          Start-Sleep -Seconds $seqDelay
        }
      }
    } else {
      # single process with all accounts
      $ff = Start-FewFeedInstance -ExePath $ffExe -MenuOption $ffMenu -AccountsCsv $ffAccountsRaw -PostWaitSeconds $ffWait -CaptureOutput
      $ffInstances += $ff
      $lastFF = $ff
    }

    Write-Host 'FewFeed launch sequence(s) sent.' -ForegroundColor Green

    # === Unified stabilization wait for FewFeed ===
    try {
      $ffExpectedAccounts = 0
      if ($launchMode -in @('sequential','batch')) { $ffExpectedAccounts = $accountsList.Count }
      else { $ffExpectedAccounts = ($ffAccountsRaw.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }).Count }
      
      $ffExpectedChrome = [Math]::Max(1, $ffExpectedAccounts)
      $quietPeriod = 20
      if ($config.FewFeed.PSObject.Properties.Name -contains 'StabilityQuietPeriodSeconds') { $quietPeriod = [int]$config.FewFeed.StabilityQuietPeriodSeconds }
      
      Write-Host ("Waiting for FewFeed Chrome stabilization (expected: {0}, quiet period: {1}s)..." -f $ffExpectedChrome, $quietPeriod) -ForegroundColor DarkYellow
      [void](Wait-ChromeProcesses -ExpectedCount $ffExpectedChrome -TimeoutSeconds 600 -QuietPeriodSeconds $quietPeriod)
      
      Write-Host "Ensuring all FewFeed Chrome processes are settled..." -ForegroundColor DarkYellow
      Start-Sleep -Seconds 5
      
    } catch {
      Write-Warning ('Failed waiting for FewFeed Chrome processes: ' + $_.Exception.Message)
    }
  } else {
    Write-Host 'FewFeed is disabled via config. Skipping launch.' -ForegroundColor Yellow
  }
}

if ($config.PSObject.Properties.Name -contains 'ReplyBot') {
  $rbEnabled = $true
  if ($config.ReplyBot.PSObject.Properties.Name -contains 'Enabled') {
    try { $rbEnabled = [bool]$config.ReplyBot.Enabled } catch {}
  }
  if ($rbEnabled) {
    # Switch to assigned Desktop for ReplyBot
    if ($config.NewDesktop.UseNewVirtualDesktop) {
      $rbIdx = 4
      if ($config.ReplyBot.PSObject.Properties.Name -contains 'DesktopIndex') { $rbIdx = [int]$config.ReplyBot.DesktopIndex }
      Switch-ToDesktopIndex -Index $rbIdx -Name "ReplyBot"
    }

    # Optional delay before ReplyBot to let FewFeed stabilize
    if ($config.ReplyBot.PSObject.Properties.Name -contains 'DelayBeforeStartSeconds') {
      $delayRB = [int]$config.ReplyBot.DelayBeforeStartSeconds
      if ($delayRB -gt 0) {
        Write-Host ("Waiting {0}s before starting ReplyBot..." -f $delayRB) -ForegroundColor DarkYellow
        Start-Sleep -Seconds $delayRB
      }
    }

    # 3) Start ReplyBot and run steps
    Write-Host 'Starting ReplyBot...' -ForegroundColor Cyan
    $rbExe = $config.ReplyBot.ExePath
    $rb = Start-ConsoleApp $rbExe

    # Check RotationEnabled toggle
    $rbRotationEnabled = $false
    if ($config.ReplyBot.PSObject.Properties.Name -contains 'RotationEnabled') {
      try { $rbRotationEnabled = [bool]$config.ReplyBot.RotationEnabled } catch {}
    }
    elseif ($config.ReplyBot.PSObject.Properties.Name -contains 'RotateAccountsDaily') {
      try { $rbRotationEnabled = [bool]$config.ReplyBot.RotateAccountsDaily } catch {}
    }

    # Build a map of option -> accounts string from config
    $accountsMap = @{}
    
    if (-not $rbRotationEnabled -and $config.ReplyBot.PSObject.Properties.Name -contains 'ManualAccounts') {
      # Use ManualAccounts when rotation is disabled
      $manualAccts = $config.ReplyBot.ManualAccounts
      foreach ($prop in $manualAccts.PSObject.Properties) {
        if ($prop.Name -like 'AccountsForOption*') {
          $optKey = ($prop.Name -replace '^AccountsForOption','')
          if ($optKey) { $accountsMap[$optKey] = [string]$prop.Value }
        }
      }
    }
    else {
      # Use top-level AccountsForOptionX (backward compatibility)
      if ($config.ReplyBot.PSObject.Properties.Name -contains 'AccountsForOption2') { $accountsMap['2'] = [string]$config.ReplyBot.AccountsForOption2 }
      if ($config.ReplyBot.PSObject.Properties.Name -contains 'AccountsForOption11') { $accountsMap['11'] = [string]$config.ReplyBot.AccountsForOption11 }
    }

    # Optional: list of ReplyBot options to ignore/skip
    $rbIgnore = @()
    if ($config.ReplyBot.PSObject.Properties.Name -contains 'IgnoreOptions') {
      $rbIgnore = @($config.ReplyBot.IgnoreOptions | ForEach-Object { [string]$_ })
    }

    foreach ($opt in $config.ReplyBot.OptionSequence) {
      $optStr = [string]$opt
      if ($rbIgnore -and ($rbIgnore -contains $optStr)) {
        Write-Host ("Skipping ReplyBot option {0} due to IgnoreOptions config..." -f $optStr) -ForegroundColor Yellow
        continue
      }
      Send-Line -Process $rb -Text $optStr -DelayMs 800
      $rbAccounts = $null
      if ($accountsMap.ContainsKey($optStr)) {
        $rbAccounts = $accountsMap[$optStr]
      } elseif ($rbRotationEnabled) {
        # Rotation for ReplyBot if enabled and no explicit AccountsForOptionX
        # Use RotationPreset settings
        $preset = $null
        if ($config.ReplyBot.PSObject.Properties.Name -contains 'RotationPreset') { $preset = $config.ReplyBot.RotationPreset }
        
        $poolRawRB = ''
        if ($preset -and $preset.PSObject.Properties.Name -contains 'AccountPool') {
          $poolRawRB = [string]$preset.AccountPool
        }
        elseif ($config.ReplyBot.PSObject.Properties.Name -contains 'AccountPool') {
          $poolRawRB = [string]$config.ReplyBot.AccountPool
        }
        
        if ($poolRawRB) {
          $poolRB = $poolRawRB.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
          if ($poolRB.Count -gt 0) {
            $batchRB = 1
            $rbBatchProp = ("BatchSizeForOption{0}" -f $optStr)
            
            if ($preset -and $preset.PSObject.Properties.Name -contains $rbBatchProp) {
              try { $batchRB = [int]($preset.$rbBatchProp) } catch {}
              if ($batchRB -le 0) { $batchRB = 1 }
            }
            elseif ($config.ReplyBot.PSObject.Properties.Name -contains $rbBatchProp) {
              try { $batchRB = [int]($config.ReplyBot.$rbBatchProp) } catch {}
              if ($batchRB -le 0) { $batchRB = 1 }
            }
            
            $rbRotationMode = 'DayOfYear'
            if ($preset -and $preset.PSObject.Properties.Name -contains 'RotationMode') {
              $rbRotationMode = [string]$preset.RotationMode
            }
            elseif ($config.ReplyBot.PSObject.Properties.Name -contains 'RotationMode') {
              $rbRotationMode = [string]$config.ReplyBot.RotationMode
            }
            
            $nowRB = Get-Date
            $startIndexRB = 0
            if ($rbRotationMode -eq 'DayOfWeek') {
              $dow = [int]$nowRB.DayOfWeek; $mondayZero = ($dow - 1); if ($mondayZero -lt 0) { $mondayZero = 6 }
              $startIndexRB = $mondayZero % $poolRB.Count
            } else {
              $startIndexRB = $nowRB.DayOfYear % $poolRB.Count
            }
            $selRB = New-Object System.Collections.Generic.List[string]
            for ($i=0; $i -lt $batchRB; $i++) { $idx = ($startIndexRB + $i) % $poolRB.Count; $selRB.Add($poolRB[$idx]) | Out-Null }
            $rbAccounts = ($selRB -join ',')
            Write-Host ("ReplyBot using rotated accounts for option {0}: {1}" -f $optStr, $rbAccounts) -ForegroundColor DarkCyan
          }
        }
      }
      if ($rbAccounts) { Send-Line -Process $rb -Text $rbAccounts -DelayMs 2000 }
      Start-Sleep -Seconds ([int]$config.ReplyBot.WaitAfterEachStepSeconds)
    }

    Write-Host 'ReplyBot sequence sent.' -ForegroundColor Green
    
    # Wait for ReplyBot Chrome launches
    $rbAllAccounts = New-Object System.Collections.Generic.HashSet[string]
    foreach ($opt in $config.ReplyBot.OptionSequence) {
        $optStr = [string]$opt
        $accts = $null
        if ($accountsMap.ContainsKey($optStr)) { $accts = $accountsMap[$optStr] }
        elseif ($rbRotationEnabled) {
            # This is a bit redundant but ensures we get a count
            $accts = Get-RotatedAccountsForOption -CommentsReplyBotConfig $config.ReplyBot -Option $optStr
        }
        if ($accts) { $accts.Split(',') | ForEach-Object { [void]$rbAllAccounts.Add($_.Trim()) } }
    }
    if ($rbAllAccounts.Count -gt 0) {
        Write-Host ("Waiting for ReplyBot Chrome launches (expected: {0})..." -f $rbAllAccounts.Count) -ForegroundColor DarkYellow
        [void](Wait-ChromeProcesses -ExpectedCount $rbAllAccounts.Count -TimeoutSeconds 240 -QuietPeriodSeconds 15)
    }
  } else {
    Write-Host 'ReplyBot is disabled via config. Skipping launch.' -ForegroundColor Yellow
  }
}

Write-Host "All launch sequences dispatched. Bots will continue in their own windows." -ForegroundColor Yellow

# Optionally return to a specific desktop after launching all bots
$returnTarget = $null
if ($config.PSObject.Properties.Name -contains 'Launcher') {
  if ($config.Launcher.PSObject.Properties.Name -contains 'ReturnToDesktopAfterLaunch') {
    $returnTarget = $config.Launcher.ReturnToDesktopAfterLaunch
  }
}

function Resolve-ReturnDesktopIndex {
  param([object]$Target)
  if ($null -eq $Target) { return $null }
  if ($Target -is [int]) { return [int]$Target }
  $name = [string]$Target
  switch -Regex ($name.ToLower()) {
    'autojoinbot' { 
      if ($config.AutoJoinBot.PSObject.Properties.Name -contains 'DesktopIndex') { return [int]$config.AutoJoinBot.DesktopIndex }
      return 1
    }
    'comments?replybot' { 
      if ($config.CommentsReplyBot.PSObject.Properties.Name -contains 'DesktopIndex') { return [int]$config.CommentsReplyBot.DesktopIndex }
      return 2
    }
    'fewfeed' { 
      if ($config.FewFeed.PSObject.Properties.Name -contains 'DesktopIndex') { return [int]$config.FewFeed.DesktopIndex }
      return 3
    }
    'replybot' { 
      if ($config.ReplyBot.PSObject.Properties.Name -contains 'DesktopIndex') { return [int]$config.ReplyBot.DesktopIndex }
      return 4
    }
    default {
      # if numeric string
      $out = 0
      if ([int]::TryParse($name, [ref]$out)) { return $out }
      return $null
    }
  }
}

$returnIndex = Resolve-ReturnDesktopIndex -Target $returnTarget
if ($returnIndex) {
  $doneReturn = $false
  if ($config.NewDesktop.UseNewVirtualDesktop -and (Ensure-VirtualDesktopModule -AutoInstall:([bool]$config.NewDesktop.AutoInstallIfMissing))) {
    if (Ensure-DesktopCount -Count $returnIndex) {
      if (Switch-ToDesktopIndex -Index $returnIndex) { $doneReturn = $true }
    }
  }
  if (-not $doneReturn -and $config.NewDesktop.UseNewVirtualDesktop) {
    Write-Warning ("Could not return to Desktop {0} via module. Attempting fallback create/switch..." -f $returnIndex)
    [void](Ensure-DesktopSwitch -Purpose ("ReturnTo{0}" -f $returnIndex))
  }
}

# Keep the launcher alive to avoid closing stdin pipes prematurely (which can cause EOFError in bots)
$keepAlive = $false
$extraHold = 0
if ($config.PSObject.Properties.Name -contains 'Launcher') {
  if ($config.Launcher.PSObject.Properties.Name -contains 'KeepAliveWhileChildrenRunning') { $keepAlive = [bool]$config.Launcher.KeepAliveWhileChildrenRunning }
  if ($config.Launcher.PSObject.Properties.Name -contains 'ExtraKeepAliveSecondsAfterStart') { $extraHold = [int]$config.Launcher.ExtraKeepAliveSecondsAfterStart }
}

if ($keepAlive) {
  Write-Host 'Keeping launcher open while bots are running. Close this window to terminate the launcher only.' -ForegroundColor Cyan
  try {
    $ids = @()
    if ($ffInstances) {
      foreach ($p in $ffInstances) { if ($p -and -not $p.HasExited) { $ids += $p.Id } }
    }
    if ($rb -and -not $rb.HasExited) { $ids += $rb.Id }
    if ($ids.Count -gt 0) { Wait-Process -Id $ids }
  } catch {}
} elseif ($extraHold -gt 0) {
  Start-Sleep -Seconds $extraHold
}
