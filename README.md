# roblox-friend-tracker

A self-hosted, dark minimal web app that tracks your Roblox profile and friends list, and detects whenever someone unfriends you.

## Features

- **Profile card** — avatar, display name, username, ID, join date, bio, group count, verified badge
- **Live presence** — Online / In-Game / Offline status with last location
- **Friends grid** — all friends with avatars, links to their profiles
- **Unfriend log** — persistent history of everyone who removed you, with timestamps
- **Stats bar** — friend count, unfriend count, group count
- **Auto-sync** — polls Roblox API every 5 minutes in the background
- **Manual sync** — "Sync now" button with spinner
- **Dark minimal UI** — obsidian background, fade-in animations, no bloat

## Stack

- **Backend** — Python + Flask + SQLite
- **Frontend** — Vanilla HTML/CSS/JS (no frameworks)
- **API** — Roblox public REST APIs (no auth required for public profiles)

## Setup

### 1. Clone

```bash
git clone https://github.com/TonyDotV/roblox-friend-tracker.git
cd roblox-friend-tracker
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run

```bash
python app.py
```

Open your browser and go to: **http://localhost:5000**

The app will:
- Initialize the SQLite database (`tracker.db`) on first run
- Do an immediate sync of your friends list
- Start a background thread that re-syncs every 5 minutes
- Serve the frontend at `http://localhost:5000`

## File Structure

```
roblox-friend-tracker/
|-- app.py              # Flask backend + Roblox API + polling logic
|-- requirements.txt    # Python dependencies
|-- tracker.db          # SQLite database (auto-created on first run)
|-- static/
    |-- index.html      # Full frontend (HTML + CSS + JS)
```

## Notes

- Your Roblox profile must be **public** for the friends list to be accessible
- The app runs entirely on your local machine — no data is sent anywhere else
- To change the poll interval, edit `POLL_INTERVAL` in `app.py` (default: 300 seconds)
- The user ID is hardcoded as `50121854` in `app.py` — change `USER_ID` if needed
