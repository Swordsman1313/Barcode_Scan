#!/usr/bin/env python3
"""
=============================================================================
AUTOMATED TELEGRAM PHOTO RESTORATION SCRIPT FOR GOOGLE SHEETS
=============================================================================
This script restores all expired photos in your Google Sheet automatically!
It matches photos exported from Telegram Desktop with rows in Google Sheets
by timestamp, uploads them directly to Google Drive, and updates the formulas.
=============================================================================
"""

import os
import sys
import json
import base64
import glob
import requests
from datetime import datetime
from pathlib import Path

def find_telegram_export_dir():
    """Scans ~/Downloads for Telegram Desktop export folder."""
    downloads = Path.home() / "Downloads"
    patterns = [
        str(downloads / "Telegram Desktop" / "ChatExport_*"),
        str(downloads / "ChatExport_*"),
        str(downloads / "*" / "ChatExport_*"),
        str(Path.cwd() / "ChatExport_*"),
    ]
    matches = []
    for pattern in patterns:
        matches.extend(glob.glob(pattern))
    
    if not matches:
        return None
    # Pick the newest one
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return Path(matches[0])


def main():
    print("=" * 65)
    print(" 🚀 AUTOMATED TELEGRAM PHOTO RESTORER FOR GOOGLE SHEETS")
    print("=" * 65)
    
    # 1. Ask for Webhook URL
    webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("\nPlease enter your Google Apps Script Webhook URL:")
        print("(From Google Sheets -> Extensions -> Apps Script -> Deploy -> Web app URL)")
        webhook_url = input("Webhook URL: ").strip()
        
    if not webhook_url or not webhook_url.startswith("http"):
        print("❌ Error: Invalid Webhook URL.")
        sys.exit(1)

    # 2. Locate Telegram export directory
    export_dir = find_telegram_export_dir()
    if not export_dir:
        print("\n🔍 Looking for exported Telegram chat folder...")
        print("Could not auto-detect ~/Downloads/Telegram Desktop/ChatExport_...")
        user_path = input("Please paste the path to your ChatExport folder: ").strip().strip("'\"")
        export_dir = Path(user_path)

    json_file = export_dir / "result.json"
    if not json_file.exists():
        print(f"❌ Error: Could not find 'result.json' in {export_dir}")
        print("Please make sure you exported the chat in 'Machine-readable JSON' format.")
        sys.exit(1)

    print(f"\n📂 Found export folder: {export_dir}")
    print(f"📄 Reading {json_file.name}...")

    with open(json_file, "r", encoding="utf-8") as f:
        chat_data = json.load(f)

    messages = chat_data.get("messages", [])
    print(f"💬 Total messages found: {len(messages)}")

    # Filter messages that have photos
    photo_msgs = [m for m in messages if m.get("photo")]
    print(f"📸 Total photos to restore: {len(photo_msgs)}")

    if not photo_msgs:
        print("⚠️ No photo messages found in export.")
        sys.exit(0)

    print("\n⏳ Restoring photos into Google Sheet... (Please keep this window open)")
    print("-" * 65)

    success_count = 0
    fail_count = 0

    # Group photos by minute and sender to handle Front + Barcode pairs
    user_time_counts = {}

    for idx, msg in enumerate(photo_msgs, 1):
        raw_date = msg.get("date", "")
        sender = msg.get("from", "Unknown")
        rel_photo_path = msg.get("photo", "")
        full_photo_path = export_dir / rel_photo_path

        if not full_photo_path.exists():
            continue

        # Format date to match Google Sheets: YYYY-MM-DD HH:mm:ss
        try:
            # Telegram format: "2026-09-01T14:07:32"
            dt = datetime.fromisoformat(raw_date)
            formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
            time_key = f"{sender}_{dt.strftime('%Y-%m-%d %H:%M')}"
        except Exception:
            formatted_date = raw_date
            time_key = f"{sender}_{raw_date}"

        order_in_group = user_time_counts.get(time_key, 0)
        user_time_counts[time_key] = order_in_group + 1
        is_barcode = (order_in_group > 0) # 1st is front, 2nd is barcode

        photo_type = "Barcode Photo" if is_barcode else "Front Photo"

        # Read and encode photo
        try:
            with open(full_photo_path, "rb") as pf:
                b64_content = base64.b64encode(pf.read()).decode("utf-8")
        except Exception as e:
            print(f"⚠️ [{idx}/{len(photo_msgs)}] Error reading photo file {rel_photo_path}: {e}")
            fail_count += 1
            continue

        payload = {
            "action": "restore_photo",
            "timestamp": formatted_date,
            "photo_base64": b64_content,
            "is_barcode": is_barcode
        }

        try:
            resp = requests.post(webhook_url, json=payload, timeout=30)
            res_text = resp.text.strip()
            if "RESTORED_ROW" in res_text:
                row_num = res_text.split("_")[-1]
                print(f"✅ [{idx}/{len(photo_msgs)}] Row {row_num}: Restored {photo_type} for {sender} ({formatted_date})")
                success_count += 1
            elif "ROW_NOT_FOUND" in res_text:
                # Try matching by time prefix
                print(f"ℹ️ [{idx}/{len(photo_msgs)}] No exact row match for {formatted_date} ({sender})")
            else:
                print(f"⚠️ [{idx}/{len(photo_msgs)}] Server replied: {res_text}")
        except Exception as e:
            print(f"❌ [{idx}/{len(photo_msgs)}] Network error for {formatted_date}: {e}")
            fail_count += 1

    print("-" * 65)
    print(f"🎉 RESTORATION COMPLETE!")
    print(f"✅ Successfully restored: {success_count} photos")
    if fail_count > 0:
        print(f"⚠️ Skipped/Failed: {fail_count} photos")
    print("=" * 65)


if __name__ == "__main__":
    main()
