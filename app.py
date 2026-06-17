from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import sqlite3
import requests
import threading
import time
import os
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)

# Configuration
USER_ID = 50121854
DB_PATH = 'tracker.db'
POLL_INTERVAL = 300  # seconds (5 minutes)
API_TIMEOUT = 10  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json'
}

# Global variables
polling_active = False
polling_thread = None
shutdown_event = threading.Event()

# -- Database Context Manager -------------------------------------------
@contextmanager
def get_db():
    """Context manager for database connections."""
    con = None
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        yield con
        con.commit()
    except sqlite3.Error as e:
        if con:
            con.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        if con:
            con.close()

# -- Database Setup -----------------------------------------------------
def init_db():
    """Initialize database with required tables."""
    try:
        with get_db() as con:
            cur = con.cursor()
            
            # Friends table
            cur.execute('''
                CREATE TABLE IF NOT EXISTS friends (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    avatar_url TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                )
            ''')
            
            # Unfriend log table
            cur.execute('''
                CREATE TABLE IF NOT EXISTS unfriend_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    display_name TEXT,
                    unfriend_time TEXT,
                    was_friend_since TEXT
                )
            ''')
            
            # Snapshots table
            cur.execute('''
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    taken_at TEXT,
                    friend_count INTEGER
                )
            ''')
            
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# -- API Helper Functions -----------------------------------------------
def make_api_request(url: str, max_retries: int = MAX_RETRIES) -> Optional[Dict[str, Any]]:
    """Make API request with retry logic."""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=API_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning(f"API request failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                logger.error(f"API request failed after {max_retries} attempts")
                return None
    return None

def fetch_friends_page(user_id: int, cursor: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch a single page of friends data."""
    url = f'https://friends.roblox.com/v1/users/{user_id}/friends'
    if cursor:
        url += f'?cursor={cursor}'
    return make_api_request(url)

def fetch_all_friends(user_id: int) -> List[Dict[str, Any]]:
    """Fetch all friends using pagination."""
    all_friends = []
    cursor = None
    
    while True:
        data = fetch_friends_page(user_id, cursor)
        if not data:
            break
            
        friends = data.get('data', [])
        all_friends.extend(friends)
        
        cursor = data.get('nextPageCursor')
        if not cursor:
            break
            
        # Small delay to avoid rate limiting
        time.sleep(0.5)
    
    logger.info(f"Fetched {len(all_friends)} friends")
    return all_friends

def fetch_user_info(user_id: int) -> Optional[Dict[str, Any]]:
    """Fetch detailed user information."""
    url = f'https://users.roblox.com/v1/users/{user_id}'
    return make_api_request(url)

def fetch_avatar_headshot(user_id: int) -> Optional[str]:
    """Fetch user avatar headshot URL."""
    url = f'https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png'
    data = make_api_request(url)
    if data and data.get('data'):
        return data['data'][0].get('imageUrl')
    return None

# -- Core Sync Logic ----------------------------------------------------
def sync_friends():
    """Synchronize friends data with database."""
    try:
        logger.info("Starting friend sync...")
        current_time = datetime.now(timezone.utc).isoformat()
        
        # Fetch current friends from API
        api_friends = fetch_all_friends(USER_ID)
        if not api_friends:
            logger.warning("No friends data received from API")
            return
        
        api_friend_ids = {f['id'] for f in api_friends}
        
        with get_db() as con:
            cur = con.cursor()
            
            # Get existing friends from database
            cur.execute('SELECT user_id, username, display_name, first_seen FROM friends')
            db_friends = {row['user_id']: dict(row) for row in cur.fetchall()}
            db_friend_ids = set(db_friends.keys())
            
            # Find new friends and unfriended users
            new_friend_ids = api_friend_ids - db_friend_ids
            unfriended_ids = db_friend_ids - api_friend_ids
            
            # Log new friends
            if new_friend_ids:
                logger.info(f"Found {len(new_friend_ids)} new friends")
                for friend in api_friends:
                    if friend['id'] in new_friend_ids:
                        avatar_url = fetch_avatar_headshot(friend['id'])
                        cur.execute('''
                            INSERT OR REPLACE INTO friends 
                            (user_id, username, display_name, avatar_url, first_seen, last_seen)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            friend['id'],
                            friend.get('name', ''),
                            friend.get('displayName', ''),
                            avatar_url,
                            current_time,
                            current_time
                        ))
            
            # Log unfriended users
            if unfriended_ids:
                logger.info(f"Found {len(unfriended_ids)} unfriended users")
                for user_id in unfriended_ids:
                    friend_data = db_friends[user_id]
                    cur.execute('''
                        INSERT INTO unfriend_log 
                        (user_id, username, display_name, unfriend_time, was_friend_since)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        friend_data['username'],
                        friend_data['display_name'],
                        current_time,
                        friend_data['first_seen']
                    ))
                    # Remove from friends table
                    cur.execute('DELETE FROM friends WHERE user_id = ?', (user_id,))
            
            # Update last_seen for all current friends
            for friend in api_friends:
                if friend['id'] not in new_friend_ids:
                    cur.execute('''
                        UPDATE friends 
                        SET last_seen = ?, username = ?, display_name = ?
                        WHERE user_id = ?
                    ''', (current_time, friend.get('name', ''), friend.get('displayName', ''), friend['id']))
            
            # Create snapshot
            cur.execute('''
                INSERT INTO snapshots (taken_at, friend_count)
                VALUES (?, ?)
            ''', (current_time, len(api_friend_ids)))
            
        logger.info("Friend sync completed successfully")
        
    except Exception as e:
        logger.error(f"Error during friend sync: {e}", exc_info=True)

# -- Polling Loop -------------------------------------------------------
def polling_loop():
    """Background polling loop."""
    global polling_active
    logger.info("Polling loop started")
    
    while not shutdown_event.is_set():
        try:
            sync_friends()
        except Exception as e:
            logger.error(f"Error in polling loop: {e}", exc_info=True)
        
        # Wait with interrupt check
        for _ in range(POLL_INTERVAL):
            if shutdown_event.is_set():
                break
            time.sleep(1)
    
    polling_active = False
    logger.info("Polling loop stopped")

# -- API Endpoints ------------------------------------------------------
@app.route('/api/friends', methods=['GET'])
def api_friends():
    """Get all current friends."""
    try:
        with get_db() as con:
            cur = con.cursor()
            cur.execute('''
                SELECT user_id, username, display_name, avatar_url, first_seen, last_seen
                FROM friends
                ORDER BY first_seen DESC
            ''')
            friends = [dict(row) for row in cur.fetchall()]
        return jsonify(friends)
    except Exception as e:
        logger.error(f"Error fetching friends: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/unfriends', methods=['GET'])
def api_unfriends():
    """Get unfriend log."""
    try:
        with get_db() as con:
            cur = con.cursor()
            cur.execute('''
                SELECT id, user_id, username, display_name, unfriend_time, was_friend_since
                FROM unfriend_log
                ORDER BY unfriend_time DESC
            ''')
            unfriends = [dict(row) for row in cur.fetchall()]
        return jsonify(unfriends)
    except Exception as e:
        logger.error(f"Error fetching unfriends: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get statistics."""
    try:
        with get_db() as con:
            cur = con.cursor()
            
            # Current friend count
            cur.execute('SELECT COUNT(*) as count FROM friends')
            friend_count = cur.fetchone()['count']
            
            # Total unfriends
            cur.execute('SELECT COUNT(*) as count FROM unfriend_log')
            unfriend_count = cur.fetchone()['count']
            
            # Recent snapshots (last 30)
            cur.execute('''
                SELECT taken_at, friend_count 
                FROM snapshots 
                ORDER BY taken_at DESC 
                LIMIT 30
            ''')
            snapshots = [{'at': r['taken_at'], 'count': r['friend_count']} for r in cur.fetchall()]
            
        return jsonify({
            'friendCount': friend_count,
            'unfriendCount': unfriend_count,
            'snapshots': list(reversed(snapshots)),
            'pollingActive': polling_active
        })
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """Manually trigger a sync."""
    try:
        sync_friends()
        return jsonify({'ok': True, 'message': 'Sync complete'})
    except Exception as e:
        logger.error(f"Error during manual sync: {e}")
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/status', methods=['GET'])
def api_status():
    """Get application status."""
    return jsonify({
        'ok': True,
        'pollingActive': polling_active,
        'userId': USER_ID,
        'pollInterval': POLL_INTERVAL
    })

# -- Frontend Serving ---------------------------------------------------
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """Serve frontend files."""
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')

# -- Main ---------------------------------------------------------------
if __name__ == '__main__':
    try:
        logger.info("Starting Roblox Friend Tracker")
        
        # Initialize database
        init_db()
        
        # Initial sync
        logger.info("Performing initial friend sync...")
        sync_friends()
        
        # Start polling thread
        polling_active = True
        polling_thread = threading.Thread(target=polling_loop, daemon=True)
        polling_thread.start()
        
        # Start Flask app
        logger.info("Starting Flask server on http://0.0.0.0:5000")
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
        
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
        shutdown_event.set()
        if polling_thread:
            polling_thread.join(timeout=5)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
