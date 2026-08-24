"""
=============================================================================
STORE STOCK COUNT TELEGRAM BOT — ALL-IN-ONE
=============================================================================
Features:
- Multi-user concurrency for many store crew members counting simultaneously
- Sticky shelf memory (/shelf G101)
- Dual photo capture (Front of item + Barcode)
- Smart auto barcode detection (zxing-cpp) with manual typing fallback
- Fast Quantity selection buttons (1, 2, 5, 10, 12, 24, Custom)
- Real-time Google Sheets auto-fill via Webhook (async background queue)
- Beautiful 3-tab Excel (.xlsx) export (/export)
=============================================================================
"""

import os
import uuid
import asyncio
import logging
import zoneinfo
from io import BytesIO
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from PIL import Image, ImageEnhance, ImageOps
import zxingcpp
import aiosqlite
import aiohttp
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "inventory.db"))
PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR", str(BASE_DIR / "photos")))
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bangkok")

ADMIN_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS = set()
if ADMIN_IDS_RAW:
    for uid in ADMIN_IDS_RAW.split(","):
        if uid.strip().isdigit():
            ADMIN_USER_IDS.add(int(uid.strip()))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("StockBot")

# Conversation States
(
    STATE_FRONT_PHOTO,
    STATE_BARCODE_PHOTO,
    STATE_SHELF,
    STATE_BARCODE,
    STATE_ITEM_NAME,
    STATE_QTY
) = range(6)


# ---------------------------------------------------------------------------
# 2. DATABASE LAYER (SQLite WAL Mode)
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
                synced_sheet INTEGER NOT NULL DEFAULT 0
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                active_shelf TEXT,
                updated_at TEXT
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_shelf ON counts(shelf);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_user ON counts(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_barcode ON counts(barcode);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_synced ON counts(synced_sheet);")
        await db.commit()


async def db_insert_count(user_id: int, crew_name: str, shelf: str, barcode: str, item_name: str, qty: float, photo_front: str = None, photo_barcode: str = None) -> Dict[str, Any]:
    timestamp = get_current_timestamp()
    shelf_clean = shelf.strip().upper()
    barcode_clean = str(barcode).strip()
    item_name_clean = (item_name or "").strip()

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO counts (
                timestamp, user_id, crew_name, shelf, barcode, item_name, qty, photo_front, photo_barcode, synced_sheet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (timestamp, user_id, crew_name, shelf_clean, barcode_clean, item_name_clean, qty, photo_front, photo_barcode)
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
        "synced_sheet": 0
    }


async def db_get_user_active_shelf(user_id: int) -> Optional[str]:
    async with get_db() as db:
        async with db.execute("SELECT active_shelf FROM user_preferences WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row["active_shelf"]:
                return row["active_shelf"]
    return None


async def db_set_user_active_shelf(user_id: int, shelf: str):
    timestamp = get_current_timestamp()
    shelf_clean = shelf.strip().upper()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO user_preferences (user_id, active_shelf, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                active_shelf = excluded.active_shelf,
                updated_at = excluded.updated_at
            """,
            (user_id, shelf_clean, timestamp)
        )
        await db.commit()


async def db_get_all_counts() -> List[Dict[str, Any]]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM counts ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def db_get_recent_counts(user_id: Optional[int] = None, limit: int = 10) -> List[Dict[str, Any]]:
    async with get_db() as db:
        if user_id:
            query = "SELECT * FROM counts WHERE user_id = ? ORDER BY id DESC LIMIT ?"
            params = (user_id, limit)
        else:
            query = "SELECT * FROM counts ORDER BY id DESC LIMIT ?"
            params = (limit,)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def db_mark_synced(count_ids: List[int]):
    if not count_ids:
        return
    placeholders = ",".join("?" for _ in count_ids)
    async with get_db() as db:
        await db.execute(f"UPDATE counts SET synced_sheet = 1 WHERE id IN ({placeholders})", count_ids)
        await db.commit()


async def db_get_unsynced_counts(limit: int = 50) -> List[Dict[str, Any]]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM counts WHERE synced_sheet = 0 ORDER BY id ASC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def db_get_summary_stats() -> Dict[str, Any]:
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) as total_skus, COALESCE(SUM(qty), 0) as total_qty, COUNT(DISTINCT shelf) as total_shelves FROM counts") as c1:
            r1 = await c1.fetchone()
            total_skus = r1["total_skus"] if r1 else 0
            total_qty = r1["total_qty"] if r1 else 0
            total_shelves = r1["total_shelves"] if r1 else 0

        async with db.execute("SELECT shelf, COUNT(*) as sku_count, SUM(qty) as total_qty FROM counts GROUP BY shelf ORDER BY shelf ASC") as c2:
            shelf_breakdown = [dict(r) for r in await c2.fetchall()]

        async with db.execute("SELECT crew_name, COUNT(*) as sku_count, SUM(qty) as total_qty FROM counts GROUP BY user_id, crew_name ORDER BY total_qty DESC") as c3:
            crew_breakdown = [dict(r) for r in await c3.fetchall()]

        async with db.execute("SELECT COUNT(*) as pending FROM counts WHERE synced_sheet = 0") as c4:
            r4 = await c4.fetchone()
            pending_sync = r4["pending"] if r4 else 0

    return {
        "total_skus": total_skus,
        "total_qty": total_qty,
        "total_shelves": total_shelves,
        "pending_sync": pending_sync,
        "shelf_breakdown": shelf_breakdown,
        "crew_breakdown": crew_breakdown
    }


async def db_delete_count(count_id: int, user_id: Optional[int] = None) -> bool:
    async with get_db() as db:
        if user_id:
            cursor = await db.execute("DELETE FROM counts WHERE id = ? AND user_id = ?", (count_id, user_id))
        else:
            cursor = await db.execute("DELETE FROM counts WHERE id = ?", (count_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# 3. BARCODE RECOGNITION (zxing-cpp + PIL)
# ---------------------------------------------------------------------------
def detect_barcode_from_image(image_path: str) -> Optional[str]:
    if not image_path or not os.path.exists(image_path):
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
        logger.warning(f"Barcode detection error on {image_path}: {e}")
    return None


# ---------------------------------------------------------------------------
# 4. GOOGLE SHEETS ASYNC BACKGROUND SYNC
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
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
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
            "qty": data.get("qty", 1)
        }
        for attempt in range(1, 4):
            try:
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    if resp.status in (200, 201, 302):
                        return True
            except Exception:
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

    async def sync_pending_records(self) -> int:
        if not self.webhook_url:
            return 0
        pending = await db_get_unsynced_counts(limit=100)
        synced_ids = []
        for item in pending:
            if await self._send(item):
                synced_ids.append(item["id"])
        if synced_ids:
            await db_mark_synced(synced_ids)
        return len(synced_ids)


sync_manager = SheetsSyncManager()


# ---------------------------------------------------------------------------
# 5. EXCEL EXPORTER (.xlsx)
# ---------------------------------------------------------------------------
def create_excel_report(counts: List[Dict[str, Any]]) -> BytesIO:
    wb = openpyxl.Workbook()
    HEADER_FILL = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    SUBHEADER_FILL = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")
    SUBHEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    ZEBRA_FILL = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    TOTAL_FILL = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    TOTAL_FONT = Font(name="Calibri", size=11, bold=True, color="0F172A")
    BOLD_FONT = Font(name="Calibri", size=11, bold=True, color="1E293B")
    THIN_BORDER = Border(left=Side(style="thin", color="CBD5E1"), right=Side(style="thin", color="CBD5E1"), top=Side(style="thin", color="CBD5E1"), bottom=Side(style="thin", color="CBD5E1"))
    DOUBLE_BOTTOM = Border(left=Side(style="thin", color="CBD5E1"), right=Side(style="thin", color="CBD5E1"), top=Side(style="thin", color="CBD5E1"), bottom=Side(style="double", color="1E293B"))

    # Tab 1: Stock Items
    ws = wb.active
    ws.title = "Stock Items"
    ws.views.sheetView[0].showGridLines = True
    ws.merge_cells("A1:H1")
    ws["A1"].value = f"📦 STORE STOCK COUNT REPORT — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    ws["A1"].font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    headers = ["No.", "Date & Time", "Crew Member", "Shelf Location", "Barcode Number", "Item Name / Description", "Quantity", "Sheet Sync"]
    ws.row_dimensions[2].height = 24
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER

    row_num = 3
    for idx, item in enumerate(counts, 1):
        ws.row_dimensions[row_num].height = 20
        fill = ZEBRA_FILL if idx % 2 == 0 else PatternFill(fill_type=None)
        
        c1 = ws.cell(row=row_num, column=1, value=idx)
        c2 = ws.cell(row=row_num, column=2, value=item.get("timestamp", ""))
        c3 = ws.cell(row=row_num, column=3, value=item.get("crew_name", ""))
        c4 = ws.cell(row=row_num, column=4, value=str(item.get("shelf", "")).upper())
        c4.font = BOLD_FONT

        # Barcode formatted strictly as text string (@) to prevent scientific notation
        c5 = ws.cell(row=row_num, column=5, value=str(item.get("barcode", "")))
        c5.number_format = "@"
        c5.font = BOLD_FONT
        c5.alignment = Alignment(horizontal="center", vertical="center")

        c6 = ws.cell(row=row_num, column=6, value=item.get("item_name", "-"))
        c7 = ws.cell(row=row_num, column=7, value=float(item.get("qty", 0)))
        c7.number_format = "#,##0"
        c7.font = BOLD_FONT
        c7.alignment = Alignment(horizontal="right", vertical="center")

        c8 = ws.cell(row=row_num, column=8, value="✅ Synced" if item.get("synced_sheet") == 1 else "⏳ Pending")
        c8.alignment = Alignment(horizontal="center", vertical="center")

        for cell in [c1, c2, c3, c4, c5, c6, c7, c8]:
            if fill.fill_type:
                cell.fill = fill
            cell.border = THIN_BORDER
        row_num += 1

    if counts:
        ws.row_dimensions[row_num].height = 24
        ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=6)
        t_label = ws.cell(row=row_num, column=1, value=f"TOTAL ({len(counts)} SKUs Counted)")
        t_label.font = TOTAL_FONT
        t_label.fill = TOTAL_FILL
        t_label.alignment = Alignment(horizontal="right", vertical="center")

        t_val = ws.cell(row=row_num, column=7, value=f"=SUM(G3:G{row_num-1})")
        t_val.font = TOTAL_FONT
        t_val.fill = TOTAL_FILL
        t_val.number_format = "#,##0"
        t_val.alignment = Alignment(horizontal="right", vertical="center")

        ws.cell(row=row_num, column=8).fill = TOTAL_FILL
        for col in range(1, 9):
            ws.cell(row=row_num, column=col).border = DOUBLE_BOTTOM
        ws.auto_filter.ref = f"A2:H{row_num-1}"

    # Tab 2: Summary by Shelf
    ws_shelf = wb.create_sheet(title="Summary by Shelf")
    ws_shelf.views.sheetView[0].showGridLines = True
    ws_shelf.merge_cells("A1:C1")
    ws_shelf["A1"].value = "🏢 STOCK BREAKDOWN BY SHELF LOCATION"
    ws_shelf["A1"].font = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    ws_shelf["A1"].fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    ws_shelf["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws_shelf.row_dimensions[1].height = 28

    s_headers = ["Shelf Location", "Total SKUs (Items)", "Total Quantity Units"]
    for idx, h in enumerate(s_headers, 1):
        c = ws_shelf.cell(row=2, column=idx, value=h)
        c.fill = SUBHEADER_FILL
        c.font = SUBHEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = THIN_BORDER

    shelf_dict = {}
    for item in counts:
        s = str(item.get("shelf", "UNKNOWN")).upper()
        if s not in shelf_dict:
            shelf_dict[s] = {"skus": 0, "qty": 0.0}
        shelf_dict[s]["skus"] += 1
        shelf_dict[s]["qty"] += float(item.get("qty", 0))

    s_row = 3
    for s_name in sorted(shelf_dict.keys()):
        d = shelf_dict[s_name]
        ws_shelf.cell(row=s_row, column=1, value=s_name).font = BOLD_FONT
        ws_shelf.cell(row=s_row, column=2, value=d["skus"]).alignment = Alignment(horizontal="right")
        q = ws_shelf.cell(row=s_row, column=3, value=d["qty"])
        q.alignment = Alignment(horizontal="right")
        q.font = BOLD_FONT
        for col in range(1, 4):
            ws_shelf.cell(row=s_row, column=col).border = THIN_BORDER
        s_row += 1

    # Tab 3: Summary by Crew
    ws_crew = wb.create_sheet(title="Summary by Crew")
    ws_crew.views.sheetView[0].showGridLines = True
    ws_crew.merge_cells("A1:C1")
    ws_crew["A1"].value = "👤 STOCK COUNT ACTIVITY BY CREW MEMBER"
    ws_crew["A1"].font = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    ws_crew["A1"].fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    ws_crew["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws_crew.row_dimensions[1].height = 28

    c_headers = ["Crew Member", "Total SKUs Logged", "Total Quantity Logged"]
    for idx, h in enumerate(c_headers, 1):
        c = ws_crew.cell(row=2, column=idx, value=h)
        c.fill = SUBHEADER_FILL
        c.font = SUBHEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = THIN_BORDER

    crew_dict = {}
    for item in counts:
        cr = item.get("crew_name") or "Unknown"
        if cr not in crew_dict:
            crew_dict[cr] = {"skus": 0, "qty": 0.0}
        crew_dict[cr]["skus"] += 1
        crew_dict[cr]["qty"] += float(item.get("qty", 0))

    c_row = 3
    for cr_name in sorted(crew_dict.keys(), key=lambda x: crew_dict[x]["qty"], reverse=True):
        d = crew_dict[cr_name]
        ws_crew.cell(row=c_row, column=1, value=cr_name).font = BOLD_FONT
        ws_crew.cell(row=c_row, column=2, value=d["skus"]).alignment = Alignment(horizontal="right")
        q = ws_crew.cell(row=c_row, column=3, value=d["qty"])
        q.alignment = Alignment(horizontal="right")
        q.font = BOLD_FONT
        for col in range(1, 4):
            ws_crew.cell(row=c_row, column=col).border = THIN_BORDER
        c_row += 1

    # Auto column width
    for worksheet in [ws, ws_shelf, ws_crew]:
        for col in worksheet.columns:
            max_len = max((len(str(cell.value or "")) for cell in col if cell.row > 1), default=10)
            col_letter = get_column_letter(col[0].column)
            worksheet.column_dimensions[col_letter].width = max(max_len + 4, 12)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# 6. TELEGRAM BOT HANDLERS & STATE MACHINE
# ---------------------------------------------------------------------------
def get_user_display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Unknown"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if user.username:
        return f"{full_name} (@{user.username})" if full_name else f"@{user.username}"
    return full_name or f"User_{user.id}"


async def save_tg_photo(file_id: str, context: ContextTypes.DEFAULT_TYPE, prefix: str = "img") -> str:
    try:
        tg_file = await context.bot.get_file(file_id)
        filename = f"{prefix}_{uuid.uuid4().hex[:8]}.jpg"
        file_path = str(PHOTOS_DIR / filename)
        await tg_file.download_to_drive(custom_path=file_path)
        return file_path
    except Exception as e:
        logger.error(f"Error saving photo: {e}")
        return ""


def get_quick_qty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1", callback_data="qty_1"),
            InlineKeyboardButton("2", callback_data="qty_2"),
            InlineKeyboardButton("3", callback_data="qty_3"),
            InlineKeyboardButton("4", callback_data="qty_4"),
        ],
        [
            InlineKeyboardButton("5", callback_data="qty_5"),
            InlineKeyboardButton("6", callback_data="qty_6"),
            InlineKeyboardButton("10", callback_data="qty_10"),
            InlineKeyboardButton("12", callback_data="qty_12"),
        ],
        [
            InlineKeyboardButton("24", callback_data="qty_24"),
            InlineKeyboardButton("36", callback_data="qty_36"),
            InlineKeyboardButton("48", callback_data="qty_48"),
            InlineKeyboardButton("100", callback_data="qty_100"),
        ],
        [
            InlineKeyboardButton("✏️ Type Custom Number", callback_data="qty_custom")
        ]
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    active_shelf = await db_get_user_active_shelf(user.id) if user else None
    shelf_text = f"📍 *Active Shelf:* `{active_shelf}`" if active_shelf else "📍 *Active Shelf:* _Not set yet_"

    msg = (
        f"👋 *Store Stock Count Bot*\n\n"
        f"{shelf_text}\n\n"
        f"🚀 *How to Count Stock:*\n"
        f"1️⃣ **Send a photo of the item** (or type /count)\n"
        f"2️⃣ Send or skip barcode photo\n"
        f"3️⃣ Confirm/Type Shelf (e.g. `G101`)\n"
        f"4️⃣ Confirm/Type Barcode number\n"
        f"5️⃣ Enter Item Name\n"
        f"6️⃣ Select/Type Quantity (QTY)\n\n"
        f"📌 *Commands:*\n"
        f"• `/shelf <code?>` — Set active shelf (e.g. `/shelf G101`)\n"
        f"• `/mycounts` — View your last counted items\n"
        f"• `/delete_last` — Delete your most recent count\n"
        f"• `/stats` — View total count summary\n"
        f"• `/export` — 📊 Download Excel (.xlsx) report\n"
        f"• `/sync` — Sync pending items to Google Sheets\n"
        f"• `/cancel` — Cancel current count\n\n"
        f"👉 *Send a photo now to begin counting!*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_shelf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if context.args:
        new_shelf = " ".join(context.args).strip().upper()
        await db_set_user_active_shelf(user.id, new_shelf)
        await update.message.reply_text(f"✅ *Active shelf updated to:* `{new_shelf}`\nSend an item photo to start counting!", parse_mode="Markdown")
    else:
        current = await db_get_user_active_shelf(user.id)
        if current:
            await update.message.reply_text(f"📍 Current active shelf: `{current}`\nTo change: `/shelf <NEW_SHELF>`", parse_mode="Markdown")
        else:
            await update.message.reply_text("📍 No active shelf set. Type: `/shelf <SHELF_CODE>` (e.g. `/shelf G101`)", parse_mode="Markdown")


async def cmd_mycounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    records = await db_get_recent_counts(user_id=user.id, limit=8)
    if not records:
        await update.message.reply_text("ℹ️ No items recorded yet. Send a photo to start!", parse_mode="Markdown")
        return
    text = "📋 *Your Recent Scanned Items:*\n\n"
    for r in records:
        synced = "✅" if r.get("synced_sheet") == 1 else "⏳"
        text += f"• *ID #{r['id']}* — `{r['shelf']}` | `{r['barcode']}`\n   📦 {r.get('item_name') or 'No Name'} (x{r['qty']}) {synced}\n\n"
    text += "To delete a mistake, type `/delete <ID>` or `/delete_last`"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    records = await db_get_recent_counts(user_id=user.id, limit=1)
    if not records:
        await update.message.reply_text("❌ No items found to delete.")
        return
    last_item = records[0]
    if await db_delete_count(last_item["id"], user_id=user.id):
        await update.message.reply_text(f"🗑️ Deleted ID #{last_item['id']} ({last_item['shelf']} | {last_item['barcode']})")
    else:
        await update.message.reply_text("❌ Failed to delete entry.")


async def cmd_delete_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/delete <ID>`", parse_mode="Markdown")
        return
    count_id = int(context.args[0])
    is_admin = user.id in ADMIN_USER_IDS
    if await db_delete_count(count_id, user_id=None if is_admin else user.id):
        await update.message.reply_text(f"🗑️ Deleted item ID #{count_id}")
    else:
        await update.message.reply_text(f"❌ Could not delete item #{count_id}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await db_get_summary_stats()
    text = (
        f"📊 *STOCK COUNT SUMMARY STATS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *Total SKUs Counted:* `{stats['total_skus']}`\n"
        f"🔢 *Total Units (Qty):* `{stats['total_qty']:,.0f}`\n"
        f"🏢 *Total Shelves:* `{stats['total_shelves']}`\n"
        f"🌐 *Pending Sheet Sync:* `{stats['pending_sync']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    if stats["shelf_breakdown"]:
        text += "🏢 *By Shelf:*\n"
        for s in stats["shelf_breakdown"][:8]:
            text += f"• `{s['shelf']}`: {s['sku_count']} SKUs ({s['total_qty']:,.0f} units)\n"
        text += "\n"
    if stats["crew_breakdown"]:
        text += "👤 *By Crew:*\n"
        for c in stats["crew_breakdown"]:
            text += f"• {c['crew_name']}: {c['sku_count']} SKUs ({c['total_qty']:,.0f} units)\n"
    text += "\n👉 Type `/export` to download Excel report!"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Generating Excel report...*", parse_mode="Markdown")
    try:
        counts = await db_get_all_counts()
        if not counts:
            await msg.edit_text("ℹ️ No items recorded yet.")
            return
        excel_buf = create_excel_report(counts)
        filename = f"Stock_Count_Report_{get_current_timestamp().replace(':', '-').replace(' ', '_')}.xlsx"
        await update.message.reply_document(
            document=excel_buf,
            filename=filename,
            caption=f"📊 *Stock Report Exported!*\n• Total Items: `{len(counts)}`\n• Time: `{get_current_timestamp()}`",
            parse_mode="Markdown"
        )
        await msg.delete()
    except Exception as e:
        logger.error(f"Export error: {e}")
        await msg.edit_text(f"❌ Error generating Excel: {e}")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_SHEET_WEBHOOK_URL:
        await update.message.reply_text("⚠️ Google Sheet Webhook URL is not configured in `.env`.", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("⏳ *Syncing pending items to Google Sheets...*", parse_mode="Markdown")
    count = await sync_manager.sync_pending_records()
    await msg.edit_text(f"✅ *Sync Complete:* `{count}` pending items pushed to Google Sheets.", parse_mode="Markdown")


# Conversation Flow
async def flow_start_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
        file_path = await save_tg_photo(photo.file_id, context, prefix="front")
        context.user_data["photo_front"] = file_path
        detected = detect_barcode_from_image(file_path)
        if detected:
            context.user_data["detected_barcode"] = detected

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏩ Skip (Barcode is in this photo)", callback_data="skip_barcode_photo")]])
        await update.message.reply_text("📸 *Front Photo Saved!*\n\nNow send a close-up photo of the **Barcode label** (or tap Skip):", reply_markup=kb, parse_mode="Markdown")
        return STATE_BARCODE_PHOTO

    await update.message.reply_text("📸 *Step 1/5: Item Photo*\n\nPlease send a photo of the **Front of the product**:", parse_mode="Markdown")
    return STATE_FRONT_PHOTO


async def flow_receive_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("⚠️ Please send a photo.")
        return STATE_FRONT_PHOTO
    photo = update.message.photo[-1]
    file_path = await save_tg_photo(photo.file_id, context, prefix="front")
    context.user_data["photo_front"] = file_path
    detected = detect_barcode_from_image(file_path)
    if detected:
        context.user_data["detected_barcode"] = detected
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏩ Skip (Barcode is in this photo)", callback_data="skip_barcode_photo")]])
    await update.message.reply_text("📸 *Front Photo Saved!*\n\nNow send a photo of the **Barcode label** (or tap Skip):", reply_markup=kb, parse_mode="Markdown")
    return STATE_BARCODE_PHOTO


async def flow_receive_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
        file_path = await save_tg_photo(photo.file_id, context, prefix="barcode")
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
    user = update.effective_user
    active_shelf = await db_get_user_active_shelf(user.id) if user else None
    target = update.callback_query.message if update.callback_query else update.message

    if active_shelf:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Keep Shelf {active_shelf}", callback_data=f"keep_shelf_{active_shelf}")],
            [InlineKeyboardButton("✏️ Enter Different Shelf", callback_data="change_shelf")]
        ])
        await target.reply_text(f"📍 *Step 2/5: Shelf Location*\n\nCurrent shelf: `{active_shelf}`\nTap below or type new shelf (e.g. `G102`):", reply_markup=kb, parse_mode="Markdown")
    else:
        await target.reply_text("📍 *Step 2/5: Shelf Location*\n\nPlease type the **Shelf Code** (e.g. `G101`):", parse_mode="Markdown")
    return STATE_SHELF


async def flow_shelf_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("keep_shelf_"):
        shelf = query.data.replace("keep_shelf_", "").strip().upper()
        context.user_data["shelf"] = shelf
        return await prompt_barcode_step(update, context)
    elif query.data == "change_shelf":
        await query.message.reply_text("📍 Please type the new **Shelf Code** (e.g. `G102`):", parse_mode="Markdown")
        return STATE_SHELF
    return STATE_SHELF


async def flow_shelf_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    shelf = update.message.text.strip().upper()
    if not shelf:
        await update.message.reply_text("⚠️ Shelf cannot be empty. Type e.g. `G101`:")
        return STATE_SHELF
    context.user_data["shelf"] = shelf
    if update.effective_user:
        await db_set_user_active_shelf(update.effective_user.id, shelf)
    return await prompt_barcode_step(update, context)


async def prompt_barcode_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.callback_query.message if update.callback_query else update.message
    detected = context.user_data.get("detected_barcode")
    if detected:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm Barcode: {detected}", callback_data=f"confirm_barcode_{detected}")],
            [InlineKeyboardButton("✏️ Type Different Barcode", callback_data="type_barcode")]
        ])
        await target.reply_text(f"🏷️ *Step 3/5: Barcode Number*\n\n🔍 *Detected from photo:* `{detected}`\nTap to confirm or type manually:", reply_markup=kb, parse_mode="Markdown")
    else:
        await target.reply_text("🏷️ *Step 3/5: Barcode Number*\n\nPlease type the **Barcode number** from the label:", parse_mode="Markdown")
    return STATE_BARCODE


async def flow_barcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("confirm_barcode_"):
        context.user_data["barcode"] = query.data.replace("confirm_barcode_", "").strip()
        return await prompt_item_name_step(update, context)
    elif query.data == "type_barcode":
        await query.message.reply_text("🏷️ Please type the **Barcode number**:", parse_mode="Markdown")
        return STATE_BARCODE
    return STATE_BARCODE


async def flow_barcode_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    barcode = update.message.text.strip()
    if not barcode:
        await update.message.reply_text("⚠️ Barcode cannot be empty.")
        return STATE_BARCODE
    context.user_data["barcode"] = barcode
    return await prompt_item_name_step(update, context)


async def prompt_item_name_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.callback_query.message if update.callback_query else update.message
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏩ Skip / No Name", callback_data="skip_item_name")]])
    await target.reply_text("📦 *Step 4/5: Item Name*\n\nPlease type the **Product Name** (e.g. `Oishi Green Tea 500ml`)\n_(Or tap Skip):_", reply_markup=kb, parse_mode="Markdown")
    return STATE_ITEM_NAME


async def flow_item_name_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "skip_item_name":
        context.user_data["item_name"] = "-"
        return await prompt_qty_step(update, context)
    return STATE_ITEM_NAME


async def flow_item_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["item_name"] = update.message.text.strip() or "-"
    return await prompt_qty_step(update, context)


async def prompt_qty_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("🔢 *Step 5/5: Quantity (QTY)*\n\nSelect or type quantity:", reply_markup=get_quick_qty_keyboard(), parse_mode="Markdown")
    return STATE_QTY


async def flow_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = query.data.replace("qty_", "")
    if val == "custom":
        await query.message.reply_text("🔢 Please type the exact **Quantity** number (e.g. `15`):", parse_mode="Markdown")
        return STATE_QTY
    try:
        context.user_data["qty"] = float(val)
        return await finalize_and_save_count(update, context)
    except ValueError:
        return STATE_QTY


async def flow_qty_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        qty = float(update.message.text.strip())
        if qty <= 0:
            await update.message.reply_text("⚠️ Quantity must be > 0.")
            return STATE_QTY
        context.user_data["qty"] = qty
        return await finalize_and_save_count(update, context)
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number. Please enter numeric quantity (e.g. `12`):")
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

    record = await db_insert_count(
        user_id=user.id if user else 0,
        crew_name=crew_name,
        shelf=shelf,
        barcode=barcode,
        item_name=item_name,
        qty=qty,
        photo_front=photo_front,
        photo_barcode=photo_barcode
    )

    sync_manager.enqueue(record)
    target = update.callback_query.message if update.callback_query else update.message
    qty_display = int(qty) if qty.is_integer() else qty

    card = (
        f"✅ *STOCK ITEM SAVED! (ID #{record['id']})*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Shelf:* `{shelf}`\n"
        f"🏷️ *Barcode:* `{barcode}`\n"
        f"📦 *Item:* {item_name}\n"
        f"🔢 *Quantity:* `{qty_display}`\n"
        f"👤 *Crew:* {crew_name}\n"
        f"🕒 *Time:* `{record['timestamp']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Send next item photo to keep counting!* 📸\n"
        f"_(Active shelf is still `{shelf}`)_"
    )
    await target.reply_text(card, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Count cancelled.", parse_mode="Markdown")
    return ConversationHandler.END


def build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is empty! Please set it in your .env file.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("count", flow_start_count),
            MessageHandler(filters.PHOTO, flow_start_count)
        ],
        states={
            STATE_FRONT_PHOTO: [MessageHandler(filters.PHOTO, flow_receive_front_photo), CommandHandler("cancel", flow_cancel)],
            STATE_BARCODE_PHOTO: [
                MessageHandler(filters.PHOTO, flow_receive_barcode_photo),
                CallbackQueryHandler(flow_skip_barcode_photo_cb, pattern="^skip_barcode_photo$"),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_SHELF: [
                CallbackQueryHandler(flow_shelf_callback, pattern="^(keep_shelf_|change_shelf)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_shelf_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_BARCODE: [
                CallbackQueryHandler(flow_barcode_callback, pattern="^(confirm_barcode_|type_barcode)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_barcode_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_ITEM_NAME: [
                CallbackQueryHandler(flow_item_name_callback, pattern="^skip_item_name$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_item_name_text),
                CommandHandler("cancel", flow_cancel)
            ],
            STATE_QTY: [
                CallbackQueryHandler(flow_qty_callback, pattern="^qty_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_qty_text),
                CommandHandler("cancel", flow_cancel)
            ]
        },
        fallbacks=[CommandHandler("cancel", flow_cancel)],
        per_user=True,
        per_chat=True
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("shelf", cmd_shelf))
    app.add_handler(CommandHandler("mycounts", cmd_mycounts))
    app.add_handler(CommandHandler("delete_last", cmd_delete_last))
    app.add_handler(CommandHandler("delete", cmd_delete_id))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("sync", cmd_sync))
    return app


async def post_init(application: Application):
    await init_db()
    await sync_manager.start()
    logger.info("Bot & Database Initialized.")


async def post_shutdown(application: Application):
    await sync_manager.stop()
    logger.info("Bot Shutdown.")


def main():
    print("🚀 Initializing Store Stock Count Telegram Bot...")
    app = build_application()
    app.post_init = post_init
    app.post_shutdown = post_shutdown
    print("🤖 Bot is running! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
