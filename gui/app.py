from flask import Flask, render_template, jsonify, request
import os
import sys

# Determine the correct template and static folders
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0 # Disable caching for dev

# Mock data
torrents = [
    {'id': 1, 'name': 'mytotallylegallinuxiso.iso', 'status': 'Downloading', 'progress': 45, 'downloadSpeed': '2.5 MB/s', 'uploadSpeed': '100 KB/s'},
    {'id': 2, 'name': 'totallylegitadobesoftware.zip', 'status': 'Seeding', 'progress': 100, 'downloadSpeed': '0 B/s', 'uploadSpeed': '500 KB/s'},
]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/torrents', methods=['GET'])
def get_torrents():
    return jsonify(torrents)

@app.route('/api/torrents/add', methods=['POST'])
def add_torrent():
    data = request.json
    path = data.get('path')
    if path:
        new_torrent = {
            'id': len(torrents) + 1,
            'name': os.path.basename(path),
            'status': 'Downloading',
            'progress': 0,
            'downloadSpeed': '0 B/s',
            'uploadSpeed': '0 B/s'
        }
        torrents.append(new_torrent)
        return jsonify({'success': True, 'torrent': new_torrent})
    return jsonify({'success': False, 'error': 'No path provided'}), 400

@app.route('/api/torrents/<int:torrent_id>/action', methods=['POST'])
def torrent_action(torrent_id):
    data = request.json
    action = data.get('action')
    
    for t in torrents:
        if t['id'] == torrent_id:
            if action == 'start':
                t['status'] = 'Downloading'
            elif action == 'pause':
                t['status'] = 'Paused'
            elif action == 'remove':
                torrents.remove(t)
                return jsonify({'success': True, 'removed': True})
            return jsonify({'success': True, 'torrent': t})
            
    return jsonify({'success': False, 'error': 'Torrent not found'}), 404

@app.route('/api/settings', methods=['POST'])
def update_settings():
    # TODO: Save settings
    return jsonify({'success': True})

if __name__ == '__main__':
    port = 5000
    print(f"Starting Flask server on port {port}")
    app.run(host='127.0.0.1', port=port)
