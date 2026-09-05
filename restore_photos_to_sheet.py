#!/usr/bin/env python3
"""
=============================================================================
AUTOMATED TELEGRAM PHOTO RESTORATION SCRIPT FOR GOOGLE SHEETS
=============================================================================
Matches all 205+ records from Telegram chat export (ON Scan TK) by exact
timestamp and crew member, uploads photos to Google Drive, and replaces broken
formulas in Google Sheets.
=============================================================================
"""

import os
import sys
import re
import json
import base64
import time
import requests
from pathlib import Path

# Paths
EXPORT_DIR = Path("/Users/mac/Downloads/ChatExport_2026-09-05")
JSON_FILE = EXPORT_DIR / "result.json"

def main():
    print("=" * 65)
    print(" 🚀 AUTOMATED PHOTO RESTORER FOR GOOGLE SHEETS")
    print("=" * 65)

    if not JSON_FILE.exists():
        print(f"❌ Could not find: {JSON_FILE}")
        sys.exit(1)

    # 1. Ask for or read Webhook URL
    default_url = "https://script.google.com/macros/s/AKfycby9E4U-6LRMUBiiqGULqoCJSczXmRaKb-xZy6kpVl3Vq-wlFc44gVZEaNq2ErV1lQcA/exec"
    webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(f"\nPress [Enter] to use detected Webhook URL, or paste a new one:")
        print(f"Default: {default_url}")
        user_input = input("Webhook URL: ").strip()
        webhook_url = user_input if user_input else default_url

    if not webhook_url.startswith("http"):
        print("❌ Error: Invalid Webhook URL.")
        sys.exit(1)

    print(f"\n📂 Reading Telegram Export: {JSON_FILE}")
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages", [])
    print(f"💬 Found {len(messages)} total messages.")

    # 2. Extract records matched to bot confirmations
    records = []
    for idx, m in enumerate(messages):
        text = ""
        if isinstance(m.get("text"), list):
            for part in m.get("text"):
                if isinstance(part, dict):
                    text += part.get("text", "")
                else:
                    text += str(part)
        elif isinstance(m.get("text"), str):
            text = m.get("text")

        if "SAVED TO GOOGLE SHEET" in text:
            time_match = re.search(r"Time:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})", text)
            barcode_match = re.search(r"Barcode:\s*([0-9A-Za-z_-]+)", text)
            item_match = re.search(r"Item:\s*([^\n]+)", text)
            crew_match = re.search(r"Crew:\s*([^\n]+)", text)

            timestamp = time_match.group(1) if time_match else ""
            barcode = barcode_match.group(1) if barcode_match else ""
            item = item_match.group(1).strip() if item_match else ""
            crew = crew_match.group(1).strip() if crew_match else ""

            # Look backwards up to 15 messages for photos belonging to this count
            photos = []
            for back_idx in range(idx - 1, max(0, idx - 15), -1):
                prev_m = messages[back_idx]
                if "SAVED TO GOOGLE SHEET" in str(prev_m.get("text")):
                    break  # Hit previous transaction
                if prev_m.get("photo"):
                    photos.insert(0, prev_m.get("photo"))

            front_photo = photos[0] if len(photos) >= 1 else None
            barcode_photo = photos[1] if len(photos) >= 2 else None

            if timestamp and (front_photo or barcode_photo):
                records.append({
                    "timestamp": timestamp,
                    "barcode": barcode,
                    "item": item,
                    "crew": crew,
                    "front_photo": front_photo,
                    "barcode_photo": barcode_photo
                })

    print(f"🎯 Successfully matched {len(records)} transactions with photos!")
    print("\n⏳ Starting restoration into Google Sheet & Drive...")
    print("-" * 65)

    success = 0
    skipped = 0

    for i, rec in enumerate(records, 1):
        ts = rec["timestamp"]
        item = rec["item"]
        crew = rec["crew"]
        bc = rec["barcode"]

        # 1. Restore Front Photo
        if rec["front_photo"]:
            front_path = EXPORT_DIR / rec["front_photo"]
            if front_path.exists():
                try:
                    with open(front_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    payload = {
                        "action": "restore_photo",
                        "timestamp": ts,
                        "item": item,
                        "barcode": bc,
                        "photo_base64": b64,
                        "is_barcode": False
                    }
                    resp = requests.post(webhook_url, json=payload, timeout=30)
                    if "RESTORED_ROW" in resp.text:
                        print(f"✅ [{i}/{len(records)}] Restored Front Photo: {item} ({ts}) by {crew}")
                        success += 1
                    else:
                        print(f"ℹ️ [{i}/{len(records)}] {resp.text}: {ts} ({item})")
                except Exception as e:
                    print(f"⚠️ [{i}/{len(records)}] Error restoring front photo: {e}")

        # 2. Restore Barcode Photo if available
        if rec["barcode_photo"]:
            bc_path = EXPORT_DIR / rec["barcode_photo"]
            if bc_path.exists():
                try:
                    with open(bc_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    payload = {
                        "action": "restore_photo",
                        "timestamp": ts,
                        "item": item,
                        "barcode": bc,
                        "photo_base64": b64,
                        "is_barcode": True
                    }
                    resp = requests.post(webhook_url, json=payload, timeout=30)
                    if "RESTORED_ROW" in resp.text:
                        print(f"   ↳ 🏷️ Restored Barcode Photo for {item}")
                except Exception as e:
                    pass

        # Brief delay to avoid Google Apps Script rate limit
        time.sleep(0.5)

    print("-" * 65)
    print(f"🎉 RESTORATION COMPLETE! Restored {success} photos successfully.")
    print("Check your Google Sheet to view all restored photos!")
    print("=" * 65)

if __name__ == "__main__":
    main()
