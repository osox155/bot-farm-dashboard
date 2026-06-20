# setup_obs.ps1 — provision OBS on a fresh ephemeral RDP box.
# OBS is installed by your YAML but has NO config on a new box. This writes:
#   • WebSocket server enabled (so run_match.py can control OBS)
#   • Recording output -> mp4 into $RecDir
#   • Stream service -> Restream (or any RTMP) using $env:RESTREAM_KEY
#   • A default "Match" scene with a full-screen Display Capture
# Idempotent: safe to run on every boot. Reads secrets from env vars.

param(
  [string]$RecDir   = $(if ($env:OBS_REC_DIR) { $env:OBS_REC_DIR } else { "C:\obs-recordings" }),
  [string]$WsPass   = $env:OBS_PASSWORD,
  [int]   $WsPort   = $(if ($env:OBS_PORT) { [int]$env:OBS_PORT } else { 4455 }),
  [string]$RtmpUrl  = $(if ($env:RTMP_URL) { $env:RTMP_URL } else { "rtmp://live.restream.io/live" }),
  [string]$StreamKey= $env:RESTREAM_KEY
)

$ErrorActionPreference = 'Stop'
$cfg = Join-Path $env:APPDATA 'obs-studio'
New-Item -ItemType Directory -Force -Path $RecDir | Out-Null
New-Item -ItemType Directory -Force -Path "$cfg\basic\profiles\Match" | Out-Null
New-Item -ItemType Directory -Force -Path "$cfg\basic\scenes" | Out-Null

if (-not $WsPass) { $WsPass = "soccer$(Get-Random -Maximum 99999)"; Write-Host "Generated OBS_PASSWORD=$WsPass (save it!)" }

# --- global.ini : websocket + active profile/scene ---
@"
[General]
FirstRun=true

[Basic]
Profile=Match
ProfileDir=Match
SceneCollection=Match
SceneCollectionFile=Match

[OBSWebSocket]
FirstLoad=false
ServerEnabled=true
ServerPort=$WsPort
AuthRequired=true
ServerPassword=$WsPass
"@ | Set-Content -Encoding UTF8 "$cfg\global.ini"

# --- profile basic.ini : recording to mp4 ---
$recEsc = $RecDir.Replace('\','\\')
@"
[Output]
Mode=Simple

[SimpleOutput]
FilePath=$recEsc
RecFormat2=mp4
VBitrate=4000
ABitrate=160
StreamEncoder=x264
RecQuality=Stream

[Video]
BaseCX=1920
BaseCY=1080
OutputCX=1280
OutputCY=720
FPSInt=30
"@ | Set-Content -Encoding UTF8 "$cfg\basic\profiles\Match\basic.ini"

# --- profile service.json : RTMP stream target (Restream) ---
@"
{
  "type": "rtmp_custom",
  "settings": { "server": "$RtmpUrl", "key": "$StreamKey", "use_auth": false }
}
"@ | Set-Content -Encoding UTF8 "$cfg\basic\profiles\Match\service.json"

# --- scene collection : one full-screen Display Capture ---
@"
{
  "current_scene": "Match",
  "current_program_scene": "Match",
  "name": "Match",
  "scene_order": [ { "name": "Match" } ],
  "sources": [
    {
      "id": "monitor_capture",
      "name": "Display Capture",
      "versioned_id": "monitor_capture",
      "settings": { "method": 0, "monitor_id": "DUMMY", "capture_cursor": true },
      "enabled": true
    },
    {
      "id": "scene",
      "name": "Match",
      "versioned_id": "scene",
      "settings": {
        "items": [
          { "name": "Display Capture", "visible": true, "bounds_type": 2,
            "bounds": { "x": 1280.0, "y": 720.0 }, "pos": { "x": 0.0, "y": 0.0 } }
        ]
      }
    }
  ]
}
"@ | Set-Content -Encoding UTF8 "$cfg\basic\scenes\Match.json"

Write-Host "OBS provisioned. Rec dir: $RecDir  | WS port: $WsPort"
Write-Host "Launch OBS once so it loads the config, then run run_match."
