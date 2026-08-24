"""
Google Sheets synchronization worker.
Uses an asynchronous background queue with retry logic to push count records to Google Apps Script Webhook
without blocking Telegram user interactions.
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional
import aiohttp
from config import GOOGLE_SHEET_WEBHOOK_URL, SHEET_SYNC_MAX_RETRIES, SHEET_SYNC_TIMEOUT_SECONDS
import database

logger = logging.getLogger(__name__)


class SheetsSyncManager:
    """Manages asynchronous background synchronization with Google Sheets."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or GOOGLE_SHEET_WEBHOOK_URL
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._is_running = False

    async def start(self):
        """Starts the background sync worker."""
        if self._is_running:
            return
        self._is_running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=SHEET_SYNC_TIMEOUT_SECONDS)
        )
        self._worker_task = asyncio.create_task(self._process_queue())
        logger.info("SheetsSyncManager started.")

    async def stop(self):
        """Stops the sync worker gracefully."""
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        logger.info("SheetsSyncManager stopped.")

    def enqueue(self, count_data: Dict[str, Any]):
        """Enqueues a count item for background sync."""
        if not self.webhook_url:
            return
        self._queue.put_nowait(count_data)

    async def _send_to_webhook(self, data: Dict[str, Any]) -> bool:
        """Sends a single record to the Google Apps Script Webhook."""
        if not self.webhook_url or not self._session:
            return False

        payload = {
            "timestamp": data.get("timestamp", ""),
            "crew": data.get("crew_name", ""),
            "shelf": data.get("shelf", ""),
            "barcode": str(data.get("barcode", "")),
            "name": data.get("item_name", ""),
            "qty": data.get("qty", 1),
            "photo_front": data.get("photo_front", ""),
            "photo_barcode": data.get("photo_barcode", "")
        }

        for attempt in range(1, SHEET_SYNC_MAX_RETRIES + 1):
            try:
                async with self._session.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    if resp.status in (200, 201, 302):
                        return True
                    else:
                        text = await resp.text()
                        logger.warning(f"Google Sheet Webhook returned status {resp.status}: {text}")
            except Exception as e:
                logger.warning(f"Google Sheet sync attempt {attempt}/{SHEET_SYNC_MAX_RETRIES} failed: {e}")
                if attempt < SHEET_SYNC_MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)

        return False

    async def _process_queue(self):
        """Background loop reading from queue."""
        while self._is_running:
            try:
                item = await self._queue.get()
                success = await self._send_to_webhook(item)
                if success and "id" in item:
                    await database.mark_synced([item["id"]])
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in SheetsSync worker loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def sync_pending_records(self) -> int:
        """Manually synchronizes all pending unsynced records from the database."""
        if not self.webhook_url:
            return 0

        pending_items = await database.get_unsynced_counts(limit=100)
        if not pending_items:
            return 0

        synced_ids = []
        for item in pending_items:
            success = await self._send_to_webhook(item)
            if success:
                synced_ids.append(item["id"])

        if synced_ids:
            await database.mark_synced(synced_ids)

        return len(synced_ids)


# Global instance
sync_manager = SheetsSyncManager()
