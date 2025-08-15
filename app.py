from flask import Flask, session, request, redirect, render_template, url_for, flash, get_flashed_messages
import sqlite3
import time
import os
import subprocess
from database import get_db_connection  # Import our new helper

app = Flask(__name__)
app.secret_key = 'a-much-better-secret-key-in-production'

# A lock is considered "stale" or abandoned if it's older than this (in seconds)
STALE_LOCK_TIMEOUT = 300  # 5 minutes

GOOD_REASONS = {'1': 'Perfect', '2': 'Stylistic Difference'}
BAD_REASONS = {
    '1': 'No context, I would be equally confused.',
    '2': 'Something misinterpreted, I could understand it, the model didn\'t',
    '3': 'Completely incorrect'
}

@app.before_request
def release_stale_locks():
    """
    Run before each request to release any locks that have timed out.
    This prevents cards from being stuck if a user closes their browser.
    """
    conn = get_db_connection()
    # The '?' substitution calculates the timestamp on the fly
    conn.execute(
        "UPDATE cards SET status = 'available', locked_by = NULL, locked_at = NULL WHERE status = 'locked' AND locked_at < datetime('now', '-' || ? || ' seconds')",
        (STALE_LOCK_TIMEOUT,)
    )
    conn.commit()
    conn.close()

@app.route('/')
def index():
    if 'name' in session:
        return redirect(url_for('review'))
    return render_template('index.html')

@app.route('/set_name', methods=['POST'])
def set_name():
    name = request.form.get('name')
    if name:
        session['name'] = name
    return redirect(url_for('review'))

@app.route('/review')
def review():
    if 'name' not in session:
        return redirect(url_for('index'))

    reviewer_name = session['name']
    conn = get_db_connection()
    card = None

    # 1. Check if this user already has a card locked out
    card = conn.execute(
        "SELECT id, input_text AS input, output_text AS output FROM cards WHERE status = 'locked' AND locked_by = ?",
        (reviewer_name,)
    ).fetchone()

    if not card:
        # 2. If not, find a new, available card and lock it atomically.
        # This is the core of the concurrency solution.
        with conn:  # 'with conn' creates a transaction
            # Find an available card that this user has NEVER reviewed before.
            available_card = conn.execute(
                """
                SELECT id FROM cards
                WHERE status = 'available' AND id NOT IN (
                    SELECT card_id FROM reviews WHERE reviewer_name = ?
                )
                LIMIT 1
                """,
                (reviewer_name,)
            ).fetchone()

            if available_card:
                card_id = available_card['id']
                # Lock the found card for the current user
                conn.execute(
                    "UPDATE cards SET status = 'locked', locked_by = ?, locked_at = datetime('now') WHERE id = ?",
                    (reviewer_name, card_id)
                )
                # Fetch the card data to display
                card = conn.execute(
                    "SELECT id, input_text AS input, output_text AS output FROM cards WHERE id = ?",
                    (card_id,)
                ).fetchone()

    # Count the number of reviews completed by this user
    review_count = conn.execute(
        "SELECT COUNT(*) AS count FROM reviews WHERE reviewer_name = ?",
        (reviewer_name,)
    ).fetchone()['count']

    # Get leaderboard: top 5 reviewers by review count
    leaderboard = conn.execute(
        """
        SELECT reviewer_name AS name, COUNT(*) AS count
        FROM reviews
        GROUP BY reviewer_name
        ORDER BY count DESC
        LIMIT 5
        """
    ).fetchall()

    # Calculate remaining cards for this user
    remaining_query = """
    SELECT COUNT(*) AS count FROM cards
    WHERE status IN ('available', 'locked') 
    AND (status = 'available' OR locked_by = ?)
    AND id NOT IN (
        SELECT card_id FROM reviews WHERE reviewer_name = ?
    )
    """
    remaining = conn.execute(remaining_query, (reviewer_name, reviewer_name)).fetchone()['count']

    conn.close()

    if not card:
        # No available cards left for this user to review
        return render_template('done.html', name=session['name'], leaderboard=leaderboard)

    session['card_id'] = card['id']
    session['start_time'] = time.time()

    # The 'undo' button is now shown if a user has a card checked out
    has_pending = 'card_id' in session
    return render_template('review.html', card=card, has_pending=has_pending, review_count=review_count, leaderboard=leaderboard, remaining=remaining)

@app.route('/decide', methods=['POST'])
def decide():
    if 'name' not in session or 'card_id' not in session:
        return 'Unauthorized', 401

    reviewer_name = session['name']
    card_id = session['card_id']
    time_spent = int(time.time() - session.get('start_time', time.time()))

    direction = request.form['direction']
    status = 'good' if direction == 'right' else 'bad'
    reason_key = request.form.get('reason')
    reason = GOOD_REASONS.get(reason_key, '') if status == 'good' else BAD_REASONS.get(reason_key, '')

    conn = get_db_connection()
    with conn:  # Transaction
        # 1. Insert the review
        conn.execute(
            "INSERT INTO reviews (card_id, reviewer_name, decision, reason, time_spent) VALUES (?, ?, ?, ?, ?)",
            (card_id, reviewer_name, status, reason, time_spent)
        )
        # 2. Mark the card as permanently reviewed
        conn.execute(
            "UPDATE cards SET status = 'reviewed', locked_by = NULL, locked_at = NULL WHERE id = ?",
            (card_id,)
        )
    conn.close()

    # Clear the session variables for this card
    session.pop('card_id', None)
    session.pop('start_time', None)

    return 'OK', 200

@app.route('/undo', methods=['POST'])
def undo():
    """
    Effectively "skips" the current card by releasing the lock.
    It goes back into the 'available' pool for someone else (or this user later).
    """
    if 'name' in session and 'card_id' in session:
        reviewer_name = session['name']
        card_id = session['card_id']

        conn = get_db_connection()
        cursor = conn.execute(
            "UPDATE cards SET status = 'available', locked_by = NULL, locked_at = NULL WHERE id = ? AND locked_by = ?",
            (card_id, reviewer_name)
        )
        conn.commit()
        if cursor.rowcount > 0:
            flash('Card successfully returned to the pool!', 'success')
        else:
            flash('Card could not be undone. It may no longer be locked.', 'warning')
        conn.close()

        session.pop('card_id', None)
        session.pop('start_time', None)

    return redirect(url_for('review'))

if __name__ == '__main__':
    app.run(debug=False)