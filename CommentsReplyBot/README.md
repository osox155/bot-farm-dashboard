# Facebook Group Comment Reply Bot

An automated bot that replies to comments in Facebook groups using Selenium WebDriver and cookie-based authentication.

## Features

- **Cookie-based Authentication**: Uses saved Facebook cookies for login
- **Multi-group Support**: Process multiple Facebook groups from `groups.txt`
- **Smart Comment Filtering**: Skip comments based on length, keywords, and users
- **Randomized Replies**: Randomly selects reply messages from `reply_text.txt`
- **Comprehensive Logging**: Tracks all actions, replies, and errors
- **Clean Console Interface**: Simple menu-driven operation
- **Post Processing**: Automatically finds new posts and processes comments
- **Duplicate Prevention**: Tracks processed posts and comments to avoid spam

## Setup

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Chrome Driver**: 
   - Download ChromeDriver from https://chromedriver.chromium.org/
   - Extract `chromedriver.exe` to the `chromedriver/` folder
   - Make sure ChromeDriver version matches your Chrome browser version

3. **Facebook Cookies**: 
   - Export your Facebook cookies as JSON
   - Save them as `accounts/1_cookies.json`, `accounts/2_cookies.json`, etc.
   - Use browser extensions like "Cookie Editor" to export cookies

4. **Configuration**:
   - Edit `config.json` to adjust bot settings
   - Add Facebook group URLs to `groups.txt`
   - Customize reply messages in `reply_text.txt`

## File Structure

```
comments_reply_bot/
├── accounts/                 # Facebook cookie files
│   ├── 1_cookies.json       # Account 1 cookies
│   └── 2_cookies.json       # Account 2 cookies
├── logs/                    # Log files and processed data
│   ├── bot.log             # Main log file
│   ├── processed_posts.json    # Processed posts tracking
│   └── processed_comments.json # Processed comments tracking
├── config.json             # Bot configuration
├── groups.txt              # Facebook group URLs
├── reply_text.txt          # Reply messages (one per line)
├── facebook_bot.py         # Main bot script
├── requirements.txt        # Python dependencies
└── README.md              # This file
```

## Configuration Options

### `config.json` Settings:

#### **bot_settings**
- `delay_between_actions`: 2 - General delay between actions (seconds)
- `delay_between_comments`: 5 - Delay between processing comments (seconds)
- `delay_between_posts`: 10 - Delay between processing posts (seconds)
- `delay_between_groups`: 15 - Delay between processing groups (seconds)
- `max_comments_per_post`: 0 - Max comments to process per post (0 = unlimited)
- `max_posts_per_group`: 10 - Max posts to process per group
- `max_posts_per_session`: 20 - Max posts per session (not currently used)
- `headless_mode`: false - Run browser in headless mode
- `window_size`: "1920,1080" - Browser window size
- `max_scrolls_collect`: 10 - Max scrolls when collecting posts
- `max_comment_load_rounds`: 8 - Max rounds to load more comments
- `stagnation_threshold`: 3 - Scrolls before considering page stagnant
- `scroll_pause_seconds`: 1.5 - Pause between scrolls
- `continuous_mode`: false - **Enable for unlimited operation**
- `continuous_cycle_minutes`: 30 - Minutes between cycles in continuous mode
- `revisit_old_posts`: false - Check old posts for new comments

#### **reply_settings**
- `reply_probability`: 0.8 - Probability of replying to a comment (0.0-1.0)
- `skip_own_comments`: true - Skip your own comments
- `skip_replied_comments`: true - Skip already replied comments
- `max_reply_length`: 500 - Maximum reply text length
- `randomize_replies`: true - Randomize reply selection

#### **filters**
- `min_comment_length`: 3 - Minimum comment length to reply to
- `skip_keywords`: ["spam", "bot", "fake"] - Keywords to skip
- `skip_users`: [] - Usernames to skip
- `only_reply_to_posts_newer_than_hours`: 24 - Only reply to recent posts

#### **logging**
- `log_level`: "INFO" - Logging level (DEBUG, INFO, WARNING, ERROR)
- `log_file`: "logs/bot.log" - Log file path
- `log_replies`: true - Log reply actions
- `log_skipped`: true - Log skipped comments
- `log_errors`: true - Log errors
- `log_to_console`: true - Show logs in console

#### **anti_block**
- `cooldown_minutes_on_block`: 30 - Cooldown when rate limited
- `reply_jitter`: [0.3, 1.2] - Random delay range for replies
- `post_jitter`: [1.0, 3.0] - Random delay range for posts

#### **posts_multitab** (Option 5)
- `refresh_seconds`: 60 - Refresh interval for tabs
- `per_tab_pause_seconds`: 3 - Pause between tab switches
- `max_rounds`: 0 - Max processing rounds (0 = unlimited)

#### **sheets_csv** (Google Sheets Integration)
- `enabled`: true - Enable Google Sheets integration
- `url`: "..." - Public CSV URL from Google Sheets
- `groups_col`: 0 - Column index for groups (0 = A)
- `posts_col`: 1 - Column index for posts (1 = B)
- `reload_seconds`: 300 - Reload interval for sheets data

## Usage

1. **Run the Bot**:
   ```bash
   python facebook_bot.py
   ```

2. **Menu Options**:
   - **Option 1 - Start Bot**: Process groups once or continuously
   - **Option 2 - Show Statistics**: View processing statistics  
   - **Option 3 - View Logs**: Display recent log entries
   - **Option 4 - Exit**: Close the application
   - **Option 5 - Multi-Tab Posts Mode**: Process specific posts from posts.txt

3. **Account Selection**: Enter account number (e.g., "1" for `1_cookies.json`)

## Continuous Operation (Option 1)

To enable **unlimited continuous operation**:

1. Set `"continuous_mode": true` in config.json
2. Set `"continuous_cycle_minutes": 30` (adjust as needed)
3. Run Option 1

**What happens:**
- Bot processes all groups in sequence
- Waits for the specified cycle time
- Repeats indefinitely
- Finds new posts and new comments on old posts
- Hot-reloads groups from Google Sheets if enabled

**For new comments on old posts:**
- Set `"revisit_old_posts": true` 
- Bot will re-check previously processed posts for new comments

## How It Works

1. **Authentication**: Loads cookies from `accounts/{number}_cookies.json`
2. **Group Processing**: Iterates through URLs in `groups.txt`
3. **Post Discovery**: Finds new posts not in processed list
4. **Comment Processing**: Processes comments that haven't been replied to
5. **Reply Generation**: Randomly selects message from `reply_text.txt`
6. **Logging**: Records all actions in log files
7. **Post Closure**: Closes post modal after processing

## Safety Features

- **Duplicate Prevention**: Tracks processed posts/comments
- **Rate Limiting**: Configurable delays between actions
- **Content Filtering**: Skip inappropriate comments
- **User Blacklist**: Skip specific users
- **Error Handling**: Graceful error recovery
- **Logging**: Comprehensive activity tracking

## Customization

### Reply Messages
Edit `reply_text.txt` - add one message per line:
```
Thanks for sharing this!
Great post, very informative!
Interesting perspective, thanks!
```

### Group URLs
Edit `groups.txt` - add one URL per line:
```
https://www.facebook.com/groups/544742624448267/
https://www.facebook.com/groups/your-group-id/
```

### Filters
Modify `config.json` filters section:
```json
"filters": {
    "min_comment_length": 5,
    "skip_keywords": ["spam", "bot", "fake"],
    "skip_users": ["username1", "username2"]
}
```

## Troubleshooting

1. **Login Issues**: 
   - Ensure cookies are fresh (< 24 hours old)
   - Check cookie format is valid JSON
   - Verify Facebook account is not restricted

2. **Element Not Found**:
   - Facebook frequently changes their HTML structure
   - Update CSS selectors in the code if needed
   - Check if you're logged in properly

3. **Rate Limiting**:
   - Increase delays in `config.json`
   - Reduce `max_comments_per_post` and `max_posts_per_session`

## Legal Notice

This bot is for educational purposes. Ensure compliance with:
- Facebook's Terms of Service
- Group rules and guidelines
- Local laws and regulations
- Respect for other users

Use responsibly and avoid spam behavior.
