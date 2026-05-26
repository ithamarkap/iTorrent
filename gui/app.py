from flask import Flask, render_template, jsonify, request, Response
import os
import sys
import atexit
import signal
import socket
import random
import logging
import time
import io
import threading
import time
from collections import deque

# Force UTF-8 encoding for stdout/stderr to prevent encoding issues on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ── Logging setup (must happen before any other import) ───────────────────────

log_buffer = deque(maxlen=500)

class _LogCaptureHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))

class _StreamRedirector:
    def __init__(self, stream):
        self._stream = stream
    def write(self, data):
        if data.strip():
            log_buffer.append(data.strip())
        self._stream.write(data)
    def flush(self):
        self._stream.flush()

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    encoding='utf-8')
logger = logging.getLogger('app')
_handler = _LogCaptureHandler()
_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(_handler)

sys.stdout = _StreamRedirector(sys.stdout)
sys.stderr = _StreamRedirector(sys.stderr)

# ── Flask app ─────────────────────────────────────────────────────────────────

if getattr(sys, 'frozen', False):
    app = Flask(__name__,
                template_folder=os.path.join(sys._MEIPASS, 'templates'),
                static_folder=os.path.join(sys._MEIPASS, 'static'))
else:
    app = Flask(__name__)
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# ── Backend module path ───────────────────────────────────────────────────────

_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from torrent_client import TorrentClient, parse_torrent_file, parse_magnet_link, get_default_download_path
from tracker_client import TrackerClass
import pin_manager

# ── UPnP ─────────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from upnp_manager import upnp_manager
from network_engine import network_engine

# Start the shared NetworkEngine and obtain the actual bound port.
_LISTEN_PORT = network_engine.start(6881)
if not _LISTEN_PORT:
    # Fallback if binding fails completely
    _LISTEN_PORT = 6881

upnp_manager.setup(_LISTEN_PORT)
atexit.register(upnp_manager.teardown)

def _signal_handler(sig, frame):
    logger.info(f'[UPnP] Caught signal {sig} -- shutting down.')
    upnp_manager.teardown()
    sys.exit(0)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

active_torrents = []

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/torrents', methods=['GET'])
def get_torrents():
    return jsonify([t.get_gui_data() for t in active_torrents])


@app.route('/api/torrents/add', methods=['POST'])
def add_torrent():
    path = request.json.get('path', '')
    if not path:
        return jsonify({'success': False, 'error': 'No path provided'}), 400

    client = TorrentClient(magnet_url=path) if path.startswith('magnet:') else TorrentClient(torrent_path=path)
    if client.Error:
        return jsonify({'success': False, 'error': 'Failed to initialize torrent'}), 400

    client.start()
    
    if pin_manager.is_torrent_locked(client.info_hash.hex()):
        client.is_locked = True
        
    active_torrents.append(client)
    return jsonify({'success': True, 'torrent': client.get_gui_data()})


# torrent_id is an id of a TorrentClient instance, generated from id() function
@app.route('/api/torrents/<int:torrent_id>/action', methods=['POST'])
def torrent_action(torrent_id):
    data = request.json
    action = data.get('action')

    for client in active_torrents:
        if id(client) == torrent_id:
            if action in ['pause', 'remove'] and getattr(client, 'is_locked', False):
                return jsonify({'success': False, 'error': 'Torrent is locked'}), 403
                
            if action == 'start':
                client.is_downloading = True
                if not (hasattr(client, 'thread') and client.thread and client.thread.is_alive()):
                    client.start()
                else:
                    client.status = 'Downloading'
            elif action == 'pause':
                client.is_downloading = False
                client.status = 'Paused'
            elif action == 'remove':
                client.is_downloading = False
                if data.get('delete_files'):
                    client.delete_files()
                active_torrents.remove(client)
                return jsonify({'success': True, 'removed': True})
            return jsonify({'success': True, 'torrent': client.get_gui_data()})

    return jsonify({'success': False, 'error': 'Torrent not found'}), 404


@app.route('/api/torrents/<int:torrent_id>/lock', methods=['POST'])
def lock_torrent(torrent_id):
    # Accesses the JSON data sent in the body of an HTTP request
    data = request.json
    pin = data.get('pin')
    if not pin:
        return jsonify({'success': False, 'error': 'PIN required'}), 400

    for client in active_torrents:
        if id(client) == torrent_id:
            pin_manager.lock_torrent(client.info_hash.hex(), pin)
            client.is_locked = True
            return jsonify({'success': True, 'torrent': client.get_gui_data()})
            
    return jsonify({'success': False, 'error': 'Torrent not found'}), 404


@app.route('/api/torrents/<int:torrent_id>/unlock', methods=['POST'])
def unlock_torrent(torrent_id):
    data = request.json
    pin = data.get('pin')
    if not pin:
        return jsonify({'success': False, 'error': 'PIN required'}), 400

    for client in active_torrents:
        if id(client) == torrent_id:
            if pin_manager.unlock_torrent(client.info_hash.hex(), pin):
                client.is_locked = False
                return jsonify({'success': True, 'torrent': client.get_gui_data()})
            else:
                return jsonify({'success': False, 'error': 'Invalid PIN'}), 403
            
    return jsonify({'success': False, 'error': 'Torrent not found'}), 404


@app.route('/api/get-peers', methods=['POST'])
def get_peers():
    data = request.json
    torrent_type = data.get('type')

    try:
        if torrent_type == 'file':
            file_path = data.get('path', '')
            if not os.path.exists(file_path):
                return jsonify({'success': False, 'error': 'Invalid file path'}), 400
            torrent = parse_torrent_file(file_path)
        elif torrent_type == 'magnet':
            magnet_url = data.get('url', '')
            if not magnet_url:
                return jsonify({'success': False, 'error': 'Magnet URL required'}), 400
            torrent = parse_magnet_link(magnet_url)
        else:
            return jsonify({'success': False, 'error': 'Invalid type'}), 400

        if not torrent.trackers:
            return jsonify({'success': False, 'error': 'No trackers found'}), 400

        peer_list = None
        for tracker_url in torrent.trackers:
            if not tracker_url.startswith('udp://'):
                continue
            clean = tracker_url[6:].split('/')[0]
            if ':' not in clean:
                continue
            host, port_str = clean.rsplit(':', 1)
            try:
                peer_id = os.urandom(20)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                tracker = TrackerClass((host, int(port_str)), sock, torrent, peer_id)
                peer_list = tracker.start_communicating()
                sock.close()
                if peer_list:
                    break
            except Exception as e:
                logging.getLogger('get-peers').warning(f"{tracker_url}: {e}")

        if not peer_list:
            return jsonify({'success': False, 'error': 'No peers obtained from any tracker'}), 500

        return jsonify({'success': True, 'torrent_name': torrent.name,
                        'peer_count': len(peer_list), 'peers': peer_list})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'defaultDownloadDir': get_default_download_path()
    })


@app.route('/api/settings', methods=['POST'])
def update_settings():
    return jsonify({'success': True})


@app.route('/api/upnp', methods=['GET', 'POST'])
def upnp_endpoint():
    if request.method == 'GET':
        return jsonify(upnp_manager.get_status())

    # POST -- toggle enable/disable
    data = request.json or {}
    enabled = data.get('enabled', True)
    upnp_manager.set_enabled(bool(enabled), _LISTEN_PORT)
    return jsonify({'success': True, **upnp_manager.get_status()})


@app.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify({'logs': list(log_buffer)})


@app.route('/api/logs/stream')
def stream_logs():
    def event_stream():
        last_len = len(log_buffer)
        while True:
            current_len = len(log_buffer)
            if current_len > last_len:
                for i in range(last_len, current_len):
                    yield f"data: {log_buffer[i]}\n\n"
                last_len = current_len
            time.sleep(0.1)
    return Response(event_stream(), mimetype='text/event-stream')


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    logger.info('Shutdown requested via API -- cleaning up.')
    network_engine.stop()
    upnp_manager.teardown()
    # Exit in a separate thread so we can return the response first
    threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0))).start()
    return jsonify({'success': True})


if __name__ == '__main__':
    print("Starting Flask server on port 5000")
    app.run(host='127.0.0.1', port=5000, threaded=True)
