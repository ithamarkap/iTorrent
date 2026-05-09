# iTorrent
A BitTorrent client built in Python with Electron GUI.

## Features
- Torrent file and magnet link support
- UDP tracker peer discovery
- BitTorrent protocol implementation (handshake, bitfield, piece requests)
- Multi-file torrent handling
- Download progress tracking with GUI dashboard
- Pause/resume/remove torrents
- Real-time logs and stats

## Requirements
- Python 3.8+
- Node.js 18+ (for Electron GUI)

## Installation
1. Clone/download the repo
2. Install Python and Node.js from the internet
3. Python deps:
   ```
   python -m venv venv
   venv\Scripts\activate  # Windows
   # source venv/bin/activate  # Linux/Mac
   pip install -r requirements.txt
   ```
4. GUI deps:
   ```
   cd gui
   npm install
   ```

## Quick Start (using the built-in Electron GUI)
2. Start Electron GUI:
   ```
   cd gui/
   npm start
   ```

## Quick Start (using browser)
1. Start Flask backend:
   ```
   python gui/app.py
   ```

1. Open your favorite browser and navigate to:
   ```
   http://127.0.0.1:5000
   ```

## Development
- Core: root Python files (torrent_client.py etc.)
- Backend API: gui/app.py (Flask)
- Frontend: gui/static/, gui/templates/index.html
- Electron: gui/electron/main.js

## Project Status
Early development, work in progress.

Enjoy torrenting responsibly! 🚀
