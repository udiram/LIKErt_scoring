from flask import Flask, session, request, redirect, render_template, url_for, flash
import sqlite3
import csv
import os
import time
import datetime
from datetime import timedelta
from sqlalchemy import func, desc
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY',)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# A lock is considered "stale" or abandoned if it's older than this (in seconds)
STALE_LOCK_TIMEOUT = 300  # 5 minutes

GOOD_REASONS = {'1': 'Perfect', '2': 'Stylistic Difference'}
BAD_REASONS = {
    '1': 'No context, I would be equally confused.',
    '2': 'Something misinterpreted, I could understand it, the model didn\'t',
    '3': 'Completely incorrect'
}

class Card(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    input_text = db.Column(db.Text, nullable=False)
    output_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default='available')
    locked_by = db.Column(db.String(100))
    locked_at = db.Column(db.DateTime)

class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey('card.id'), nullable=False)
    reviewer_name = db.Column(db.String(100), nullable=False)
    decision = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.String(255))
    time_spent = db.Column(db.Integer)
    reviewed_at = db.Column(db.DateTime, default=datetime.datetime.now)

def init_db():
    """Initializes the database, creating tables and populating cards from the CSV."""
    DATABASE_FILE = 'instance/reviews.db'
    print("Initializing database...")
    if os.path.exists(DATABASE_FILE):
        print("Database already exists. Skipping initialization.")
        return

    print("Creating new database...")
    with app.app_context():
        db.create_all()
        print("Created 'cards' and 'reviews' tables.")

        # Populate the 'cards' table from the CSV file
        print("Populating cards from corrected_UAB_names.csv...")
        try:
            with open('corrected_UAB_names.csv', 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                cards_to_insert = [
                    Card(input_text=row['Value'], output_text=row['Model Corrected Name'])
                    for row in reader
                ]
                db.session.bulk_save_objects(cards_to_insert)
                db.session.commit()
                print(f"Successfully inserted {len(cards_to_insert)} cards.")
        except FileNotFoundError:
            print("Error: corrected_UAB_names.csv not found. Please add it to the directory.")
        except Exception as e:
            print(f"An error occurred during population: {e}")
            db.session.rollback()

@app.before_request
def release_stale_locks():
    """
    Run before each request to release any locks that have timed out.
    This prevents cards from being stuck if a user closes their browser.
    """
    stale_time = datetime.datetime.now() - timedelta(seconds=STALE_LOCK_TIMEOUT)
    db.session.query(Card).filter(
        Card.status == 'locked',
        Card.locked_at < stale_time
    ).update({
        Card.status: 'available',
        Card.locked_by: None,
        Card.locked_at: None
    })
    db.session.commit()

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
    card = None

    # 1. Check if this user already has a card locked out
    locked_card = Card.query.filter_by(status='locked', locked_by=reviewer_name).first()
    if locked_card:
        card = {
            'id': locked_card.id,
            'input': locked_card.input_text,
            'output': locked_card.output_text
        }

    if not card:
        # 2. Find a new, available card and lock it atomically.
        available_card = Card.query.filter(
            Card.status == 'available',
            ~Card.id.in_(db.session.query(Review.card_id).filter(Review.reviewer_name == reviewer_name))
        ).with_for_update().first()

        if available_card:
            available_card.status = 'locked'
            available_card.locked_by = reviewer_name
            available_card.locked_at = datetime.datetime.now()
            db.session.commit()
            card = {
                'id': available_card.id,
                'input': available_card.input_text,
                'output': available_card.output_text
            }

    # Count the number of reviews completed by this user
    review_count = Review.query.filter_by(reviewer_name=reviewer_name).count()

    # Get leaderboard: top 5 reviewers by review count
    leaderboard_query = db.session.query(
        Review.reviewer_name.label('name'),
        func.count(Review.id).label('count'),
    ).group_by(Review.reviewer_name).order_by(desc('count')).limit(5).all()
    leaderboard = [{'name': row.name, 'count': row.count} for row in leaderboard_query]

    # Calculate remaining cards for this user
    remaining = Card.query.filter(
        db.or_(
            Card.status == 'available',
            db.and_(Card.status == 'locked', Card.locked_by == reviewer_name)
        ),
        ~Card.id.in_(db.session.query(Review.card_id).filter(Review.reviewer_name == reviewer_name))
    ).count()

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

    review = Review(
        card_id=card_id,
        reviewer_name=reviewer_name,
        decision=status,
        reason=reason,
        time_spent=time_spent
    )
    db.session.add(review)

    card_obj = Card.query.get(card_id)
    card_obj.status = 'reviewed'
    card_obj.locked_by = None
    card_obj.locked_at = None

    db.session.commit()

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

        card = Card.query.filter_by(id=card_id, locked_by=reviewer_name).first()
        if card:
            card.status = 'available'
            card.locked_by = None
            card.locked_at = None
            db.session.commit()
            flash('Card successfully returned to the pool!', 'success')
        else:
            flash('Card could not be undone. It may no longer be locked.', 'warning')

        session.pop('card_id', None)
        session.pop('start_time', None)

    return redirect(url_for('review'))


from flask.cli import AppGroup

init_cli = AppGroup('init')

@init_cli.command('db')
def init_db_command():
    with app.app_context():
        init_db()
        print('Initialized the database.')

app.cli.add_command(init_cli)

if __name__ == '__main__':
    with app.app_context():
        init_db()  # Initialize database and populate all cards
    app.run(debug=False)