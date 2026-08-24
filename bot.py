"""
=============================================================================
STORE STOCK SCAN BOT — AI VISION PACKAGING + PHOTO PUSH TO GOOGLE SHEETS
=============================================================================
- AI Vision Product Name (Gemini REST API with camelCase inlineData & mimeType)
- Direct Product Photo Push into Google Sheets (=IMAGE)
- Auto Barcode Scanner (zxing-cpp)
- Instant 3-Step Flow: Photo -> Shelf -> QTY -> Synced
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
from typing import Optional, Dict, Any, Tuple

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
# 1. CONFIGURATION
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "")
    or os.getenv("TELEGRAN_BOT_TOKEN", "")
    or os.getenv("BOT_TOKEN", "")
).strip()

GOOGLE_SHEET_WEBHOOK_URL = (
    os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "")
    or os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "")
    or os.getenv("SHEET_WEBHOOK_URL", "")
).strip()

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY", "")
    or os.getenv("GOOGLE_API_KEY", "")
    or os.getenv("GEMINI_KEY", "")
).strip()

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
STATE_BARCODE_PHOTO, STATE_SHELF, STATE_BARCODE, STATE_QTY = range(4)


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
                photo_url TEXT,
                synced_sheet INTEGER NOT NULL DEFAULT 0
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_shelf ON counts(shelf);")
        await db.commit()


async def db_insert_count(user_id: int, crew_name: str, shelf: str, barcode: str, item_name: str, qty: float, photo_front: str = None, photo_barcode: str = None, photo_url: str = None) -> Dict[str, Any]:
    timestamp = get_current_timestamp()
    shelf_clean = (shelf or "UNKNOWN").strip().upper()
    barcode_clean = str(barcode or "NO_BARCODE").strip()
    item_name_clean = (item_name or "-").strip()

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO counts (
                timestamp, user_id, crew_name, shelf, barcode, item_name, qty, photo_front, photo_barcode, photo_url, synced_sheet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (timestamp, user_id, crew_name, shelf_clean, barcode_clean, item_name_clean, qty, photo_front, photo_barcode, photo_url)
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
        "photo_url": photo_url,
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
# 3. AI VISION PRODUCT NAME EXTRACTOR (Fixed CamelCase REST Spec)
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


async def extract_product_name_from_image(image_path: str) -> Optional[str]:
    if not image_path or not os.path.exists(image_path):
        return None

    api_key = GEMINI_API_KEY
    if not api_key:
        logger.info("ℹ️ GEMINI_API_KEY is not set.")
        return None

    img_b64 = await asyncio.to_thread(compress_image_for_ai, image_path)
    if not img_b64:
        return None

    prompt = (
        "Identify and extract ONLY the Brand Name and Product Name (with flavor or size if visible). "
        "Return concise name in maximum 5 words. Do NOT include markdown, punctuation, or filler. "
        "Example: 'Jardo Seaweed Rice Chip' or 'Lay's Classic 50g'."
    )

    models = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-pro"]
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
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
            }]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            clean_name = text.strip().replace("\n", " ").replace("*", "").replace('"', '').strip()
                            if clean_name and len(clean_name) > 2:
                                logger.info(f"✨ AI Vision extracted product name: '{clean_name}'")
                                return clean_name
                    elif resp.status == 404:
                        continue
                    else:
                        err_text = await resp.text()
                        logger.warning(f"Gemini API ({model}) status {resp.status}: {err_text}")
        except Exception as e:
            logger.warning(f"Error calling Gemini API ({model}): {e}")

    return None


# ---------------------------------------------------------------------------
# 4. BARCODE RECOGNITION (zxing-cpp + PIL)
# ---------------------------------------------------------------------------
def detect_barcode_from_image(image_path: str) -> Optional[str]:
    if not HAS_ZXING or not image_path or not os.path.exists(image_path):
        return None
    try:
        with Image.open(image_path) as img:
            results = zxingcpp.read_barcodes(img)
            if results:
                for r in results:
                    if r.text and r.text.strip():
                        return r.text.strip()

            img_norm = ImageOps.exif_transpose(img) or img
            gray = img_norm.convert("L")
            results = zxingcpp.read_barcodes(gray)
            if results:
                for r in results:
                    if r.text and r.text.strip():
                        return r.text.strip()

            enhancer = ImageEnhance.Contrast(gray)
            enhanced = enhancer.enhance(2.0)
            results = zxingcpp.read_barcodes(enhanced)
            if results:
                for r in results:
                    if r.text and r.text.strip():
                        return r.text.strip()
    except Exception as e:
        logger.warning(f"Barcode detection error: {e}")
    return None


# ---------------------------------------------------------------------------
# 5. GOOGLE SHEETS ASYNC BACKGROUND SYNC (With Photo URL Support)
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
            "photo_url": data.get("photo_url", "")
        }
        for attempt in range(1, 4):
            try:
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    if resp.status in (200, 201, 302):
                        logger.info(f"✅ Google Sheet synced row: {payload['barcode']} ({payload['name']})")
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
# 6. FAST 1-SHOT CAPTION
# ---------------------------------------------------------------------------
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


async def save_tg_photo(file_id: str, context: ContextTypes.DEFAULT_TYPE, prefix: str = "img") -> Tuple[str, str]:
    try:
        tg_file = await context.bot.get_file(file_id)
        filename = f"{prefix}_{uuid.uuid4().hex[:8]}.jpg"
        file_path = str(PHOTOS_DIR / filename)
        await tg_file.download_to_drive(custom_path=file_path)
        
        # Build direct telegram photo link if file_path available
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_file.file_path}" if tg_file.file_path else ""
        return file_path, photo_url
    except Exception as e:
        logger.error(f"Error saving photo: {e}")
        return "", ""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"👋 *Store Stock Count Bot Active!*\n\n"
        f"👉 *How to count an item:*\n"
        f"1️⃣ **Send a photo of the product front** 📸\n"
        f"2️⃣ Send barcode photo (or tap Skip)\n"
        f"3️⃣ Type Shelf (e.g. `G101`)\n"
        f"4️⃣ Type Quantity (e.g. `12`)\n\n"
        f"⚡ *Data auto-syncs instantly to Google Sheets!*"
    )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )


# ---------------------------------------------------------------------------
# 8. PHOTO FLOW (Photo -> Shelf -> Quantity -> Done!)
# ---------------------------------------------------------------------------
async def handle_incoming_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    crew_name = get_user_display_name(update)

    photo = update.message.photo[-1]
    file_path, photo_url = await save_tg_photo(photo.file_id, context, prefix="front")
    context.user_data["photo_front"] = file_path
    context.user_data["photo_url"] = photo_url

    # Concurrently detect barcode and extract product name from packaging via AI
    detected_barcode, detected_name = await asyncio.gather(
        asyncio.to_thread(detect_barcode_from_image, file_path),
        extract_product_name_from_image(file_path)
    )

    if detected_barcode:
        context.user_data["detected_barcode"] = detected_barcode
    if detected_name:
        context.user_data["item_name"] = detected_name
    else:
        context.user_data["item_name"] = "-"

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
            photo_url=photo_url
        )
        sync_manager.enqueue(record)
        qty_display = int(qty) if qty.is_integer() else qty

        await update.message.reply_text(
            f"⚡ *SAVED TO GOOGLE SHEET!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Shelf:* `{shelf}`\n"
            f"🏷️ *Barcode:* `{barcode}`\n"
            f"📦 *Item:* {name}\n"
            f"🔢 *Quantity:* `{qty_display}`\n"
            f"👤 *Crew:* {crew_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 *Send next photo to continue!*",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    name_status = f"\n📦 *Item:* `{detected_name}` (Auto-detected ✨)" if detected_name else ""

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏩ Skip (Barcode is in this photo)", callback_data="skip_barcode_photo")]])
    await update.message.reply_text(
        f"📸 *Photo Received!*{name_status}\n\n"
        f"Send barcode photo (or tap Skip below):",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return STATE_BARCODE_PHOTO


async def flow_receive_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
        file_path, _ = await save_tg_photo(photo.file_id, context, prefix="barcode")
        context.user_data["photo_barcode"] = file_path
        detected = detect_barcode_from_image(file_path)
        if detected:
            context.user_data["detected_barcode"] = detected
    return await prompt_shelf_step(update, context)


async def flow_skip_barcode_photo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["photo_barcode"] = None
    return await prompt_shelf_step(update, context)


async def prompt_shelf_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(
        "📍 *Please type the Shelf Code* (e.g. `G101`, `A12`, `B05`):",
        parse_mode="Markdown"
    )
    return STATE_SHELF


async def flow_shelf_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    shelf = update.message.text.strip().upper()
    if not shelf:
        await update.message.reply_text("⚠️ Please type the Shelf Code (e.g. `G101`):")
        return STATE_SHELF
    context.user_data["shelf"] = shelf
    return await check_barcode_and_prompt_qty(update, context)


async def check_barcode_and_prompt_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    detected_barcode = context.user_data.get("detected_barcode")
    target = update.callback_query.message if update.callback_query else update.message

    if detected_barcode:
        context.user_data["barcode"] = detected_barcode
        await target.reply_text(
            f"🏷️ *Barcode:* `{detected_barcode}` (Auto-detected ✨)\n\n"
            f"🔢 *Please type the Quantity (QTY):*\n_(e.g. 1, 5, 12, 24)_",
            parse_mode="Markdown"
        )
        return STATE_QTY

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip Barcode Number", callback_data="skip_barcode_num")]
    ])
    await target.reply_text(
        "🏷️ Please type the **Barcode numbers** from the label:\n_(Or tap Skip):_",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return STATE_BARCODE


async def flow_barcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "skip_barcode_num":
        context.user_data["barcode"] = "NO_BARCODE"
        await query.message.reply_text("🔢 *Please type the Quantity (QTY):*\n_(e.g. 1, 5, 12, 24)_", parse_mode="Markdown")
        return STATE_QTY
    return STATE_BARCODE


async def flow_barcode_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    barcode = update.message.text.strip()
    context.user_data["barcode"] = barcode or "NO_BARCODE"
    await update.message.reply_text("🔢 *Please type the Quantity (QTY):*\n_(e.g. 1, 5, 12, 24)_", parse_mode="Markdown")
    return STATE_QTY


async def flow_qty_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip() if update.message and update.message.text else ""
    try:
        qty = float(text)
        if qty <= 0:
            await update.message.reply_text("⚠️ Quantity must be greater than 0. Please type a number (e.g. 12):")
            return STATE_QTY
        context.user_data["qty"] = qty
        return await finalize_and_save_count(update, context)
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid number (e.g. `12` or `5`):")
        return STATE_QTY


async def finalize_and_save_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    crew_name = get_user_display_name(update)
    shelf = context.user_data.get("shelf", "UNKNOWN")
    barcode = context.user_data.get("barcode", "NO_BARCODE")
    item_name = context.user_data.get("item_name", "-")
    qty = context.user_data.get("qty", 1.0)
    photo_front = context.user_data.get("photo_front")
    photo_barcode = context.user_data.get("photo_barcode")
    photo_url = context.user_data.get("photo_url", "")

    record = await db_insert_count(
        user_id=user.id if user else 0,
        crew_name=crew_name,
        shelf=shelf,
        barcode=barcode,
        item_name=item_name,
        qty=qty,
        photo_front=photo_front,
        photo_barcode=photo_barcode,
        photo_url=photo_url
    )

    sync_manager.enqueue(record)
    target = update.callback_query.message if update.callback_query else update.message
    qty_display = int(qty) if qty.is_integer() else qty

    card = (
        f"✅ *SAVED TO GOOGLE SHEET! (ID #{record['id']})*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Shelf:* `{shelf}`\n"
        f"🏷️ *Barcode:* `{barcode}`\n"
        f"📦 *Item:* {item_name}\n"
        f"🔢 *Quantity:* `{qty_display}`\n"
        f"👤 *Crew:* {crew_name}\n"
        f"🕒 *Time:* `{record['timestamp']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Send next photo to continue!*"
    )
    await target.reply_text(card, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", parse_mode="Markdown")
    return ConversationHandler.END


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

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, handle_incoming_photo),
            CommandHandler("count", lambda u, c: u.message.reply_text("📸 Send a photo of the product front:"))
        ],
        states={
            STATE_BARCODE_PHOTO: [
                MessageHandler(filters.PHOTO, flow_receive_barcode_photo),
                CallbackQueryHandler(flow_skip_barcode_photo_cb, pattern="^skip_barcode_photo$"),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_SHELF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_shelf_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_BARCODE: [
                CallbackQueryHandler(flow_barcode_callback, pattern="^skip_barcode_num$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_barcode_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_qty_text),
                CommandHandler("cancel", flow_cancel)
            ]
        },
        fallbacks=[CommandHandler("cancel", flow_cancel)],
        per_user=True,
        per_chat=True
    )

    tg_app.add_handler(conv)
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_start))

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
