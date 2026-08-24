# 📦 Telegram Stock Count Bot

A high-speed, multi-user Telegram bot designed for **store crew to log stock items** during inventory counting—especially for products with damaged, crumpled, or unscannable barcodes.

Features **isolated multi-user sessions**, **sticky shelf memory**, **smart barcode auto-detection**, **instant Google Sheets auto-fill via Webhook**, and **styled Excel (`.xlsx`) report exports**.

---

## ✨ Key Features

- 👥 **Multi-Crew Concurrency**: Multiple store crew members can count stock across different aisles simultaneously without data interference.
- 📌 **Sticky Shelf Memory (`/shelf G101`)**: Set the active shelf once, and all subsequent scans automatically use that shelf with 1-tap confirmation.
- 📸 **Dual Photo Capture**: Takes photo of product front + barcode label.
- 🔍 **Smart Barcode Auto-Detection**: Auto-detects barcodes from images using `zxing-cpp` (EAN-13, UPC-A, Code 128, QR, etc.) and pre-fills the barcode for instant confirmation, with manual typing fallback.
- ⚡ **1-Tap Fast Quantity Selection**: Quick inline buttons `[ 1 ] [ 2 ] [ 5 ] [ 10 ] [ 12 ] [ 24 ]` or custom numeric input.
- 🌐 **Real-Time Google Sheets Auto-Fill**: Uses a lightweight Google Apps Script Webhook (<1 min setup, no Google Cloud project required) with asynchronous non-blocking background queue.
- 📊 **Instant Multi-Tab Excel Export (`/export`)**: Sends a formatted `.xlsx` report directly inside Telegram containing:
  1. 📋 **Stock Items**: All scanned records with text-safe barcode formatting (preventing `8.85E+12` truncation).
  2. 🏢 **Summary by Shelf**: Aggregated SKU and quantity counts per shelf.
  3. 👤 **Summary by Crew**: Total counts attributed to each crew member.
- 🔒 **SQLite WAL Database**: Write-Ahead Logging mode for fast concurrent writes and offline resilience.

---

## 📱 Store Crew Telegram Flow

```
Store Crew sends photo of item (or types /count)
   │
   ├─► 📸 Front photo captured
   ├─► 📸 Send / Skip Barcode label photo
   ├─► 📍 Confirm Shelf (e.g. [✅ Keep G101] or type new)
   ├─► 🏷️ Confirm Barcode (Auto-detected: 8850123456787 or type manually)
   ├─► 📦 Enter Item Name (or tap [⏩ Skip])
   ├─► 🔢 Select/Type Quantity [ 1 ] [ 2 ] [ 6 ] [ 12 ] [ 24 ]
   │
   ▼
✅ SAVED! (Instant confirmation card)
👉 Send next photo to keep counting on Shelf G101!
```

---

## 🚀 Quick Start Guide

### Step 1: Install & Setup Environment
Run the automated setup script:
```bash
./setup.sh
```
Or manually:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

---

### Step 2: Get Telegram Bot Token
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to name your bot.
3. Copy the HTTP API token provided by BotFather.
4. Open `.env` and paste your token:
   ```env
   TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
   ```

---

### Step 3 (Optional): Connect Google Sheets for Real-Time Auto-Fill
1. Open your **Google Sheet**.
2. Go to **Extensions** → **Apps Script**.
3. Delete any default code and copy-paste the entire contents of [`google_apps_script.js`](file:///Users/mac/Desktop/Computer_Science/Business/Barcode_Scan/google_apps_script.js).
4. Click **Deploy** (top right) → **New deployment**.
5. Select type: **Web app**.
   - **Execute as**: *Me*
   - **Who has access**: *Anyone* (Important!)
6. Click **Deploy**, authorize permissions, and copy the **Web App URL**.
7. Paste into `.env`:
   ```env
   GOOGLE_SHEET_WEBHOOK_URL="https://script.google.com/macros/s/AKfycb.../exec"
   ```

---

### Step 4: Run the Bot
```bash
source venv/bin/activate
python3 bot.py
```

---

## 🤖 Telegram Bot Commands

| Command | Description |
| :--- | :--- |
| `/start` or `/help` | Shows instructions, active shelf, and command list |
| `/count` (or send photo) | Starts counting a new stock item |
| `/shelf` | Views your current active shelf |
| `/shelf <CODE>` | Sets your active shelf (e.g. `/shelf G101`) |
| `/mycounts` | Lists your last 8 counted items with sync status |
| `/delete_last` | Deletes your most recent item count |
| `/delete <ID>` | Deletes a specific item by ID (e.g. `/delete 15`) |
| `/stats` | Shows total SKUs, total units, shelf breakdown, and crew breakdown |
| `/export` | 📊 Generates and downloads the Excel (`.xlsx`) workbook in Telegram |
| `/sync` | Manually pushes any pending items to Google Sheets |
| `/cancel` | Cancels current counting session and resets |

---

## 📊 Excel (.xlsx) Report Structure

The generated Excel workbook contains 3 formatted tabs:

1. **Tab 1: `Stock Items`**
   - Columns: `No.`, `Date & Time`, `Crew Member`, `Shelf Location`, `Barcode Number`, `Item Name / Description`, `Quantity`, `Sheet Sync`
   - Styled navy headers, zebra striping, auto-filter, text format `@` on barcodes, and total summary row `=SUM(...)`.
2. **Tab 2: `Summary by Shelf`**
   - Columns: `Shelf Location`, `Total SKUs (Items)`, `Total Quantity Units`
3. **Tab 3: `Summary by Crew`**
   - Columns: `Crew Member`, `Total SKUs Logged`, `Total Quantity Logged`

---

## 🧪 Testing & Verification

Run the automated test suite anytime to verify concurrency, database integrity, and Excel generation:
```bash
source venv/bin/activate
python3 -m unittest tests/test_suite.py
```

To seed 12 realistic sample retail items for testing:
```bash
python3 seed_sample_data.py
```

---

## 📂 Project Structure

```
Barcode_Scan/
├── bot.py                  # Telegram Bot application & state handlers
├── config.py               # Configuration & environment variable loader
├── database.py             # SQLite WAL database layer (thread-safe, async)
├── barcode_reader.py       # zxing-cpp auto barcode decoder with PIL enhancements
├── sheets_sync.py          # Asynchronous background Google Sheets sync worker
├── excel_exporter.py       # Professional multi-tab Excel (.xlsx) generator
├── google_apps_script.js   # 1-minute Google Sheets Webhook script
├── seed_sample_data.py     # Sample retail dataset seeder
├── setup.sh                # 1-click environment setup script
├── requirements.txt        # Python package dependencies
├── .env.example            # Environment template
└── tests/
    └── test_suite.py       # Concurrency, barcode & Excel automated tests
```
