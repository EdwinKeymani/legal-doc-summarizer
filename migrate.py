"""
One-time migration: adds reset_token and reset_token_expiry columns
to the existing User table, without deleting any existing data.
"""

import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "legal.db")

if not os.path.exists(DB_PATH):
    print(f"No database found at {DB_PATH}. Nothing to migrate.")
    exit()

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(user)")
existing_columns = [row[1] for row in cursor.fetchall()]

if "reset_token" not in existing_columns:
    cursor.execute("ALTER TABLE user ADD COLUMN reset_token VARCHAR(64)")
    print("Added column: reset_token")
else:
    print("Column reset_token already exists, skipping.")

if "reset_token_expiry" not in existing_columns:
    cursor.execute("ALTER TABLE user ADD COLUMN reset_token_expiry DATETIME")
    print("Added column: reset_token_expiry")
else:
    print("Column reset_token_expiry already exists, skipping.")

conn.commit()
conn.close()

print("Migration complete. Your existing users and documents are untouched.")
