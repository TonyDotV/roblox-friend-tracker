from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import requests
import threading
import time
import os
from datetime import datetime, timezone

app = Flask(__name__, static_folder='static')
CORS(app)

USER_ID = 50121854
DB_PATH = 'tracker.db'
POLL_INTERVAL = 300  # seconds (5 minutes)

HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json'
}

# -- Database setup ---------------------------------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            display_name TEXT,
            avatar_url   TEXT,
            first_seen   TEXT,
            last_seen    TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS unfriend_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            username     TEXT,
            display_name TEXT,
            avatar_url   TEXT,
            detected_at  TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            taken_at     TEXT,
            friend_count INTEGER
        )
    ''')
    con.commit()
    con.close()

# -- Roblox API helpers -----------------------------------------------
def roblox_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[roblox_get] {url} -> {e}')
        return None

def get_presence():
    try:
        r = requests.post(
            'https://presence.roblox.com/v1/presence/users',
            json={'userIds': [USER_ID]},
            headers=HEADERS,
            timeout=10
        )
        data = r.json()
        users = data.get('userPresences', [])
        if users:
            p = users[0]
            pt = p.get('userPresenceType', 0)
            status_map = {0: 'Offline', 1: 'Online', 2: 'In-Game', 3: 'In Studio'}
            return {
                'status': status_map.get(pt, 'Offline'),
                'lastLocation': p.get('lastLocation', ''),
                'gameId': p.get('rootPlaceId'),
                'lastOnline': p.get('lastOnline')
            }
    except Exception as e:
        print(f'[get_presence] {e}')
    return {'status': 'Offline', 'lastLocation': '', 'gameId': None, 'lastOnline': None}

def get_avatar_url(uid):
    data = roblox_get(
        f'https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={uid}&size=150x150&format=Png'
    )
    if data and data.get('data'):
        return data['data'][0].get('imageUrl', '')
    return ''

def fetch_friends():
    data = roblox_get(f'https://friends.roblox.com/v1/users/{USER_ID}/friends')
    if data is None:
        return None  # API error
    # Deduplicate by user_id (Roblox API can return duplicates)
    seen = set()
    friends = []
    for f in data.get('data', []):
        uid = f['id']
        if uid not in seen:
            seen.add(uid)
            friends.append({
                'user_id': uid,
                'username': f.get('name', ''),
                'display_name': f.get('displayName', '')
            })
    return friends

def get_friend_avatars_batch(user_ids):
    if not user_ids:
        return {}
    ids_str = ','.join(str(i) for i in user_ids)
    data = roblox_get(
        f'https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={ids_str}&size=150x150&format=Png'
    )
    result = {}
    if data and data.get('data'):
        for item in data['data']:
            result[item['targetId']] = item.get('imageUrl', '')
    return result

# -- Friend tracking logic --------------------------------------------
def sync_friends():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = datetime.now(timezone.utc).isoformat()

    live_friends = fetch_friends()
    if live_friends is None:
        print('[sync] Skipping sync due to API error')
        con.close()
        return

    live_ids = {f['user_id'] for f in live_friends}

    # Get known friends from DB
    cur.execute('SELECT user_id FROM friends')
    known_ids = {row[0] for row in cur.fetchall()}

    # Fetch avatars for all live friends
    avatars = get_friend_avatars_batch(list(live_ids))

    # Detect unfriends (were in DB, no longer in live list)
    gone_ids = known_ids - live_ids
    for uid in gone_ids:
        cur.execute('SELECT username, display_name, avatar_url FROM friends WHERE user_id=?', (uid,))
        row = cur.fetchone()
        if row:
            cur.execute(
                'INSERT INTO unfriend_log (user_id, username, display_name, avatar_url, detected_at) VALUES (?,?,?,?,?)',
                (uid, row[0], row[1], row[2], now)
            )
            cur.execute('DELETE FROM friends WHERE user_id=?', (uid,))
            print(f'[unfriend] {row[0]} removed at {now}')

    # Upsert friends: INSERT OR IGNORE to create row, then UPDATE to refresh data.
    # This is safe against duplicates in the API response and re-runs.
    for f in live_friends:
        uid = f['user_id']
        avatar = avatars.get(uid, '')
        # Insert new friend (ignored if already exists, preserving first_seen)
        cur.execute(
            'INSERT OR IGNORE INTO friends (user_id, username, display_name, avatar_url, first_seen, last_seen) VALUES (?,?,?,?,?,?)',
            (uid, f['username'], f['display_name'], avatar, now, now)
        )
        # Always update mutable fields
        cur.execute(
            'UPDATE friends SET username=?, display_name=?, avatar_url=?, last_seen=? WHERE user_id=?',
            (f['username'], f['display_name'], avatar, now, uid)
        )

    # Snapshot
    cur.execute('INSERT INTO snapshots (taken_at, friend_count) VALUES (?,?)', (now, len(live_friends)))
    con.commit()
    con.close()
    print(f'[sync] Done. {len(live_friends)} friends, {len(gone_ids)} unfriended.')

def polling_loop():
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            sync_friends()
        except Exception as e:
            print(f'[polling_loop] {e}')

# -- API Routes -------------------------------------------------------
@app.route('/api/profile')
def api_profile():
    data = roblox_get(f'https://users.roblox.com/v1/users/{USER_ID}')
    if not data:
        return jsonify({'error': 'Failed to fetch profile'}), 500
    presence = get_presence()
    avatar = get_avatar_url(USER_ID)
    groups = roblox_get(f'https://groups.roblox.com/v1/users/{USER_ID}/groups/roles')
    group_count = len(groups.get('data', [])) if groups else 0
    badges = roblox_get(f'https://accountinformation.roblox.com/v1/users/{USER_ID}/roblox-badges')
    return jsonify({
        'id': data.get('id'),
        'username': data.get('name'),
        'displayName': data.get('displayName'),
        'description': data.get('description', ''),
        'created': data.get('created'),
        'isBanned': data.get('isBanned', False),
        'hasVerifiedBadge': data.get('hasVerifiedBadge', False),
        'avatarUrl': avatar,
        'presence': presence,
        'groupCount': group_count,
        'robloxBadges': badges if badges else []
    })

@app.route('/api/friends')
def api_friends():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute('SELECT user_id, username, display_name, avatar_url, first_seen, last_seen FROM friends ORDER BY username')
    rows = cur.fetchall()
    con.close()
    return jsonify([{
        'userId': r[0], 'username': r[1], 'displayName': r[2],
        'avatarUrl': r[3], 'firstSeen': r[4], 'lastSeen': r[5]
    } for r in rows])

@app.route('/api/unfriends')
def api_unfriends():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute('SELECT user_id, username, display_name, avatar_url, detected_at FROM unfriend_log ORDER BY detected_at DESC')
    rows = cur.fetchall()
    con.close()
    return jsonify([{
        'userId': r[0], 'username': r[1], 'displayName': r[2],
        'avatarUrl': r[3], 'detectedAt': r[4]
    } for r in rows])

@app.route('/api/stats')
def api_stats():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute('SELECT COUNT(*) FROM friends')
    friend_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM unfriend_log')
    unfriend_count = cur.fetchone()[0]
    cur.execute('SELECT taken_at, friend_count FROM snapshots ORDER BY taken_at DESC LIMIT 30')
    snapshots = [{'at': r[0], 'count': r[1]} for r in cur.fetchall()]
    con.close()
    return jsonify({
        'friendCount': friend_count,
        'unfriendCount': unfriend_count,
        'snapshots': snapshots
    })

@app.route('/api/sync', methods=['POST'])
def api_sync():
    try:
        sync_friends()
        return jsonify({'ok': True, 'message': 'Sync complete'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')

# -- Main -------------------------------------------------------------
if __name__ == '__main__':
    init_db()
    sync_friends()  # initial sync on startup
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()
    print('[server] Running at http://localhost:5000')
    app.run(host='0.0.0.0', port=5000, debug=False)
