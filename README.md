<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/he/thumb/1/15/Ministry_of_Education.svg/1920px-Ministry_of_Education.svg.png" width="100" />
</p>

# 🚀 iTorrent

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-Server-000000?style=for-the-badge&logo=flask&logoColor=white)
![Electron](https://img.shields.io/badge/Electron-GUI-47848F?style=for-the-badge&logo=electron&logoColor=white)

#### My 12th grade final assignment for another 5 units of Computer Science.
A lightweight, custom-built BitTorrent client powered by a Python backend and wrapped in a sleek Electron desktop GUI. 

---

## Features

- 🧲 **Full Support:** Handle standard `.torrent` files and magnet links.
- 📡 **Peer Discovery:** Custom UDP tracker implementation.
- ⚙️ **Native BitTorrent Protocol:** Built from scratch to handle handshakes, bitfields, and piece requests.
- 📂 **Multi-file Torrents:** Seamlessly download and construct complex directory structures.
- 📊 **Interactive Dashboard:** Track download progress with a GUI dashboard featuring real-time logs and statistics.
- ⏯️ **Playback Controls:** Pause, resume, or remove torrents on the fly.

---

## Quick Start: Pre-built Binaries

The easiest way to get started is by using the compiled executables.

### Windows (Portable EXE)
1. Download the latest Windows build executable from the Releases page.
2. Double-click the `.exe` file to open the portable app.
3. Start adding torrent files or magnet links!

### macOS (Apple Silicon only)
*Note: This build is currently compiled for Apple Silicon Macs (M1+, arm64) only.*
1. Download the latest macOS `.dmg` installer.
2. Double-click the `.dmg` file to mount it.
3. Drag **iTorrent** into your **Applications** folder.
4. Open iTorrent from Applications or Spotlight.
5. **Security Bypass (First Run Only):** If macOS blocks the launch:
   - Go to **System Settings → Privacy & Security**.
   - Click **Open Anyway** next to the iTorrent prompt.
   - Confirm and launch iTorrent again.

---

## 💻 Run using the source code (reccommended for advanced users)

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

## Start using the built-in Electron GUI
2. Start Electron GUI:
   ```
   cd gui/
   npm start
   ```

## Start using browser
1. Start Flask backend:
   ```
   python gui/app.py
   ```

1. Open your favorite browser and navigate to:
   ```
   http://127.0.0.1:5000
   ```

# Project Architecture

A quick overview of the codebase structure for developers:

* **Core Engine:** Root directory Python files (e.g., `torrent_client.py`). Handles the BitTorrent protocol logic.
* **Backend API:** `gui/app.py` (Flask server linking the Python core to the frontend).
* **Frontend Assets:** `gui/static/` and `gui/templates/index.html` (Web dashboard).
* **Electron Wrapper:** `gui/electron/main.js` (Desktop app packaging).

Enjoy torrenting responsibly! 🚀

