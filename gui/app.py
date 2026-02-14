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

@app.route('/api/get-peers', methods=['POST'])
def get_peers():
    """Get peer list from torrent file or magnet link."""
    import sys
    import os
    import socket
    import logging
    import random
    
    # Add parent directory to path to import torrent modules
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, parent_dir)
    
    from torrent_parser import parse_torrent_file, parse_magnet_link
    from get_peer_list import TrackerClass
    
    # Setup logger
    logger = logging.getLogger('peer_list')
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
    
    try:
        data = request.json
        torrent_type = data.get('type')  # 'file' or 'magnet'
        
        if torrent_type == 'file':
            file_path = data.get('path')
            if not file_path or not os.path.exists(file_path):
                return jsonify({'success': False, 'error': 'Invalid file path'}), 400
            
            torrent = parse_torrent_file(file_path)
        elif torrent_type == 'magnet':
            magnet_url = data.get('url')
            if not magnet_url:
                return jsonify({'success': False, 'error': 'Magnet URL required'}), 400
            
            torrent = parse_magnet_link(magnet_url)
        else:
            return jsonify({'success': False, 'error': 'Invalid type'}), 400
        
        # Get all trackers
        trackers = torrent.trackers
        if not trackers:
            return jsonify({'success': False, 'error': 'No trackers found'}), 400
            
        peer_list = None
        errors = []
        
        # Try each tracker until one works
        for tracker_url in trackers:
            if not tracker_url.startswith('udp://'):
                logger.debug(f"Skipping non-UDP tracker: {tracker_url}")
                continue
                
            try:
                # Parse tracker URL (udp://tracker:port)
                clean_url = tracker_url.replace('udp://', '')
                if '/' in clean_url:
                    clean_url = clean_url.split('/')[0]
                
                if ':' not in clean_url:
                    logger.warning(f"Invalid tracker URL format: {tracker_url}")
                    continue
                    
                tracker_host, tracker_port = clean_url.rsplit(':', 1)
                tracker_ip = (tracker_host, int(tracker_port))
                
                logger.info(f"Trying tracker: {tracker_url}")
                
                # Generate random peer ID
                peer_id = bytes([random.randint(0, 255) for _ in range(20)])
                
                # Create socket and tracker instance
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                tracker = TrackerClass(tracker_ip, sock, torrent, peer_id, logger)
                
                # Get peer list
                peer_list = tracker.start_communicating()
                sock.close()
                
                if peer_list:
                    logger.info(f"Successfully retrieved {len(peer_list)} peers from {tracker_url}")
                    break
                else:
                    errors.append(f"{tracker_url}: Failed to retrieve peers")
            except Exception as e:
                logger.error(f"Error communicating with {tracker_url}: {str(e)}")
                errors.append(f"{tracker_url}: {str(e)}")
        
        if peer_list is None:
            error_msg = "Failed to retrieve peers from any tracker. " + "; ".join(errors[:3])
            return jsonify({'success': False, 'error': error_msg}), 500
        
        return jsonify({
            'success': True,
            'torrent_name': torrent.name,
            'peer_count': len(peer_list),
            'peers': peer_list
        })
        
    except Exception as e:
        logger.error(f'Error getting peers: {str(e)}')
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/settings', methods=['POST'])
def update_settings():
    # TODO: Save settings
    return jsonify({'success': True})

if __name__ == '__main__':
    port = 5000
    print(f"Starting Flask server on port {port}")
    app.run(host='127.0.0.1', port=port)
