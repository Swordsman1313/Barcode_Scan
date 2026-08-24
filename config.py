"""
Configuration loader for Telegram Stock Count Bot.
Loads environment variables from .env file with validation and default fallbacks.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / ".env")

# Telegram Bot Token (from @BotFather)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Google Apps Script Webhook URL (Optional for live Google Sheets auto-fill)
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()

# SQLite Database path
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "inventory.db"))

# Directory to save captured photos
PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR", str(BASE_DIR / "photos")))
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# Timezone (e.g., 'Asia/Bangkok', 'Asia/Singapore', 'UTC')
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bangkok")

# Admin Telegram user IDs (comma-separated, e.g. "12345678,87654321")
ADMIN_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS = set()
if ADMIN_IDS_RAW:
    for uid in ADMIN_IDS_RAW.split(","):
        uid_clean = uid.strip()
        if uid_clean.isdigit():
            ADMIN_USER_IDS.add(int(uid_clean))

# Concurrency & Network configuration
SHEET_SYNC_MAX_RETRIES = int(os.getenv("SHEET_SYNC_MAX_RETRIES", "3"))
SHEET_SYNC_TIMEOUT_SECONDS = int(os.getenv("SHEET_SYNC_TIMEOUT_SECONDS", "10"))
