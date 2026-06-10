# Launcher & Deployment

Covers `start-bots.ps1`, `bot-launcher.config.json`, `start-bots.bat`, `kill-broker.bat`,
`Procfile`, `wsgi.py`.

## What the launcher does

`start-bots.bat` (double-click) → `powershell -NoProfile -ExecutionPolicy Bypass -File start-bots.ps1`.
Requires **PowerShell 5.1**, runs under `Set-StrictMode -Latest` + `$ErrorActionPreference='Stop'`. No
CLI args — everything comes from `bot-launcher.config.json` next to the script.

Boot sequence:
1. Start `telemetry_broker.py` hidden in the background with `--parent-pid $PID`.
2. For each bot block, **in config order** (AutoJoinBot → CommentsReplyBot → FewFeed → ReplyBot), if
   `Enabled`:
   a. Switch to the bot's `DesktopIndex` virtual desktop.
   b. Launch its Python entry point as a **visible console child process** (`python "<ExePath>"`,
      `RedirectStandardInput=true`).
   c. Feed its interactive menu via **stdin**: the menu option(s) then a comma-separated accounts CSV.
   d. Wait for Chrome processes to stabilize (one `chrome.exe` per account) before moving on.
3. Optionally return to a named desktop (`ReturnToDesktopAfterLaunch`) and keep the launcher alive
   (`Wait-Process` on child PIDs) so the stdin pipes stay open.

The local `dashboard.py` launch inside the script is **commented out** — the dashboard is expected to
run in the cloud (see Deployment); the local machine only runs the broker.

## Virtual-desktop switching

When `NewDesktop.UseNewVirtualDesktop=true`, each bot is launched on its own Windows Virtual Desktop:
- **Primary method:** the `VirtualDesktop` PowerShell module (auto-installed from PSGallery if missing
  and `AutoInstallIfMissing=true`) — `Get-Desktop`/`Switch-Desktop` (zero-based = `DesktopIndex - 1`).
- **Fallback:** synthesize `Win+Ctrl+Left/Right` / `Win+Ctrl+D` chords via Win32 `SendInput` P/Invoke.

Default desktop assignments (hardcoded in the script, matched by config): AutoJoinBot=1,
CommentsReplyBot=2, FewFeed=3, ReplyBot=4.

## Driving each bot's stdin menu

`Start-ConsoleApp` launches the process; `Send-Line` writes a line to the child's StandardInput with a
post-write delay (throws if the process already exited).
- **Menu bots** (CommentsReplyBot `OptionSequence`, ReplyBot `OptionSequence`): for each option in the
  sequence, send the option (≈800ms delay) then the accounts CSV (≈2000ms), with
  `WaitAfterEachStepSeconds` between.
- **FewFeed** (`Start-FewFeedInstance`): send `MenuOptionToLaunch` then the accounts CSV, supporting
  `LaunchMode` single / sequential / batch (one process per `BatchSize` accounts,
  `SequentialDelaySeconds` apart).
- **AutoJoinBot**: sends only the accounts CSV (no menu option).

> `OptionSequence` is a JSON **scalar** (e.g. `1`, `2`), not an array. PowerShell's `foreach` over a
> scalar iterates once — intended single-step behavior. To send multiple options you must make it a
> JSON array.

## ⭐ Day-based account rotation (the math)

When `<Bot>.RotationEnabled=true`, the accounts are chosen by date and **`ManualAccounts`/`Accounts`
are ignored**. Algorithm (`Get-RotatedAccountsForOption` + inline variants):

1. `pool` = `RotationPreset.AccountPool` split on commas (all blocks: `2,3,4`).
2. `batchSize` = `BatchSizeForOption<N>` (per-option, CRB/RB) or `RotationBatchSize` (AJ/FF).
3. `startIndex`:
   - **DayOfWeek** (all current blocks): `dow = [int]Now.DayOfWeek` (Sun=0…Sat=6);
     `mondayZero = dow-1` (wrap `-1→6`); `startIndex = mondayZero % pool.Count`.
   - **DayOfYear**: `startIndex = DayOfYear % pool.Count`.
4. Take `batchSize` consecutive pool entries from `startIndex`, **wrapping** modulo `pool.Count`.

> With pool `[2,3,4]` (size 3) over a 7-day week, days repeat:
> **Mon/Thu/Sun → start 0 (acct 2)**, **Tue/Fri → start 1 (acct 3)**, **Wed/Sat → start 2 (acct 4)**.
> It is *not* contiguous weekly coverage.

**Worked example — Wednesday with only ReplyBot enabled** (`OptionSequence=2`, `BatchSizeForOption2=2`):
`mondayZero = 3-1 = 2`, `startIndex = 2 % 3 = 2` → pool[2]=`4`, wrap → pool[0]=`2`. **ReplyBot launches
accounts `4,2`.** Note `ReplyBot.ManualAccounts.AccountsForOption2="6"` is a **red herring** — it's only
read when `RotationEnabled=false`, so account `6` is **not** used while rotation is on.

## `bot-launcher.config.json` — key reference

Per-bot block:
| Key | Meaning |
|-----|---------|
| `Enabled` | on/off (defaults true if absent). Currently only **ReplyBot** is true. |
| `DesktopIndex` | 1-based virtual desktop. |
| `ExePath` | Python entry file, relative to script dir. |
| `OptionSequence` / `MenuOptionToLaunch` | menu option(s) sent to stdin. |
| `RotationEnabled` | rotate accounts by date vs. use manual list. |
| `RotationPreset.AccountPool` | comma list to rotate through (`2,3,4`). |
| `RotationPreset.RotationMode` | `DayOfWeek` or `DayOfYear`. |
| `RotationPreset.RotationBatchSize` / `BatchSizeForOption<N>` | how many accounts to take. |
| `ManualAccounts.AccountsForOption<N>` / `Accounts` | fixed accounts, **only when rotation off**. |
| `LaunchMode` / `SequentialDelaySeconds` / `BatchSize` | FewFeed launch shaping. |
| `WaitAfterLaunchSeconds` / `WaitAfterEachStepSeconds` / `DelayBeforeStartSeconds` | pacing. |

Global blocks:
- `NewDesktop`: `UseNewVirtualDesktop`, `AutoInstallIfMissing`, `TryVirtualDesktopModuleFirst` (unused).
- `Launcher`: `KeepAliveWhileChildrenRunning`, `ExtraKeepAliveSecondsAfterStart`,
  `ReturnToDesktopAfterLaunch`.

Current state: **ReplyBot only.** AutoJoinBot/CommentsReplyBot/FewFeed are `Enabled:false`.

## Teardown

`kill-broker.bat` terminates any `telemetry_broker` process and deletes `telemetry\*_*.json` session
files. (The broker's own parent-watchdog also tears the fleet down when the launcher exits.)

## Deployment of the dashboard

The dashboard is a separate concern, deployed to a PaaS (PythonAnywhere / Render):
- `Procfile`: `web: python dashboard.py` (Bottle's dev server — fine for free tiers).
- `wsgi.py`: `from dashboard import app as application` (the proper WSGI entry for PythonAnywhere).
- `DASHBOARD_PASSWORD` env var enables auth (see [TELEMETRY_AND_DASHBOARD.md](TELEMETRY_AND_DASHBOARD.md)).

## Launcher gotchas

See [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for the full list. Highlights:
- `Resolve-ReturnDesktopIndex` regex `comments?replybot` matches both `CommentsReplyBot` **and**
  `ReplyBot` — a bare `ReplyBot` return target would wrongly resolve to desktop 2.
- A fallback path calls `Ensure-DesktopSwitch`, **a function that doesn't exist** — would throw if the
  module-based desktop return fails.
- Keep-alive `Wait-Process` only waits on FewFeed + ReplyBot PIDs; if only AutoJoin/Comments were
  enabled, the launcher wouldn't block on them.
