"""
Helper script to populate realistic sample stock count records into inventory.db.
Useful for testing Excel reports, statistics, and Google Sheets sync without manual entry.
"""

import asyncio
import random
import database


SAMPLE_ITEMS = [
    ("G101", "8850124001123", "Coca Cola Original 325ml", 24),
    ("G101", "8850124002234", "Coca Cola Zero Sugar 325ml", 18),
    ("G101", "8850125003345", "Sprite Lemon-Lime 325ml", 12),
    ("G102", "8850188201201", "Lay's Classic Potato Chips 50g", 36),
    ("G102", "8850188202302", "Lay's Nori Seaweed 50g", 30),
    ("G102", "8850188203403", "Lay's BBQ Ribs 50g", 15),
    ("G103", "8850999010012", "Oishi Green Tea Honey Lemon 500ml", 24),
    ("G103", "8850999020023", "Oishi Green Tea Genmai 500ml", 12),
    ("G104", "8851932301011", "Mama Instant Noodles Tom Yum 60g", 60),
    ("G104", "8851932302022", "Mama Instant Noodles Minced Pork 60g", 48),
    ("G105", "8850329112233", "Dutch Mill Strawberry Yogurt 140g", 20),
    ("G105", "8850329113344", "Dutch Mill Mixed Berry Yogurt 140g", 16),
]

CREW_MEMBERS = [
    (101, "Somchai (Aisle 1)"),
    (102, "Nok (Aisle 2)"),
    (103, "Anan (Aisle 3)")
]


async def seed():
    print("🌱 Initializing database and inserting sample stock count data...")
    await database.init_db()

    for shelf, barcode, name, qty in SAMPLE_ITEMS:
        user_id, crew_name = random.choice(CREW_MEMBERS)
        # Add slight variation to quantity
        final_qty = max(1, qty + random.randint(-5, 5))
        await database.insert_count(
            user_id=user_id,
            crew_name=crew_name,
            shelf=shelf,
            barcode=barcode,
            item_name=name,
            qty=final_qty
        )

    stats = await database.get_summary_stats()
    print(f"✅ Successfully seeded {stats['total_skus']} sample items with total {stats['total_qty']} units across {stats['total_shelves']} shelves.")
    print("📊 Run '/export' in Telegram or 'python3 test_export.py' to generate the Excel file.")


if __name__ == "__main__":
    asyncio.run(seed())
