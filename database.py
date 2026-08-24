"""
Database layer using aiosqlite with WAL mode for high concurrency.
Safely handles multiple store crew members logging items simultaneously.
"""

from contextlib import asynccontextmanager
from datetime import datetime
import zoneinfo
from typing import Optional, List, Dict, Any
import aiosqlite
import config


def get_current_timestamp() -> str:
    """Returns formatted current local timestamp."""
    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def get_db():
    """Yields an aiosqlite connection with WAL mode and sensible timeouts."""
    db = await aiosqlite.connect(config.DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute("PRAGMA busy_timeout=10000;")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    """Initializes the database schema and indexes."""
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

        # Indexes for fast querying & export
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_shelf ON counts(shelf);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_user ON counts(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_barcode ON counts(barcode);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_counts_synced ON counts(synced_sheet);")
        await db.commit()


async def insert_count(
    user_id: int,
    crew_name: str,
    shelf: str,
    barcode: str,
    item_name: str,
    qty: float,
    photo_front: Optional[str] = None,
    photo_barcode: Optional[str] = None
) -> Dict[str, Any]:
    """Inserts a new stock count record."""
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


async def get_user_active_shelf(user_id: int) -> Optional[str]:
    """Retrieves the active shelf for a specific crew member."""
    async with get_db() as db:
        async with db.execute("SELECT active_shelf FROM user_preferences WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row["active_shelf"]:
                return row["active_shelf"]
    return None


async def set_user_active_shelf(user_id: int, shelf: str):
    """Sets or updates the active shelf for a crew member."""
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


async def get_all_counts() -> List[Dict[str, Any]]:
    """Fetches all count records for export or auditing."""
    async with get_db() as db:
        async with db.execute("SELECT * FROM counts ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_recent_counts(user_id: Optional[int] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Fetches the most recent count records."""
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


async def get_unsynced_counts(limit: int = 50) -> List[Dict[str, Any]]:
    """Fetches items that have not yet been synced to Google Sheets."""
    async with get_db() as db:
        async with db.execute("SELECT * FROM counts WHERE synced_sheet = 0 ORDER BY id ASC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def mark_synced(count_ids: List[int]):
    """Marks a list of count IDs as synced."""
    if not count_ids:
        return
    placeholders = ",".join("?" for _ in count_ids)
    async with get_db() as db:
        await db.execute(
            f"UPDATE counts SET synced_sheet = 1 WHERE id IN ({placeholders})",
            count_ids
        )
        await db.commit()


async def get_summary_stats() -> Dict[str, Any]:
    """Computes summary statistics for stock counting progress."""
    async with get_db() as db:
        # Total counts & sum of qty
        async with db.execute("SELECT COUNT(*) as total_skus, COALESCE(SUM(qty), 0) as total_qty, COUNT(DISTINCT shelf) as total_shelves FROM counts") as c1:
            row1 = await c1.fetchone()
            total_skus = row1["total_skus"] if row1 else 0
            total_qty = row1["total_qty"] if row1 else 0
            total_shelves = row1["total_shelves"] if row1 else 0

        # Breakdown by shelf
        async with db.execute("SELECT shelf, COUNT(*) as sku_count, SUM(qty) as total_qty FROM counts GROUP BY shelf ORDER BY shelf ASC") as c2:
            shelf_rows = await c2.fetchall()
            shelf_breakdown = [dict(r) for r in shelf_rows]

        # Breakdown by crew member
        async with db.execute("SELECT crew_name, COUNT(*) as sku_count, SUM(qty) as total_qty FROM counts GROUP BY user_id, crew_name ORDER BY total_qty DESC") as c3:
            crew_rows = await c3.fetchall()
            crew_breakdown = [dict(r) for r in crew_rows]

        # Pending sync count
        async with db.execute("SELECT COUNT(*) as pending FROM counts WHERE synced_sheet = 0") as c4:
            row4 = await c4.fetchone()
            pending_sync = row4["pending"] if row4 else 0

    return {
        "total_skus": total_skus,
        "total_qty": total_qty,
        "total_shelves": total_shelves,
        "pending_sync": pending_sync,
        "shelf_breakdown": shelf_breakdown,
        "crew_breakdown": crew_breakdown
    }


async def delete_count(count_id: int, user_id: Optional[int] = None) -> bool:
    """Deletes a count entry (user can only delete their own unless admin)."""
    async with get_db() as db:
        if user_id:
            cursor = await db.execute("DELETE FROM counts WHERE id = ? AND user_id = ?", (count_id, user_id))
        else:
            cursor = await db.execute("DELETE FROM counts WHERE id = ?", (count_id,))
        await db.commit()
        return cursor.rowcount > 0


async def clear_all_counts():
    """Clears all count records (Admin use)."""
    async with get_db() as db:
        await db.execute("DELETE FROM counts;")
        await db.commit()
