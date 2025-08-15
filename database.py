import sqlite3
import csv
import os

DATABASE_FILE = 'reviews.db'


def get_db_connection():
    """Establishes a connection to the SQLite database."""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row  # This allows accessing columns by name
    return conn


def init_db():
    """Initializes the database, creating tables and populating cards from the CSV."""
    if os.path.exists(DATABASE_FILE):
        print("Database already exists. Skipping initialization.")
        return

    print("Creating new database...")
    conn = get_db_connection()
    cursor = conn.cursor()

    # Create the 'cards' table
    cursor.execute('''
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            input_text TEXT NOT NULL,
            output_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available', -- available, locked, reviewed
            locked_by TEXT,
            locked_at TIMESTAMP
        )
    ''')
    print("Created 'cards' table.")

    # Create the 'reviews' table
    cursor.execute('''
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            reviewer_name TEXT NOT NULL,
            decision TEXT NOT NULL, -- good, bad
            reason TEXT,
            time_spent INTEGER,
            reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (card_id) REFERENCES cards (id)
        )
    ''')
    print("Created 'reviews' table.")

    # Populate the 'cards' table from the CSV file
    print("Populating cards from corrected_UAB_names.csv...")
    try:
        with open('corrected_UAB_names.csv', 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            cards_to_insert = [
                (row['Value'], row['Model Corrected Name']) for row in reader
            ]

        cursor.executemany(
            'INSERT INTO cards (input_text, output_text) VALUES (?, ?)',
            cards_to_insert
        )
        conn.commit()
        print(f"Successfully inserted {len(cards_to_insert)} cards.")
    except FileNotFoundError:
        print("Error: corrected_UAB_names.csv not found. Please add it to the directory.")
    except Exception as e:
        print(f"An error occurred during population: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == '__main__':
    # This allows you to run 'python database.py' from your terminal to set everything up.
    init_db()