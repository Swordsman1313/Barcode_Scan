"""
Telegram Stock Count Bot.
Designed for store crew counting stock (especially unscannable/damaged barcode items).
Supports multi-user concurrency, instant Google Sheets auto-fill, and Excel export.
"""

import os
import uuid
import logging
from io import BytesIO
from typing import Optional

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

import config
import database
from sheets_sync import sync_manager
from barcode_reader import detect_barcode_from_image
from excel_exporter import create_excel_report

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
(
    STATE_FRONT_PHOTO,
    STATE_BARCODE_PHOTO,
    STATE_SHELF,
    STATE_BARCODE,
    STATE_ITEM_NAME,
    STATE_QTY
) = range(6)


# ----------------------------------------------------
# Helper Functions
# ----------------------------------------------------
def get_user_display_name(update: Update) -> str:
    """Extracts a friendly name for the crew member."""
    user = update.effective_user
    if not user:
        return "Unknown"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if user.username:
        return f"{full_name} (@{user.username})" if full_name else f"@{user.username}"
    return full_name or f"User_{user.id}"


async def save_telegram_photo(file_id: str, context: ContextTypes.DEFAULT_TYPE, prefix: str = "img") -> str:
    """Downloads a photo from Telegram and saves it locally."""
    try:
        tg_file = await context.bot.get_file(file_id)
        filename = f"{prefix}_{uuid.uuid4().hex[:8]}.jpg"
        file_path = str(config.PHOTOS_DIR / filename)
        await tg_file.download_to_drive(custom_path=file_path)
        return file_path
    except Exception as e:
        logger.error(f"Error saving photo: {e}")
        return ""


def get_quick_qty_keyboard() -> InlineKeyboardMarkup:
    """Builds inline buttons for fast quantity selection."""
    keyboard = [
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
    ]
    return InlineKeyboardMarkup(keyboard)


# ----------------------------------------------------
# Command Handlers
# ----------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends greeting and instructions."""
    user = update.effective_user
    active_shelf = await database.get_user_active_shelf(user.id) if user else None
    shelf_text = f"📍 *Active Shelf:* `{active_shelf}`" if active_shelf else "📍 *Active Shelf:* _Not set yet_"

    msg = (
        f"👋 *Welcome to Store Stock Count Bot!*\n\n"
        f"Designed to log stock items with damaged or unscannable barcodes quickly.\n\n"
        f"{shelf_text}\n\n"
        f"🚀 *How to Count Stock:*\n"
        f"1️⃣ Simply **send a photo of the item** (or type /count)\n"
        f"2️⃣ Send or skip barcode photo\n"
        f"3️⃣ Confirm/Type Shelf (e.g. `G101`)\n"
        f"4️⃣ Confirm/Type Barcode number\n"
        f"5️⃣ Enter Item Name\n"
        f"6️⃣ Select/Type Quantity (QTY)\n\n"
        f"📌 *Useful Commands:*\n"
        f"• `/shelf <code?>` — Set or view your active shelf (e.g. `/shelf G101`)\n"
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
    """Sets or views active shelf for the crew member."""
    user = update.effective_user
    if not user:
        return

    if context.args:
        new_shelf = " ".join(context.args).strip().upper()
        await database.set_user_active_shelf(user.id, new_shelf)
        context.user_data["active_shelf"] = new_shelf
        await update.message.reply_text(
            f"✅ *Active shelf updated to:* `{new_shelf}`\n\n"
            f"All your next scanned items will use `{new_shelf}` automatically. Send an item photo to start counting!",
            parse_mode="Markdown"
        )
    else:
        current_shelf = await database.get_user_active_shelf(user.id)
        if current_shelf:
            await update.message.reply_text(
                f"📍 Your current active shelf is: `{current_shelf}`\n"
                f"To change it, type: `/shelf <NEW_SHELF>` (e.g. `/shelf G102`)",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "📍 You have no active shelf set.\n"
                "To set one, type: `/shelf <SHELF_CODE>` (e.g. `/shelf G101`)",
                parse_mode="Markdown"
            )


async def cmd_mycounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows user's recent counts with delete option."""
    user = update.effective_user
    if not user:
        return

    records = await database.get_recent_counts(user_id=user.id, limit=8)
    if not records:
        await update.message.reply_text("ℹ️ You haven't recorded any items yet. Send a photo to start!", parse_mode="Markdown")
        return

    text = "📋 *Your Recent Scanned Items:*\n\n"
    for r in records:
        synced = "✅" if r.get("synced_sheet") == 1 else "⏳"
        text += (
            f"• *ID #{r['id']}* — `{r['shelf']}` | `{r['barcode']}`\n"
            f"   📦 {r.get('item_name') or 'No Name'} (x{r['qty']}) {synced}\n"
            f"   🕒 _{r['timestamp']}_\n\n"
        )
    text += "To delete a mistake, type `/delete <ID>` (e.g. `/delete 12` or `/delete_last`)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the user's most recent count entry."""
    user = update.effective_user
    if not user:
        return

    records = await database.get_recent_counts(user_id=user.id, limit=1)
    if not records:
        await update.message.reply_text("❌ No items found to delete.")
        return

    last_item = records[0]
    success = await database.delete_count(last_item["id"], user_id=user.id)
    if success:
        await update.message.reply_text(
            f"🗑️ *Deleted most recent entry:*\n"
            f"ID #{last_item['id']} — Shelf `{last_item['shelf']}` | Barcode `{last_item['barcode']}` (x{last_item['qty']})",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Failed to delete entry.")


async def cmd_delete_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes a specific count entry by ID."""
    user = update.effective_user
    if not user:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/delete <ID>` (e.g. `/delete 15`)", parse_mode="Markdown")
        return

    count_id = int(context.args[0])
    is_admin = user.id in config.ADMIN_USER_IDS
    success = await database.delete_count(count_id, user_id=None if is_admin else user.id)

    if success:
        await update.message.reply_text(f"🗑️ *Deleted item ID #{count_id} successfully.*", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Could not delete item #{count_id} (not found or not created by you).", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays counting statistics and breakdown."""
    stats = await database.get_summary_stats()
    
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
        text += "🏢 *Breakdown by Shelf (Top 10):*\n"
        for s in stats["shelf_breakdown"][:10]:
            text += f"• `{s['shelf']}`: {s['sku_count']} SKUs ({s['total_qty']:,.0f} units)\n"
        text += "\n"

    if stats["crew_breakdown"]:
        text += "👤 *Breakdown by Crew:*\n"
        for c in stats["crew_breakdown"]:
            text += f"• {c['crew_name']}: {c['sku_count']} SKUs ({c['total_qty']:,.0f} units)\n"
        text += "\n"

    text += "👉 Type `/export` to download full Excel report!"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates and sends the Excel .xlsx file."""
    msg_wait = await update.message.reply_text("⏳ *Generating Excel report...*", parse_mode="Markdown")
    
    try:
        counts = await database.get_all_counts()
        if not counts:
            await msg_wait.edit_text("ℹ️ No stock items recorded yet. Count some items first with `/count`!")
            return

        excel_file = create_excel_report(counts)
        timestamp_str = database.get_current_timestamp().replace(":", "-").replace(" ", "_")
        filename = f"Stock_Count_Report_{timestamp_str}.xlsx"

        await update.message.reply_document(
            document=excel_file,
            filename=filename,
            caption=(
                f"📊 *Stock Count Report Exported!*\n"
                f"• Total Items: `{len(counts)}`\n"
                f"• Generated at: `{database.get_current_timestamp()}`\n"
                f"• Contains: *Stock Items*, *Summary by Shelf*, and *Summary by Crew*."
            ),
            parse_mode="Markdown"
        )
        await msg_wait.delete()

    except Exception as e:
        logger.error(f"Error generating Excel: {e}", exc_info=True)
        await msg_wait.edit_text(f"❌ Error generating Excel file: {e}")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually triggers sync of pending rows to Google Sheets."""
    if not config.GOOGLE_SHEET_WEBHOOK_URL:
        await update.message.reply_text("⚠️ Google Sheet Webhook URL is not configured in `.env`.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("⏳ *Syncing pending items to Google Sheets...*", parse_mode="Markdown")
    count = await sync_manager.sync_pending_records()
    await msg.edit_text(f"✅ *Sync Complete:* `{count}` pending items pushed to Google Sheets.", parse_mode="Markdown")


# ----------------------------------------------------
# Count Conversation Flow
# ----------------------------------------------------
async def flow_start_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the count flow."""
    context.user_data.clear()
    
    # Check if initiated via photo directly
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
        file_path = await save_telegram_photo(photo.file_id, context, prefix="front")
        context.user_data["photo_front"] = file_path
        
        # Try auto-detecting barcode from front photo
        detected = detect_barcode_from_image(file_path)
        if detected:
            context.user_data["detected_barcode"] = detected

        # Move to barcode photo step
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏩ Skip (Barcode is in this photo)", callback_data="skip_barcode_photo")]
        ])
        await update.message.reply_text(
            "📸 *Front Photo Saved!*\n\n"
            "Now please take a close-up photo of the **Barcode label** (or tap Skip if it's already clearly visible):",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return STATE_BARCODE_PHOTO

    await update.message.reply_text(
        "📸 *Step 1/5: Item Photo*\n\n"
        "Please send a photo of the **Front of the product**:",
        parse_mode="Markdown"
    )
    return STATE_FRONT_PHOTO


async def flow_receive_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the front photo."""
    if not update.message.photo:
        await update.message.reply_text("⚠️ Please send an image photo of the product front.")
        return STATE_FRONT_PHOTO

    photo = update.message.photo[-1]
    file_path = await save_telegram_photo(photo.file_id, context, prefix="front")
    context.user_data["photo_front"] = file_path

    # Try barcode auto-detection
    detected = detect_barcode_from_image(file_path)
    if detected:
        context.user_data["detected_barcode"] = detected

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip (Barcode is in this photo)", callback_data="skip_barcode_photo")]
    ])
    await update.message.reply_text(
        "📸 *Front Photo Saved!*\n\n"
        "Now please send a close-up photo of the **Barcode label** (or tap Skip):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return STATE_BARCODE_PHOTO


async def flow_receive_barcode_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the barcode photo."""
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
        file_path = await save_telegram_photo(photo.file_id, context, prefix="barcode")
        context.user_data["photo_barcode"] = file_path

        detected = detect_barcode_from_image(file_path)
        if detected:
            context.user_data["detected_barcode"] = detected

    return await prompt_shelf_step(update, context)


async def flow_skip_barcode_photo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles skipping the barcode photo via inline button."""
    query = update.callback_query
    await query.answer()
    context.user_data["photo_barcode"] = None
    return await prompt_shelf_step(update, context)


async def prompt_shelf_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompts the user for shelf with smart sticky memory."""
    user = update.effective_user
    active_shelf = await database.get_user_active_shelf(user.id) if user else None

    target = update.callback_query.message if update.callback_query else update.message

    if active_shelf:
        context.user_data["shelf_candidate"] = active_shelf
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Keep Shelf {active_shelf}", callback_data=f"keep_shelf_{active_shelf}")],
            [InlineKeyboardButton("✏️ Enter Different Shelf", callback_data="change_shelf")]
        ])
        await target.reply_text(
            f"📍 *Step 2/5: Shelf Location*\n\n"
            f"Current active shelf: `{active_shelf}`\n"
            f"Tap below to keep it, or type a new shelf code (e.g. `G102`):",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        await target.reply_text(
            "📍 *Step 2/5: Shelf Location*\n\n"
            "Please type the **Shelf Code** (e.g. `G101`, `A02`, `B-12`):",
            parse_mode="Markdown"
        )

    return STATE_SHELF


async def flow_shelf_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles shelf inline button clicks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("keep_shelf_"):
        shelf = data.replace("keep_shelf_", "").strip().upper()
        context.user_data["shelf"] = shelf
        return await prompt_barcode_step(update, context)
    elif data == "change_shelf":
        await query.message.reply_text("📍 Please type the new **Shelf Code** (e.g. `G102`):", parse_mode="Markdown")
        return STATE_SHELF

    return STATE_SHELF


async def flow_shelf_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles typed shelf input."""
    shelf = update.message.text.strip().upper()
    if not shelf:
        await update.message.reply_text("⚠️ Shelf code cannot be empty. Please type e.g. `G101`:")
        return STATE_SHELF

    context.user_data["shelf"] = shelf
    user = update.effective_user
    if user:
        await database.set_user_active_shelf(user.id, shelf)

    return await prompt_barcode_step(update, context)


async def prompt_barcode_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompts for barcode (shows auto-detected button if available)."""
    target = update.callback_query.message if update.callback_query else update.message
    detected = context.user_data.get("detected_barcode")

    if detected:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm Barcode: {detected}", callback_data=f"confirm_barcode_{detected}")],
            [InlineKeyboardButton("✏️ Type Different Barcode", callback_data="type_barcode")]
        ])
        await target.reply_text(
            f"🏷️ *Step 3/5: Barcode Number*\n\n"
            f"🔍 *Auto-detected from photo:* `{detected}`\n"
            f"Tap to confirm or type the correct barcode number manually:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        await target.reply_text(
            "🏷️ *Step 3/5: Barcode Number*\n\n"
            "Please type the **Barcode number** from the item label\n"
            "(or type `NO_BARCODE` if completely missing):",
            parse_mode="Markdown"
        )

    return STATE_BARCODE


async def flow_barcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles barcode inline button selection."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("confirm_barcode_"):
        barcode = data.replace("confirm_barcode_", "").strip()
        context.user_data["barcode"] = barcode
        return await prompt_item_name_step(update, context)
    elif data == "type_barcode":
        await query.message.reply_text("🏷️ Please type the **Barcode number**:", parse_mode="Markdown")
        return STATE_BARCODE

    return STATE_BARCODE


async def flow_barcode_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles typed barcode input."""
    barcode = update.message.text.strip()
    if not barcode:
        await update.message.reply_text("⚠️ Barcode cannot be empty. Please type the barcode number:")
        return STATE_BARCODE

    context.user_data["barcode"] = barcode
    return await prompt_item_name_step(update, context)


async def prompt_item_name_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompts for product name or description."""
    target = update.callback_query.message if update.callback_query else update.message
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip / No Name", callback_data="skip_item_name")]
    ])
    await target.reply_text(
        "📦 *Step 4/5: Item Name / Description*\n\n"
        "Please type the **Product Name** (e.g. `Oishi Green Tea 500ml`, `Lay's Classic 50g`):\n"
        "_(Or tap Skip below)_",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return STATE_ITEM_NAME


async def flow_item_name_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles skipping item name."""
    query = update.callback_query
    await query.answer()
    if query.data == "skip_item_name":
        context.user_data["item_name"] = "-"
        return await prompt_qty_step(update, context)
    return STATE_ITEM_NAME


async def flow_item_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles typed item name."""
    name = update.message.text.strip()
    context.user_data["item_name"] = name or "-"
    return await prompt_qty_step(update, context)


async def prompt_qty_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompts for quantity with quick-select buttons."""
    target = update.callback_query.message if update.callback_query else update.message
    keyboard = get_quick_qty_keyboard()
    
    await target.reply_text(
        "🔢 *Step 5/5: Quantity (QTY)*\n\n"
        "Select or type the count quantity for this item on the shelf:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return STATE_QTY


async def flow_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles quick quantity button clicks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("qty_"):
        val = data.replace("qty_", "")
        if val == "custom":
            await query.message.reply_text("🔢 Please type the exact **Quantity (QTY)** number (e.g. `15` or `12`):", parse_mode="Markdown")
            return STATE_QTY
        
        try:
            qty = float(val)
            context.user_data["qty"] = qty
            return await finalize_and_save_count(update, context)
        except ValueError:
            pass

    return STATE_QTY


async def flow_qty_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles typed quantity."""
    text = update.message.text.strip()
    try:
        qty = float(text)
        if qty <= 0:
            await update.message.reply_text("⚠️ Quantity must be greater than 0. Please enter a valid number:")
            return STATE_QTY
        context.user_data["qty"] = qty
        return await finalize_and_save_count(update, context)
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number. Please enter a numeric quantity (e.g. `12` or `5`):")
        return STATE_QTY


async def finalize_and_save_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves the count record to SQLite and triggers background Google Sheet sync."""
    user = update.effective_user
    crew_name = get_user_display_name(update)
    
    shelf = context.user_data.get("shelf", "UNKNOWN")
    barcode = context.user_data.get("barcode", "NO_BARCODE")
    item_name = context.user_data.get("item_name", "-")
    qty = context.user_data.get("qty", 1.0)
    photo_front = context.user_data.get("photo_front")
    photo_barcode = context.user_data.get("photo_barcode")

    # Save to SQLite Database
    record = await database.insert_count(
        user_id=user.id if user else 0,
        crew_name=crew_name,
        shelf=shelf,
        barcode=barcode,
        item_name=item_name,
        qty=qty,
        photo_front=photo_front,
        photo_barcode=photo_barcode
    )

    # Enqueue to Google Sheets background sync worker
    sync_manager.enqueue(record)

    target = update.callback_query.message if update.callback_query else update.message

    qty_display = int(qty) if qty.is_integer() else qty

    summary_card = (
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
        f"_(Active shelf is still `{shelf}`. Type `/shelf` to change)_"
    )

    await target.reply_text(summary_card, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels active count."""
    context.user_data.clear()
    await update.message.reply_text("❌ Count session cancelled. Send a photo or type `/count` anytime to start.", parse_mode="Markdown")
    return ConversationHandler.END


# ----------------------------------------------------
# Main Application Builder
# ----------------------------------------------------
def build_application() -> Application:
    """Constructs and configures the Telegram Bot Application."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is empty! Please set it in your .env file.")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Conversation Handler for Stock Counting
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("count", flow_start_count),
            MessageHandler(filters.PHOTO, flow_start_count)
        ],
        states={
            STATE_FRONT_PHOTO: [
                MessageHandler(filters.PHOTO, flow_receive_front_photo),
                CommandHandler("cancel", flow_cancel)
            ],
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
        fallbacks=[
            CommandHandler("cancel", flow_cancel)
        ],
        per_user=True,
        per_chat=True
    )

    app.add_handler(conv_handler)
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
    """Initializes DB and background sync worker on bot startup."""
    await database.init_db()
    await sync_manager.start()
    logger.info("Database initialized and Sheets sync worker started.")


async def post_shutdown(application: Application):
    """Gracefully shuts down resources."""
    await sync_manager.stop()
    logger.info("Bot shutdown complete.")


def main():
    """Main entry point."""
    print("🚀 Initializing Store Stock Count Telegram Bot...")
    app = build_application()
    app.post_init = post_init
    app.post_shutdown = post_shutdown

    print("🤖 Bot is polling for updates... Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
