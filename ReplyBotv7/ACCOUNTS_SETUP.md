# Multiple Accounts Setup Guide

## 1. Account Credentials Structure

Each account now has **two files** in the `accounts/` folder:

```
accounts/
├── 2_cookies.json           # Your cookies (auto-generated/updated)
├── 2_cookies.credentials.json   # Your login credentials (you create this)
├── 3_cookies.json
├── 3_cookies.credentials.json
└── ...
```

## 2. Creating Credentials Files

Create a `.credentials.json` file for each account:

**Example: `accounts/2_cookies.credentials.json`**
```json
{
  "username": "your.email@example.com",
  "password": "your_password_here",
  "totp_secret": "ABCD1234EFGH5678"
}
```

**Template:**
```json
{
  "username": "",
  "password": "",
  "totp_secret": ""
}
```

## 3. How to Get TOTP Secret from Google Authenticator

### Method 1: During Initial 2FA Setup (Easiest)
When you first enable 2FA on Facebook:
1. Go to Facebook Settings → Security → Two-Factor Authentication
2. Choose "Authentication App"
3. When the QR code appears, click "Can't scan code?"
4. Facebook will show a **text secret key** (e.g., `NI6STVZDTPAMRKRQ6RRASFUHXPGLIWQ7`)
5. **Save this key!** This is your `totp_secret`
6. Add to Google Authenticator by entering the key manually

### Method 2: From Existing Google Authenticator (Harder)
Google Authenticator **does NOT allow exporting secrets** directly. You have 2 options:

**Option A: Re-setup 2FA**
1. Disable 2FA on Facebook temporarily
2. Re-enable it
3. Get the new secret key during setup
4. Update both Google Authenticator and your credentials file

**Option B: Use Alternative Authenticator App**
Apps like **Authy** or **Microsoft Authenticator** allow backup/sync:
1. Install Authy on your phone
2. Add the same Facebook account to Authy using the QR code
3. Authy will sync across devices and you can view the secret

### Method 3: Extract from QR Code (Advanced)
If you have the original QR code screenshot:
1. Use an online QR decoder (or Python library)
2. The QR contains: `otpauth://totp/Facebook:username?secret=XXXXXX&issuer=Facebook`
3. The `secret=XXXXXX` part is your `totp_secret`

## 4. Google Sheets API Setup (For Writing Cookies)

### Step 1: Create Google Cloud Project
1. Go to https://console.cloud.google.com/
2. Click "Select a project" → "New Project"
3. Name it "Messenger Bot" → Click "Create"

### Step 2: Enable Google Sheets API
1. In your project, go to "APIs & Services" → "Library"
2. Search "Google Sheets API"
3. Click "Enable"

### Step 3: Create Service Account
1. Go to "APIs & Services" → "Credentials"
2. Click "Create Credentials" → "Service Account"
3. Name: `messenger-bot`
4. Role: `Editor` (or `Viewer` if only reading)
5. Click "Continue" → "Done"

### Step 4: Download Service Account Key
1. Click on your service account name
2. Go to "Keys" tab
3. Click "Add Key" → "Create New Key"
4. Choose "JSON" format
5. Click "Create"
6. A `.json` file will download - **save it as `service_account.json` in your bot folder**

### Step 5: Share Your Google Sheet
1. Open your Google Sheet (where cookies are stored)
2. Click "Share" button (top right)
3. Add the service account email (found in your downloaded JSON, looks like: `messenger-bot@your-project.iam.gserviceaccount.com`)
4. Set permission to "Editor"
5. Click "Share"

### Step 6: Get Spreadsheet ID
1. Look at your Google Sheet URL
2. It's the long string between `/d/` and `/edit`
   Example: `https://docs.google.com/spreadsheets/d/1ABC123...XYZ/edit`
   Spreadsheet ID: `1ABC123...XYZ`

### Step 7: Update config.json
```json
"google_sheets": {
    "enabled": true,
    "mode": "api",
    "service_account_json": "service_account.json",
    "spreadsheet_id": "1IeRitONX3rBeD1mu2QSwI-Uij1tCZhAqlil0QOFjdMU",
    "sheet_name": "Sheet1",
    "account_column": "account_file",
    "json_column": "cookies_json"
}
```

### Step 8: Install Required Library
```bash
pip install gspread
```

## 5. Testing Auto-Login

1. Create credentials file for your account
2. Make sure `auto_login_with_credentials: true` in config.json
3. Delete or rename your cookies file to simulate expiration:
   ```bash
   cd accounts
   ren 2_cookies.json 2_cookies.json.backup
   ```
4. Run the bot
5. Watch the logs - it should auto-login with credentials + TOTP

## Security Notes

- **Keep `.credentials.json` files secure** - they contain passwords
- **Never commit them to git** - add to `.gitignore`:
  ```
  accounts/*.credentials.json
  service_account.json
  ```
- **Use strong unique passwords** for each Facebook account
- **TOTP secrets are sensitive** - treat them like passwords
