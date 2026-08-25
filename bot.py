"""
=============================================================================
STORE STOCK SCAN BOT — MULTI-USER GROUP & RAW MATERIAL SUPPORT
=============================================================================
- Full Multi-User Group Isolation (Each user has their own independent scan session)
- Direct Message Replies (Replies directly to the user's photo/text to avoid confusion)
- Fast 1-Shot Caption Support (e.g. caption photo with "G101 15" for instant save)
- Retail Goods: AI Packaging Reader & Barcode Scanner
- Raw Materials: Easy manual name entry & skip barcode
- Dual HD Photos: Pushes both Front & Barcode photos to Google Sheets
=============================================================================
"""

import os
import re
import uuid
import base64
import asyncio
import logging
import zoneinfo
from io import BytesIO
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, Tuple, List

from dotenv import load_dotenv
from PIL import Image, ImageEnhance, ImageOps

try:
    import zxingcpp
    HAS_ZXING = True
except ImportError:
    HAS_ZXING = False

import aiosqlite
import aiohttp
from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# ---------------------------------------------------------------------------
# 1. CONFIGURATION (Robust Environment Sanitization)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def clean_env(val: Optional[str]) -> str:
    if not val:
        return ""
    return val.strip().strip('"').strip("'").strip()

TELEGRAM_BOT_TOKEN = clean_env(
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAN_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)

GOOGLE_SHEET_WEBHOOK_URL = clean_env(
    os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
    or os.getenv("GOOGLE_SHEETS_WEBHOOK_URL")
    or os.getenv("SHEET_WEBHOOK_URL")
)

GEMINI_API_KEY = clean_env(
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
    or os.getenv("GEMINI_KEY")
)

DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "inventory.db"))
PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR", str(BASE_DIR / "photos")))
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bangkok")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("StockBot")

# Conversation States
STATE_BARCODE_PHOTO, STATE_SHELF, STATE_BARCODE, STATE_ITEM_NAME, STATE_QTY = range(5)


# ---------------------------------------------------------------------------
# 2. DATABASE LAYER
# ---------------------------------------------------------------------------
def get_current_timestamp() -> str:
    try:
        tz = zoneinfo.ZoneInfo(TIMEZONE)
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute("PRAGMA busy_timeout=10000;")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS counts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                crew_name TEXT NOT NULL,
                shelf TEXT NOT NULL,
                barcode TEXT NOT NULL,
                item_name TEXT,
                qty REAL NOT NULL DEFAULT 1,
                photo_front TEXT,
                photo_barcode TEXT,
                photo_front_url TEXT,
                photo_barcode_url TEXT,
                synced_sheet INTEGER NOT NULL DEFAULT 0
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_shelf ON counts(shelf);")
        await db.commit()


async def db_insert_count(
    user_id: int,
    crew_name: str,
    shelf: str,
    barcode: str,
    item_name: str,
    qty: float,
    photo_front: str = None,
    photo_barcode: str = None,
    photo_front_url: str = None,
    photo_barcode_url: str = None
) -> Dict[str, Any]:
    timestamp = get_current_timestamp()
    shelf_clean = (shelf or "UNKNOWN").strip().upper()
    barcode_clean = str(barcode or "NO_BARCODE").strip()
    item_name_clean = (item_name or "-").strip()

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO counts (
                timestamp, user_id, crew_name, shelf, barcode, item_name, qty,
                photo_front, photo_barcode, photo_front_url, photo_barcode_url, synced_sheet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                timestamp, user_id, crew_name, shelf_clean, barcode_clean, item_name_clean, qty,
                photo_front, photo_barcode, photo_front_url, photo_barcode_url
            )
        )
        row_id = cursor.lastrowid
        await db.commit()

    return {
        "id": row_id,
        "timestamp": timestamp,
        "user_id": user_id,
        "crew_name": crew_name,
        "shelf": shelf_clean,
        "barcode": barcode_clean,
        "item_name": item_name_clean,
        "qty": qty,
        "photo_front": photo_front,
        "photo_barcode": photo_barcode,
        "photo_front_url": photo_front_url,
        "photo_barcode_url": photo_barcode_url,
        "synced_sheet": 0
    }


async def db_mark_synced(count_ids: list):
    if not count_ids:
        return
    placeholders = ",".join("?" for _ in count_ids)
    async with get_db() as db:
        await db.execute(f"UPDATE counts SET synced_sheet = 1 WHERE id IN ({placeholders})", count_ids)
        await db.commit()


# ---------------------------------------------------------------------------
# 3. AI VISION: READS BRAND & PACKAGING FROM PHOTO
# ---------------------------------------------------------------------------
def compress_image_for_ai(image_path: str) -> Optional[str]:
    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img) or img
            img.thumbnail((768, 768), Image.Resampling.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.warning(f"Image compression error: {e}")
        return None


def get_gemini_models_list() -> List[str]:
    return [
        "models/gemini-3.5-flash",
        "models/gemini-3.5-flash-lite",
        "models/gemini-3.7-flash",
        "models/gemini-flash-latest",
        "models/gemini-2.5-flash"
    ]


async def extract_product_name_from_image(image_path: str) -> Optional[str]:
    if not image_path or not os.path.exists(image_path):
        return None

    api_key = clean_env(os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY)
    if not api_key:
        logger.warning("⚠️ GEMINI_API_KEY is not set.")
        return None

    img_b64 = await asyncio.to_thread(compress_image_for_ai, image_path)
    if not img_b64:
        return None

    prompt = (
        "Look at this product photo. Read the main printed product name and brand on the packaging. "
        "Return ONLY the concise Brand Name and Product Name (including flavor or size if visible, maximum 5 words). "
        "Do NOT include markdown, asterisks, quotes, bullet points, or filler words. "
        "Example: 'Jardo Seaweed Rice Chip' or 'Lay's Classic 50g' or 'Taro Fish Snack'. "
        "If it is a raw unbranded material or vegetable without text, return ONLY '-'."
    )

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": img_b64
                    }
                }
            ]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 30
        }
    }

    models = get_gemini_models_list()
    
    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            clean_name = text.strip().replace("\n", " ").replace("*", "").replace('"', '').strip()
                            if clean_name and len(clean_name) > 1 and clean_name != "-" and "unknown" not in clean_name.lower():
                                logger.info(f"✨ AI Vision ({model_name}) extracted name: '{clean_name}'")
                                return clean_name
                    elif resp.status in (404, 429):
                        continue
        except Exception as e:
            logger.debug(f"Gemini API ({model_name}) error: {e}")

    return None


async def test_gemini_api_connection() -> str:
    """Diagnostic tool to test Gemini API key health."""
    api_key = clean_env(os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY)
    if not api_key:
        return "❌ `GEMINI_API_KEY` is NOT set in Render Environment Variables!"

    key_preview = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "SHORT_KEY"
    models = get_gemini_models_list()

    payload = {
        "contents": [{
            "parts": [{"text": "Hello! Reply with only: OK"}]
        }]
    }

    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        res_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                        return (
                            f"✅ **Gemini AI API is Working 100%!**\n"
                            f"• Key: `{key_preview}`\n"
                            f"• Active Model: `{model_name.replace('models/', '')}`\n"
                            f"• Test Response: `{res_text}`\n"
                            f"• Status: `200 OK` 🎉"
                        )
        except Exception:
            continue

    return f"⚠️ **Could not connect to Gemini API with key `{key_preview}`.**"


# ---------------------------------------------------------------------------
# 4. BARCODE RECOGNITION (zxing-cpp + PIL)
# ---------------------------------------------------------------------------
def is_valid_retail_barcode(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    # Reject Indonesian BPOM / batch DataMatrix codes (e.g. (90)MD240935007100027)
    if t.startswith("(90)MD") or t.startswith("MD24") or t.startswith("MD 24") or t.startswith("(90)"):
        return False
    cleaned = re.sub(r"[^0-9A-Za-z\-_]", "", t)
    return len(cleaned) >= 4


def detect_barcode_from_image(image_path: str) -> Optional[str]:
    if not HAS_ZXING or not image_path or not os.path.exists(image_path):
        return None
    try:
        candidates = []
        with Image.open(image_path) as img:
            # 1. Original
            res = zxingcpp.read_barcodes(img)
            if res:
                candidates.extend(res)

            # 2. Grayscale
            img_norm = ImageOps.exif_transpose(img) or img
            gray = img_norm.convert("L")
            res_gray = zxingcpp.read_barcodes(gray)
            if res_gray:
                candidates.extend(res_gray)

            # 3. High Contrast
            enhancer = ImageEnhance.Contrast(gray)
            enhanced = enhancer.enhance(2.0)
            res_enh = zxingcpp.read_barcodes(enhanced)
            if res_enh:
                candidates.extend(res_enh)

        valid_codes = []
        for r in candidates:
            txt = (r.text or "").strip()
            if is_valid_retail_barcode(txt):
                fmt = str(r.format).upper()
                is_1d_retail = any(k in fmt for k in ["EAN", "UPC", "128", "39", "ITF"]) or (txt.isdigit() and len(txt) in (8, 12, 13, 14))
                valid_codes.append((0 if is_1d_retail else 1, txt))

        if valid_codes:
            # Sort by 1D priority first, then longest matching code
            valid_codes.sort(key=lambda x: (x[0], -len(x[1])))
            return valid_codes[0][1]
    except Exception as e:
        logger.warning(f"Barcode detection error: {e}")
    return None


# ---------------------------------------------------------------------------
# 5. GOOGLE SHEETS ASYNC BACKGROUND SYNC (Dual Photos)
# ---------------------------------------------------------------------------
class SheetsSyncManager:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or GOOGLE_SHEET_WEBHOOK_URL
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._is_running = False

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        self._worker_task = asyncio.create_task(self._process_queue())

    async def stop(self):
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    def enqueue(self, count_data: Dict[str, Any]):
        if not self.webhook_url:
            return
        self._queue.put_nowait(count_data)

    async def _send(self, data: Dict[str, Any]) -> bool:
        if not self.webhook_url or not self._session:
            return False
        payload = {
            "timestamp": data.get("timestamp", ""),
            "crew": data.get("crew_name", ""),
            "shelf": data.get("shelf", ""),
            "barcode": str(data.get("barcode", "")),
            "name": data.get("item_name", ""),
            "qty": data.get("qty", 1),
            "photo_front_url": data.get("photo_front_url", ""),
            "photo_barcode_url": data.get("photo_barcode_url", "")
        }
        for attempt in range(1, 4):
            try:
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    if resp.status in (200, 201, 302):
                        logger.info(f"✅ Google Sheet synced: {payload['barcode']} ({payload['name']})")
                        return True
            except Exception as e:
                logger.warning(f"Google Sheet sync attempt {attempt} failed: {e}")
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
        return False

    async def _process_queue(self):
        while self._is_running:
            try:
                item = await self._queue.get()
                success = await self._send(item)
                if success and "id" in item:
                    await db_mark_synced([item["id"]])
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sync queue error: {e}")
                await asyncio.sleep(1)


sync_manager = SheetsSyncManager()


# ---------------------------------------------------------------------------
# 6. FAST 1-SHOT CAPTION & INPUT PARSER
# ---------------------------------------------------------------------------
def parse_shelf_qty_text(text: str) -> Tuple[Optional[str], Optional[float]]:
    """Parses combined user input like 'R105 2' or 'G101 12' or 'A12 5.5' into (shelf, qty)."""
    if not text or not text.strip():
        return None, None
    tokens = text.strip().split()
    if not tokens:
        return None, None

    if len(tokens) >= 2:
        last_tok = tokens[-1].replace(",", ".")
        try:
            qty = float(last_tok)
            if qty > 0:
                shelf = " ".join(tokens[:-1]).strip().upper()
                return shelf, qty
        except ValueError:
            pass

    if len(tokens) == 1:
        return tokens[0].strip().upper(), None

    return text.strip().upper(), None


def parse_quick_caption(caption: str, detected_barcode: Optional[str] = None) -> Optional[Tuple[str, str, str, float]]:
    if not caption or not caption.strip():
        return None
    tokens = caption.strip().split()
    if not tokens:
        return None

    shelf = "UNKNOWN"
    barcode = detected_barcode or "NO_BARCODE"
    item_name = "-"
    qty = 1.0

    if len(tokens) == 1 and tokens[0].replace(".", "", 1).isdigit():
        qty = float(tokens[0])
        return shelf, barcode, item_name, qty

    last_token = tokens[-1]
    if last_token.replace(".", "", 1).isdigit():
        qty = float(last_token)
        tokens = tokens[:-1]

    if not tokens:
        return shelf, barcode, item_name, qty

    if re.match(r"^[A-Za-z0-9\-_]{2,8}$", tokens[0]) and not tokens[0].isdigit():
        shelf = tokens[0].upper()
        tokens = tokens[1:]

    if not tokens:
        return shelf, barcode, item_name, qty

    if tokens[0].isdigit() and len(tokens[0]) >= 6:
        barcode = tokens[0]
        tokens = tokens[1:]

    if tokens:
        item_name = " ".join(tokens)

    return shelf, barcode, item_name, qty


# ---------------------------------------------------------------------------
# 7. TELEGRAM HANDLERS
# ---------------------------------------------------------------------------
def get_user_display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Unknown"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if user.username:
        return f"{full_name} (@{user.username})" if full_name else f"@{user.username}"
    return full_name or f"User_{user.id}"


def get_photo_file_id(update: Update) -> Optional[str]:
    if update.message and update.message.photo:
        return update.message.photo[-1].file_id
    if update.message and update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith("image/"):
            return doc.file_id
        if doc.file_name and any(doc.file_name.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            return doc.file_id
    return None


async def safe_reply(update: Update, text: str, reply_markup=None) -> Any:
    """Helper to always reply directly to the specific user's message in groups."""
    target = update.callback_query.message if update.callback_query else update.message
    if not target:
        return None
    try:
        user_tag = f"👤 *{get_user_display_name(update)}:*\n"
        full_text = f"{user_tag}{text}"
        return await target.reply_text(
            full_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Reply error: {e}")
        return await target.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def save_tg_photo(file_id: str, context: ContextTypes.DEFAULT_TYPE, prefix: str = "img") -> Tuple[str, str]:
    try:
        tg_file = await context.bot.get_file(file_id)
        filename = f"{prefix}_{uuid.uuid4().hex[:8]}.jpg"
        file_path = str(PHOTOS_DIR / filename)
        await tg_file.download_to_drive(custom_path=file_path)
        
        raw_path = tg_file.file_path or ""
        if raw_path.startswith("http"):
            photo_url = raw_path
        elif raw_path:
            token = clean_env(os.getenv("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN)
            photo_url = f"https://api.telegram.org/file/bot{token}/{raw_path.lstrip('/')}"
        else:
            photo_url = ""
            
        return file_path, photo_url
    except Exception as e:
        logger.error(f"Error saving photo: {e}")
        return "", ""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"👋 *Store Stock Count Bot Active!*\n\n"
        f"👉 *How to count items:*\n"
        f"1️⃣ **Send a photo of the product front or raw material** 📸\n"
        f"2️⃣ Send the barcode photo (or tap Skip)\n"
        f"3️⃣ Type Shelf (e.g. `G101`, `RM-01`)\n"
        f"4️⃣ Type Quantity (e.g. `12`)\n\n"
        f"⚡ *Tip: In busy groups, caption your photo with `Shelf QTY` (e.g. `G101 10`) for instant 1-shot save!*"
    )
    await safe_reply(update, msg, reply_markup=ReplyKeyboardRemove())


async def cmd_testai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await safe_reply(update, "⏳ *Testing Gemini AI Vision API connection...*")
    report = await test_gemini_api_connection()
    if msg:
        await msg.edit_text(report, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# 8. PHOTO FLOW (Retail Goods & Raw Materials) + ALBUM / MEDIA GROUP SUPPORT
# ---------------------------------------------------------------------------
MEDIA_GROUP_CACHE: Dict[str, List[Update]] = {}
MEDIA_GROUP_LOCK = asyncio.Lock()


def assign_photos_front_and_barcode(
    context: ContextTypes.DEFAULT_TYPE,
    p1_path: str, p1_url: str,
    p2_path: str, p2_url: str,
    p1_has_barcode: bool, p2_has_barcode: bool,
    p1_has_name: bool, p2_has_name: bool
):
    """Accurately places the front packaging photo in Front column and barcode in Barcode column."""
    if p1_has_barcode and not p2_has_barcode:
        context.user_data["photo_barcode_path"] = p1_path
        context.user_data["photo_barcode_url"] = p1_url
        context.user_data["photo_front_path"] = p2_path
        context.user_data["photo_front_url"] = p2_url
    elif p2_has_barcode and not p1_has_barcode:
        context.user_data["photo_barcode_path"] = p2_path
        context.user_data["photo_barcode_url"] = p2_url
        context.user_data["photo_front_path"] = p1_path
        context.user_data["photo_front_url"] = p1_url
    elif p2_has_name and not p1_has_name:
        context.user_data["photo_front_path"] = p2_path
        context.user_data["photo_front_url"] = p2_url
        context.user_data["photo_barcode_path"] = p1_path
        context.user_data["photo_barcode_url"] = p1_url
    else:
        context.user_data["photo_front_path"] = p1_path
        context.user_data["photo_front_url"] = p1_url
        context.user_data["photo_barcode_path"] = p2_path
        context.user_data["photo_barcode_url"] = p2_url


async def handle_incoming_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message:
        return ConversationHandler.END

    media_group_id = message.media_group_id

    if media_group_id:
        async with MEDIA_GROUP_LOCK:
            if media_group_id in MEDIA_GROUP_CACHE:
                MEDIA_GROUP_CACHE[media_group_id].append(update)
                # Return state (do NOT return None) so ConversationHandler does not trigger fallbacks
                return STATE_BARCODE_PHOTO
            else:
                MEDIA_GROUP_CACHE[media_group_id] = [update]

        # Wait for all photos in the album to arrive
        await asyncio.sleep(1.2)

        async with MEDIA_GROUP_LOCK:
            album_updates = MEDIA_GROUP_CACHE.pop(media_group_id, [update])

        if len(album_updates) > 1:
            return await process_album_photos(album_updates, context)
        else:
            return await process_single_photo(album_updates[0], context)
    else:
        # Check if user is sending a 2nd photo in existing session
        if context.user_data.get("photo1_path") and not context.user_data.get("photo_barcode_path"):
            return await flow_receive_barcode_photo(update, context)
        return await process_single_photo(update, context)


async def process_album_photos(album_updates: List[Update], context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    first_update = album_updates[0]
    user = first_update.effective_user
    crew_name = get_user_display_name(first_update)

    file_id_1 = get_photo_file_id(album_updates[0])
    file_id_2 = get_photo_file_id(album_updates[1])

    if not file_id_1 or not file_id_2:
        return await process_single_photo(first_update, context)

    # Download Photo 1 and Photo 2 concurrently
    (p1_path, p1_url), (p2_path, p2_url) = await asyncio.gather(
        save_tg_photo(file_id_1, context, prefix="p1"),
        save_tg_photo(file_id_2, context, prefix="p2")
    )

    # Scan BOTH photos for Barcode and AI Name in parallel
    p1_name, p2_name, p1_barcode, p2_barcode = await asyncio.gather(
        extract_product_name_from_image(p1_path),
        extract_product_name_from_image(p2_path),
        asyncio.to_thread(detect_barcode_from_image, p1_path),
        asyncio.to_thread(detect_barcode_from_image, p2_path)
    )

    detected_barcode = p2_barcode or p1_barcode
    detected_name = None
    if p1_name and p1_name != "-":
        detected_name = p1_name
    elif p2_name and p2_name != "-":
        detected_name = p2_name

    assign_photos_front_and_barcode(
        context, p1_path, p1_url, p2_path, p2_url,
        bool(p1_barcode), bool(p2_barcode),
        bool(p1_name and p1_name != "-"), bool(p2_name and p2_name != "-")
    )

    if detected_name:
        context.user_data["item_name"] = detected_name
    else:
        context.user_data["item_name"] = "-"

    if detected_barcode:
        context.user_data["detected_barcode"] = detected_barcode
        context.user_data["barcode"] = detected_barcode

    # Check for 1-shot caption
    caption = ""
    for u in album_updates:
        if u.message and u.message.caption:
            caption = u.message.caption.strip()
            break

    quick_data = parse_quick_caption(caption, detected_barcode=detected_barcode)
    if quick_data:
        shelf, barcode, name, qty = quick_data
        if name == "-" and detected_name and detected_name != "-":
            name = detected_name

        record = await db_insert_count(
            user_id=user.id if user else 0,
            crew_name=crew_name,
            shelf=shelf,
            barcode=barcode,
            item_name=name,
            qty=qty,
            photo_front=context.user_data.get("photo_front_path"),
            photo_barcode=context.user_data.get("photo_barcode_path"),
            photo_front_url=context.user_data.get("photo_front_url"),
            photo_barcode_url=context.user_data.get("photo_barcode_url")
        )
        sync_manager.enqueue(record)
        qty_display = int(qty) if qty.is_integer() else qty

        await safe_reply(
            first_update,
            f"⚡ *SAVED TO GOOGLE SHEET!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Shelf:* `{shelf}`\n"
            f"🏷️ *Barcode:* `{barcode}`\n"
            f"📦 *Item:* {name}\n"
            f"🔢 *Quantity:* `{qty_display}`\n"
            f"🕒 *Time:* `{record['timestamp']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 *Send next photo to continue!*"
        )
        context.user_data.clear()
        return ConversationHandler.END

    name_status = f"\n📦 *Item:* `{detected_name}` (Auto-detected ✨)" if detected_name and detected_name != "-" else ""
    barcode_status = f"\n🏷️ *Barcode:* `{detected_barcode}`" if detected_barcode else ""

    await safe_reply(
        first_update,
        f"📸 *2 Photos Received! (Front & Barcode)*{name_status}{barcode_status}\n\n"
        f"📍 *Please type Shelf Code* (e.g. `G101` or `R105`):"
    )
    return STATE_SHELF


async def process_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    crew_name = get_user_display_name(update)

    file_id = get_photo_file_id(update)
    if not file_id:
        return ConversationHandler.END

    file_path, photo_url = await save_tg_photo(file_id, context, prefix="p1")
    context.user_data["photo1_path"] = file_path
    context.user_data["photo1_url"] = photo_url
    context.user_data["photo_front_path"] = file_path
    context.user_data["photo_front_url"] = photo_url

    # Check both barcode and name on Photo 1
    detected_barcode, detected_name = await asyncio.gather(
        asyncio.to_thread(detect_barcode_from_image, file_path),
        extract_product_name_from_image(file_path)
    )

    context.user_data["photo1_barcode"] = detected_barcode
    context.user_data["photo1_name"] = detected_name

    if detected_barcode:
        context.user_data["detected_barcode"] = detected_barcode
        context.user_data["barcode"] = detected_barcode

    if detected_name:
        context.user_data["item_name"] = detected_name

    caption = update.message.caption or ""
    quick_data = parse_quick_caption(caption, detected_barcode=detected_barcode)

    if quick_data:
        shelf, barcode, name, qty = quick_data
        if name == "-" and detected_name:
            name = detected_name

        record = await db_insert_count(
            user_id=user.id if user else 0,
            crew_name=crew_name,
            shelf=shelf,
            barcode=barcode,
            item_name=name,
            qty=qty,
            photo_front=file_path,
            photo_barcode=None,
            photo_front_url=photo_url,
            photo_barcode_url=""
        )
        sync_manager.enqueue(record)
        qty_display = int(qty) if qty.is_integer() else qty

        await safe_reply(
            update,
            f"⚡ *SAVED TO GOOGLE SHEET!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Shelf:* `{shelf}`\n"
            f"🏷️ *Barcode:* `{barcode}`\n"
            f"📦 *Item:* {name}\n"
            f"🔢 *Quantity:* `{qty_display}`\n"
            f"🕒 *Time:* `{record['timestamp']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 *Send next photo to continue!*"
        )
        context.user_data.clear()
        return ConversationHandler.END

    name_status = f"\n📦 *Item:* `{detected_name}` (Auto-detected ✨)" if detected_name and detected_name != "-" else ""
    barcode_status = f"\n🏷️ *Barcode:* `{detected_barcode}`" if detected_barcode else ""

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip (No Barcode / Raw Material)", callback_data="skip_barcode_photo")]
    ])
    await safe_reply(
        update,
        f"📸 *Photo 1 Received!*{name_status}{barcode_status}\n\n"
        f"📷 Send **Photo 2** (Or type Shelf e.g. `G101`):",
        reply_markup=kb
    )
    return STATE_BARCODE_PHOTO


async def flow_receive_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file_id = get_photo_file_id(update)
    if not file_id:
        return await prompt_shelf_step(update, context)

    p2_path, p2_url = await save_tg_photo(file_id, context, prefix="p2")
    p1_path = context.user_data.get("photo1_path") or p2_path
    p1_url = context.user_data.get("photo1_url") or p2_url

    p2_barcode, p2_name = await asyncio.gather(
        asyncio.to_thread(detect_barcode_from_image, p2_path),
        extract_product_name_from_image(p2_path) if not context.user_data.get("item_name") else asyncio.sleep(0)
    )

    p1_barcode = context.user_data.get("photo1_barcode")
    p1_name = context.user_data.get("photo1_name") or context.user_data.get("item_name")

    final_barcode = p2_barcode or p1_barcode or context.user_data.get("detected_barcode")
    final_name = (p2_name if isinstance(p2_name, str) and p2_name != "-" else None) or p1_name or context.user_data.get("item_name")

    if final_barcode:
        context.user_data["detected_barcode"] = final_barcode
        context.user_data["barcode"] = final_barcode

    if final_name:
        context.user_data["item_name"] = final_name

    assign_photos_front_and_barcode(
        context, p1_path, p1_url, p2_path, p2_url,
        bool(p1_barcode), bool(p2_barcode), bool(p1_name), bool(p2_name and isinstance(p2_name, str))
    )

    name_status = f"\n📦 *Item:* `{context.user_data.get('item_name')}`" if context.user_data.get("item_name") and context.user_data.get("item_name") != "-" else ""
    barcode_status = f"\n🏷️ *Barcode:* `{context.user_data.get('detected_barcode')}`" if context.user_data.get("detected_barcode") else ""

    await safe_reply(
        update,
        f"📸 *2 Photos Received! (Front & Barcode)*{name_status}{barcode_status}\n\n"
        f"📍 *Please type Shelf Code* (e.g. `G101` or `R105`):"
    )
    return STATE_SHELF


async def flow_skip_barcode_photo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await prompt_shelf_step(update, context)


async def prompt_shelf_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_reply(
        update,
        "📍 *Please type Shelf Code* (e.g. `G101` or `R105`):"
    )
    return STATE_SHELF


async def flow_shelf_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_text = update.message.text.strip() if update.message and update.message.text else ""
    if not raw_text:
        await safe_reply(update, "⚠️ Please type the Shelf Code (e.g. `G101` or `G101 1`):")
        return STATE_SHELF

    shelf, quick_qty = parse_shelf_qty_text(raw_text)
    context.user_data["shelf"] = shelf or raw_text.upper()

    if quick_qty is not None:
        context.user_data["qty"] = quick_qty
        detected_barcode = context.user_data.get("detected_barcode") or context.user_data.get("barcode")

        if detected_barcode and detected_barcode != "NO_BARCODE":
            context.user_data["barcode"] = detected_barcode
            return await finalize_and_save_count(update, context)
        else:
            return await check_barcode_step(update, context)

    # Only shelf was typed (e.g. "G101")
    detected_barcode = context.user_data.get("detected_barcode") or context.user_data.get("barcode")
    if detected_barcode and detected_barcode != "NO_BARCODE":
        context.user_data["barcode"] = detected_barcode
        await safe_reply(
            update,
            f"🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):"
        )
        return STATE_QTY

    return await check_barcode_step(update, context)


async def check_barcode_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    detected_barcode = context.user_data.get("detected_barcode")

    if detected_barcode:
        context.user_data["barcode"] = detected_barcode
        if not context.user_data.get("qty"):
            await safe_reply(
                update,
                f"🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):"
            )
            return STATE_QTY
        return await finalize_and_save_count(update, context)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip Barcode (Raw Material / No Code)", callback_data="skip_barcode_num")]
    ])
    await safe_reply(
        update,
        "🏷️ Type **Barcode numbers** (Or tap Skip if Raw Material):",
        reply_markup=kb
    )
    return STATE_BARCODE


async def flow_barcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "skip_barcode_num":
        context.user_data["barcode"] = "NO_BARCODE"
        if not context.user_data.get("qty"):
            await safe_reply(update, "🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):")
            return STATE_QTY
        return await finalize_and_save_count(update, context)
    return STATE_BARCODE


async def flow_barcode_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    barcode = update.message.text.strip()
    context.user_data["barcode"] = barcode or "NO_BARCODE"
    if not context.user_data.get("qty"):
        await safe_reply(update, "🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):")
        return STATE_QTY
    return await finalize_and_save_count(update, context)


async def check_item_name_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current_name = context.user_data.get("item_name")

    if current_name and current_name != "-" and len(current_name.strip()) > 1:
        if not context.user_data.get("qty"):
            await safe_reply(
                update,
                f"🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):"
            )
            return STATE_QTY
        return await finalize_and_save_count(update, context)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip Name", callback_data="skip_item_name")]
    ])
    await safe_reply(
        update,
        "📦 *Please type the Item Name:*\n_(e.g. Sugar 50kg, Raw Cashew, Flour Bag)_",
        reply_markup=kb
    )
    return STATE_ITEM_NAME


async def flow_item_name_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "skip_item_name":
        context.user_data["item_name"] = "-"
        if not context.user_data.get("qty"):
            await safe_reply(update, "🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):")
            return STATE_QTY
        return await finalize_and_save_count(update, context)
    return STATE_ITEM_NAME


async def flow_item_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    context.user_data["item_name"] = name or "-"
    if not context.user_data.get("qty"):
        await safe_reply(update, "🔢 *Please type Quantity (QTY)* (e.g. `1`, `5`, `12`):")
        return STATE_QTY
    return await finalize_and_save_count(update, context)


async def flow_qty_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip() if update.message and update.message.text else ""
    # Check if user typed combined "G101 1" at qty prompt
    shelf, parsed_qty = parse_shelf_qty_text(text)
    if parsed_qty is not None:
        context.user_data["qty"] = parsed_qty
        if shelf:
            context.user_data["shelf"] = shelf
        return await finalize_and_save_count(update, context)

    try:
        qty = float(text)
        if qty <= 0:
            await safe_reply(update, "⚠️ Quantity must be greater than 0. Please type a number (e.g. 12):")
            return STATE_QTY
        context.user_data["qty"] = qty
        return await finalize_and_save_count(update, context)
    except ValueError:
        await safe_reply(update, "⚠️ Please enter a valid number (e.g. `1` or `5`):")
        return STATE_QTY


async def finalize_and_save_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    crew_name = get_user_display_name(update)
    shelf = context.user_data.get("shelf", "UNKNOWN")
    barcode = context.user_data.get("barcode", "NO_BARCODE")
    item_name = context.user_data.get("item_name") or "-"
    qty = context.user_data.get("qty", 1.0)
    
    photo_front = context.user_data.get("photo_front_path") or context.user_data.get("photo1_path")
    photo_barcode = context.user_data.get("photo_barcode_path")
    photo_front_url = context.user_data.get("photo_front_url") or context.user_data.get("photo1_url", "")
    photo_barcode_url = context.user_data.get("photo_barcode_url", "")

    record = await db_insert_count(
        user_id=user.id if user else 0,
        crew_name=crew_name,
        shelf=shelf,
        barcode=barcode,
        item_name=item_name,
        qty=qty,
        photo_front=photo_front,
        photo_barcode=photo_barcode,
        photo_front_url=photo_front_url,
        photo_barcode_url=photo_barcode_url
    )

    sync_manager.enqueue(record)
    qty_display = int(qty) if qty.is_integer() else qty

    card = (
        f"✅ *SAVED TO GOOGLE SHEET! (ID #{record['id']})*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Shelf:* `{shelf}`\n"
        f"🏷️ *Barcode:* `{barcode}`\n"
        f"📦 *Item:* {item_name}\n"
        f"🔢 *Quantity:* `{qty_display}`\n"
        f"🕒 *Time:* `{record['timestamp']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Send next photo to continue!*"
    )
    await safe_reply(update, card)
    context.user_data.clear()
    return ConversationHandler.END


async def flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await safe_reply(update, "❌ Cancelled. Send a new photo anytime!")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)


# ---------------------------------------------------------------------------
# 9. LIGHTWEIGHT HTTP HEALTH SERVER
# ---------------------------------------------------------------------------
async def handle_health_check(request):
    if not TELEGRAM_BOT_TOKEN:
        return web.Response(
            text="⚠️ Bot server is online, but TELEGRAM_BOT_TOKEN is missing in Render Environment Variables!",
            content_type="text/plain",
            status=200
        )
    return web.Response(
        text="✅ Store Stock Count Telegram Bot is active and running 24/7!",
        content_type="text/plain",
        status=200
    )


# ---------------------------------------------------------------------------
# 10. MAIN RUNNER
# ---------------------------------------------------------------------------
async def main_async():
    logger.info("Initializing SQLite database...")
    await init_db()
    await sync_manager.start()

    app_web = web.Application()
    app_web.router.add_get("/", handle_health_check)
    app_web.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 Web health server listening on port {PORT}")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ ERROR: TELEGRAM_BOT_TOKEN is not set in Environment Variables!")
        while True:
            await asyncio.sleep(3600)

    logger.info("🤖 Starting Telegram Bot polling...")
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    photo_filter = filters.PHOTO | (filters.Document.IMAGE & ~filters.COMMAND)

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(photo_filter, handle_incoming_photo),
            CommandHandler("count", lambda u, c: u.message.reply_text("📸 Send a photo of the product front:"))
        ],
        states={
            STATE_BARCODE_PHOTO: [
                MessageHandler(photo_filter, flow_receive_barcode_photo),
                CallbackQueryHandler(flow_skip_barcode_photo_cb, pattern="^skip_barcode_photo$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_shelf_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_SHELF: [
                MessageHandler(photo_filter, flow_receive_barcode_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_shelf_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_BARCODE: [
                MessageHandler(photo_filter, handle_incoming_photo),
                CallbackQueryHandler(flow_barcode_callback, pattern="^skip_barcode_num$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_barcode_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_ITEM_NAME: [
                MessageHandler(photo_filter, handle_incoming_photo),
                CallbackQueryHandler(flow_item_name_callback, pattern="^skip_item_name$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_item_name_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_QTY: [
                MessageHandler(photo_filter, handle_incoming_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_qty_text),
                CommandHandler("cancel", flow_cancel)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", flow_cancel)
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True
    )

    tg_app.add_handler(conv)
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_start))
    tg_app.add_handler(CommandHandler("testai", cmd_testai))
    tg_app.add_error_handler(error_handler)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    logger.info("🎉 Bot is online and listening for messages!")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await sync_manager.stop()
        await runner.cleanup()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
