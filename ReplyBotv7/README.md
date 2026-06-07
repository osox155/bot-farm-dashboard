# Messenger Auto-Reply Bot v3

A powerful Selenium-based automation bot for Facebook Messenger with multi-account support, auto-reply capabilities, bulk messaging, and real-time Telegram notifications.

## Table of Contents
1. [Features](#features)
2. [Menu Options](#menu-options)
3. [Configuration](#configuration)
4. [File Structure](#file-structure)
5. [How to Run](#how-to-run)
6. [New Features](#new-features)
7. [Troubleshooting](#troubleshooting)

---

## Features

### Core Features
- **Multi-Account Support**: Run multiple Messenger accounts simultaneously
- **Auto-Reply to Message Requests**: Automatically accept and reply to pending message requests
- **Main Chat Auto-Reply**: Reply to messages in existing conversations
- **Bulk Messaging**: Send messages to multiple recipients/groups at once
- **Cookie-Based Login**: Uses stored cookies for faster login (no password needed)
- **Real-Time Telegram Notifications**: Instant updates on bot activity and statistics

### Advanced Features
- **Auto-Login with Credentials**: Automatic login with email/password + TOTP 2FA when cookies fail
- **Window Size Configuration**: Custom or maximized Chrome window sizes via config
- **Google Sheets Integration**: Sync cookies from Google Sheets for remote management
- **Robust Recovery**: Auto-recovery when Messenger redirects or pages fail to load
- **Staggered Window Positioning**: Multiple Chrome windows don't overlap
- **Session Statistics**: Track all replies and messages with per-account breakdown
- **Pause/Resume**: Pause the bot anytime without stopping

---

## Menu Options

### Options 1 & 2: Auto-Reply to Message Requests
- **Option 1**: Start with single account
- **Option 2**: Start with multiple accounts

Monitors "You may know" and "Spam" tabs in message requests, auto-accepts chats, and sends your reply message.

**Key Features:**
- Caches chat URLs to recover if Messenger redirects
- Verifies message delivery before marking as processed
- Retries tab switching if page changes unexpectedly
- Updates Telegram instantly when a message is sent

### Option 3: Bulk Send Message to Groups/People (Option 12)
Send messages to multiple recipients defined in `recipients.txt`.
- Supports groups and individual chats
- Configurable delay between messages
- Repeat count for continuous sending

### Option 11: Auto-Reply in Main Chat
Reply to new messages in existing conversations.
- Monitors main chat list for new messages
- Sends reply automatically
- Tracks sent messages to avoid duplicates

### Account Management (Options 3-6, 8-9)
- **Option 3**: List all accounts
- **Option 4**: Add new account (login and save cookies)
- **Option 5**: Remove account
- **Option 6**: Rename account
- **Option 8**: Pause/Resume bot
- **Option 9**: View logs for account

### Other Options
- **Option 7**: Edit reply message template
- **Option 10**: Exit bot (sends final Telegram report)
- **Option 13**: Toggle auto popup closer

---

## Configuration

### config.json

```json
{
    "delay_between_messages": 3,
    "delay_resend_same_chat": 900,
    "delay_window_launch": 0,
    "delay_click_request": 3,
    "delay_accept": 2,
    "delay_retry_login": 4,
    "delay_failed_login": 10,
    "delay_check_requests": 60,
    "delay_chat_load": 5,
    "repeat_count": 0,
    "message_count": 1,
    "keep_browser_open_on_fail": true,
    "max_auto_relogin_attempts": 0,
    "auto_login_with_credentials": true,

    "google_sheets": {
        "enabled": true,
        "mode": "published_csv",
        "published_csv_url": "YOUR_GOOGLE_SHEETS_CSV_URL",
        "account_column": "account_file",
        "json_column": "cookies_json"
    },

    "telegram": {
        "bot_token": "YOUR_BOT_TOKEN",
        "chat_id": "YOUR_CHAT_ID",
        "min_interval_sec": 900,
        "auto_report_minutes": 30,
        "status_heartbeat_seconds": 60
    },

    "watchdog": {
        "inactivity_minutes": 15,
        "rdp_disconnect_grace_minutes": 10,
        "session_limit_hours": 6
    },

    "window": {
        "mode": "custom",
        "width": 800,
        "height": 600,
        "stagger_offset": 40
    },

    "fb_credentials": {
        "account_name": {
            "username": "email@example.com",
            "password": "password",
            "totp_secret": "TOTP_SECRET"
        }
    }
}
```

### Configuration Options Explained

#### Delays
| Option | Description | Default |
|--------|-------------|---------|
| `delay_between_messages` | Seconds between each message | 3 |
| `delay_resend_same_chat` | Seconds before resending to same chat | 900 |
| `delay_window_launch` | Seconds before launching browser | 0 |
| `delay_click_request` | Seconds before clicking Requests icon | 3 |
| `delay_accept` | Seconds before accepting request | 2 |
| `delay_retry_login` | Seconds before retrying login | 4 |
| `delay_failed_login` | Seconds after failed login | 10 |
| `delay_check_requests` | Seconds between request checks | 60 |
| `delay_chat_load` | Seconds to wait for chat load | 5 |
| `auto_login_with_credentials` | Enable auto-login with email/password when cookies fail | true |

#### Window Settings
| Option | Description | Values |
|--------|-------------|--------|
| `window.mode` | Window display mode | "custom" or "maximized" |
| `window.width` | Window width (custom mode) | 800 |
| `window.height` | Window height (custom mode) | 600 |
| `window.stagger_offset` | Pixels to offset each window | 40 |

**Window Mode:**
- `custom`: Uses specified width/height with staggered positioning
- `maximized`: Full-screen window (ignores width/height)

#### Telegram Settings
| Option | Description | Default |
|--------|-------------|---------|
| `bot_token` | Your Telegram bot token | - |
| `chat_id` | Your Telegram chat ID | - |
| `auto_report_minutes` | Minutes between auto reports | 30 |
| `status_heartbeat_seconds` | Seconds between status updates | 60 |

#### Google Sheets (Optional)
Sync cookies from Google Sheets for remote account management.
- `enabled`: true/false
- `published_csv_url`: Published CSV URL from Google Sheets
- `account_column`: Column name containing account filenames
- `json_column`: Column name containing cookie JSON

---

## File Structure

```
Reply Bot v3 (PY)/
├── new.py              # Main bot script
├── config.json         # Configuration file
├── reply_message.txt   # Default reply message template
├── recipients.txt      # Bulk send recipients list
├── accounts/           # Cookie files for each account
│   ├── 1_cookies.json
│   ├── 2_cookies.json
│   └── ...
├── chromedriver/       # ChromeDriver executable
│   └── chromedriver.exe
├── logs/               # Log files (auto-created)
│   ├── 1_cookies.json.log
│   └── ...
└── dist/ReplyBot/      # Compiled EXE (if built)
    ├── ReplyBot.exe
    ├── config.json
    ├── reply_message.txt
    ├── accounts/
    └── chromedriver/
```

---

## How to Run

### Method 1: Run with Python (Recommended for development)
```powershell
cd "c:\Users\HP\Downloads\RDP FOLDER\BOT\Reply Bot v3 (PY)"
py -3 new.py
```

### Method 2: Run Compiled EXE (For distribution)
Double-click or run:
```powershell
"c:\Users\HP\Downloads\RDP FOLDER\BOT\Reply Bot v3 (PY)\dist\ReplyBot\ReplyBot.exe"
```

### First Time Setup
1. **Add Accounts**: Select Option 4 to add Facebook accounts
2. **Edit Reply Message**: Select Option 7 to set your auto-reply message
3. **Configure Telegram** (Optional): Add bot_token and chat_id to config.json
4. **Configure Window Size** (Optional): Adjust window settings in config.json

---

## New Features

### 1. Enhanced Reply Logic (Options 1 & 2)
- **Chat URL Caching**: Remembers chat URLs to recover if Messenger redirects
- **Send Verification**: Verifies message was actually delivered before marking as processed
- **Auto-Recovery**: Detects off-route pages and recovers to cached URL
- **Retry Logic**: Retries sending if initial attempt fails

### 2. Window Size Configuration
Configure in `config.json`:
```json
"window": {
    "mode": "custom",
    "width": 1024,
    "height": 768,
    "stagger_offset": 50
}
```
- Supports custom dimensions or maximized mode
- Staggered positioning prevents window overlap
- Works for all options (1, 2, 11, 12)

### 3. Real-Time Telegram Updates
- **Instant Updates**: Telegram shows message within 3 seconds of sending
- **Force Updates**: Critical events bypass throttle for immediate notification
- **No Spam**: Intelligent throttling prevents duplicate messages
- **Real Tracking**: Per-account statistics with live counters

### 4. Robust Tab Switching
- **Retry Logic**: Multiple attempts to find and click tabs
- **Multiple Strategies**: Various XPath strategies for finding elements
- **Verification**: Confirms tab is active before proceeding
- **Page Verification**: Ensures on Requests page before switching tabs

### 5. Auto-Login with Credentials (2FA/TOTP Support)
When cookies expire or fail, bot can automatically log in using email/password + TOTP:

```json
"auto_login_with_credentials": true,
"fb_credentials": {
    "account_name_cookies.json": {
        "username": "your@email.com",
        "password": "your_password",
        "totp_secret": "YOUR_TOTP_SECRET"
    }
}
```

**How it works:**
1. Bot detects logout or cookie failure
2. First tries to refresh cookies from Google Sheets
3. If that fails, performs fresh login with email/password
4. Automatically generates TOTP 2FA code from secret
5. Enters the code and completes login
6. Saves new cookies locally (logs notice for Google Sheets update)

**Getting your TOTP Secret:**
- When setting up 2FA on Facebook, choose "Authentication App"
- Instead of scanning QR code, select "Can't scan? Show secret key"
- Copy the secret key (usually 32 characters) into `totp_secret`

**Note:** Writing cookies back to Google Sheets requires OAuth setup (not implemented - manual update needed).

### 6. Google Sheets Integration
Sync cookies remotely from Google Sheets:
- Automatically downloads latest cookies before login
- Useful for managing multiple accounts across machines
- Falls back to local cookies if sync fails

---

## Troubleshooting

### Common Issues

**EXE not working (ModuleNotFoundError)**
- Rebuild with: `py -3 -m PyInstaller --onedir --name "ReplyBot" ...` (see full command in build section)
- Ensure all dependencies are installed: `py -3 -m pip install selenium requests pyperclip webdriver-manager`

**Chrome window not found / chromedriver error**
- Ensure Chrome is installed
- Check `chromedriver/chromedriver.exe` exists
- Driver version should match Chrome version

**Telegram not receiving updates**
- Verify `bot_token` and `chat_id` in config.json
- Check bot has permission to send messages to chat
- Review `logs/` folder for Telegram errors

**Message not sending**
- Check `reply_message.txt` is not empty
- Verify account cookies are valid (re-add account if needed)
- Check logs in `logs/` folder for specific errors

**"Could not find Spam tab" errors**
- This is handled by the new verification logic
- Bot will retry and re-open Requests page if needed
- Check logs to see if recovery is working

**Auto-login with credentials not working**
- Verify credentials are correct in `config.json` under `fb_credentials`
- Ensure `auto_login_with_credentials` is set to `true`
- Check that TOTP secret is correct (32-character key from Facebook 2FA setup)
- If 2FA code is rejected, check system time is synced (TOTP requires accurate time)
- Look for "TOTP" or "credentials" in logs for specific errors

**TOTP code generation fails**
- TOTP requires system time to be accurate (within 30 seconds)
- Ensure `totp_secret` has no spaces (spaces are auto-removed)
- Secret should be 32 characters (Base32 encoded)
- Try generating a test code with a TOTP app using same secret

### Log Files
All activity is logged to `logs/` folder:
- `1_cookies.json.log` - Account-specific logs
- `mainchat_1_cookies.json.log` - Main chat mode logs
- `bulk_1_cookies.json.log` - Bulk send logs

---

## Tips

1. **Use `mode: "maximized"`** in config.json for full-screen windows if you have screen space
2. **Set `auto_report_minutes: 10`** for more frequent Telegram summaries
3. **Enable Google Sheets** to manage accounts remotely
4. **Check logs regularly** - they contain detailed information about bot activity
5. **Use Pause (Option 8)** instead of closing if you need to temporarily stop
6. **Set up auto-login credentials** to prevent bot stopping when cookies expire
7. **Add TOTP secret** for seamless 2FA handling - bot generates codes automatically

---

## Support

For issues or questions, check the log files first. They contain detailed error messages and stack traces that help identify problems.

