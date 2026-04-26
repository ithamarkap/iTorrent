import os
import time
import hashlib
import urllib.parse
import bencodepy
import socket
from pathlib import Path

# Local imports moved inside methods to avoid circular dependencies


class TorrentClass:
    # Represents a parsed torrent with all necessary metadata.
    
    def __init__(self, name: str, info_hash: bytes, size: int, announce: str, trackers: list = None, piece_length: int = 0, pieces: bytes = b'', files: list = None):
        self.name = name
        self.info_hash = info_hash
        self.size = size
        self.announce = announce  # Primary tracker URL
        self.trackers = trackers if trackers else [announce] if announce else []
        self.piece_length = piece_length
        self.pieces = pieces
        self.files = files if files is not None else []
        # Pieces is a concatenation of 20-byte SHA1 hashes
        self.piece_amount = len(pieces) // 20 if pieces else 0


def parse_torrent_file(file_path: str) -> TorrentClass:
    """
    Parse a .torrent file and extract metadata.
    """
    with open(file_path, 'rb') as f:
        data = bencodepy.decode(f.read())
    
    info = data[b'info']
    info_hash = hashlib.sha1(bencodepy.encode(info)).digest()
    name = info[b'name'].decode('utf-8')
    
    if b'length' in info:
        size = info[b'length']
        files = [{'name': name, 'length': size}]
    else:
        size = sum(f[b'length'] for f in info[b'files'])
        files = []
        for f in info[b'files']:
            path = [p.decode('utf-8') for p in f[b'path']]
            files.append({'path': path, 'length': f[b'length']})
    
    announce = data.get(b'announce', b'').decode('utf-8')
    
    trackers = []
    if b'announce-list' in data:
        for tier in data[b'announce-list']:
            for tracker in tier:
                trackers.append(tracker.decode('utf-8'))
    
    if announce and announce not in trackers:
        trackers.insert(0, announce)
        
    piece_length = info.get(b'piece length', 0)
    pieces = info.get(b'pieces', b'')
    
    return TorrentClass(name, info_hash, size, announce, trackers, piece_length, pieces, files)


def parse_magnet_link(magnet_url: str) -> TorrentClass:
    """
    Parse a magnet link and extract metadata.
    """
    if not magnet_url.startswith('magnet:?'):
        raise ValueError("Invalid magnet link")
    
    parsed = urllib.parse.urlparse(magnet_url)
    params = urllib.parse.parse_qs(parsed.query)
    
    if 'xt' not in params:
        raise ValueError("Magnet link missing xt parameter")
    
    xt = params['xt'][0]
    if not xt.startswith('urn:btih:'):
        raise ValueError("Invalid xt parameter")
    
    hash_str = xt[9:]
    
    if len(hash_str) == 40:
        info_hash = bytes.fromhex(hash_str)
    elif len(hash_str) == 32:
        import base64
        info_hash = base64.b32decode(hash_str.upper())
    else:
        raise ValueError("Invalid info hash length")
    
    name = params.get('dn', ['Unknown'])[0]
    trackers = params.get('tr', [])
    announce = trackers[0] if trackers else ''
    size = 0
    
    return TorrentClass(name, info_hash, size, announce, trackers)


class TorrentClient:
    """
    Main controller class for a Torrent, integrating parsing, tracking, peers, and pieces.
    """
    
    def __init__(self, torrent_path=None, magnet_url=None):
        self.torrent_path = torrent_path
        self.magnet_url = magnet_url
        self.peer_id = os.urandom(20)  # Randomized 20-byte string by client
        self.Error = False

        try:
            import bencodepy
        except ImportError:
            print("ERROR: 'bencodepy' is not installed. Metadata fetching will fail. Run 'pip install bencodepy'")
            
        try:
            if self.torrent_path:
                self.parsed_torrent = parse_torrent_file(self.torrent_path)
            elif self.magnet_url:
                self.parsed_torrent = parse_magnet_link(self.magnet_url)
            else:
                raise ValueError("Must provide either torrent_path or magnet_url")
            
            self.name = self.parsed_torrent.name
            self.info_hash = self.parsed_torrent.info_hash
            self.total_size = self.parsed_torrent.size
            self.announce_urls = self.parsed_torrent.trackers
            
            self.pieces_hash = getattr(self.parsed_torrent, 'pieces', b'')
            self.piece_size = getattr(self.parsed_torrent, 'piece_length', 0)
            self.piece_amount = getattr(self.parsed_torrent, 'piece_amount', 0)
            
            # Additional metadata that would normally be extracted
            self.creation_date = None
            self.creator = None
            self.comment = None
            
        except Exception as e:
            print(f"Failed to setup torrent: {e}")
            self.Error = True
            return

        self.relative_directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")
        self.files = getattr(self.parsed_torrent, 'files', []) if hasattr(self, 'parsed_torrent') else []
        self.download_files = None
        
        # We will use our new PieceManager wrapper that encompasses the Pieces object
        self.pieces = None
        self.piece_manager = None
        
        self.peer_list = list()
        self.connected_peers = list()
        self.communication_peers_per_loop = list()
        self.MAX_PEER_CONNECTIONS = 75

        self.statuses = list()
        self.is_downloading = False
        
        self.download_speed = 0
        self.upload_speed = 0
        self.status = 'Paused'
        
        self.user_info = {
            'Name': self.name,
            'Path': getattr(self, 'torrent_path', None) or getattr(self, 'magnet_url', None),
            'Created On': getattr(self, 'creation_date', None),
            "Created By": getattr(self, 'creator', None),
            'Comment': getattr(self, 'comment', None),
            'Total_Size': f"{self.total_size} bytes",
            'Hash': getattr(self, 'info_hash', b'').hex(),
            'Pieces': f"{self.piece_amount} x {self.piece_size} bytes"
        }
        
        self.metadata_buffer = {}
        
        if self.piece_amount > 0 and not self.setup_pieces():
            self.Error = True
            
        self.setup_download_files()
        self.verify_existing_files()
        
    def verify_and_install_metadata(self, metadata_size):
        if not hasattr(self, 'metadata_buffer'):
            return
            
        metadata_bytes = b"".join([self.metadata_buffer[i] for i in sorted(self.metadata_buffer.keys())])
        
        import hashlib
        if hashlib.sha1(metadata_bytes).digest() != self.info_hash:
            print("Metadata hash mismatch! Verification failed.")
            return
            
        print("Metadata downloaded and verified successfully!")
        
        import bencodepy
        try:
            info = bencodepy.decode(metadata_bytes)
            
            name = info.get(b'name', b'').decode('utf-8')
            if name and self.name == 'Unknown':
                self.name = name
                
            if b'length' in info:
                self.total_size = info[b'length']
                self.files = [{'name': name, 'length': self.total_size}]
            else:
                self.total_size = sum(f[b'length'] for f in info.get(b'files', []))
                self.files = []
                for f in info.get(b'files', []):
                    path = [p.decode('utf-8') for p in f.get(b'path', [])]
                    self.files.append({'path': path, 'length': f.get(b'length', 0)})
                    
            self.piece_size = info.get(b'piece length', 0)
            self.pieces_hash = info.get(b'pieces', b'')
            self.piece_amount = len(self.pieces_hash) // 20
            
            # Setup torrent files and pieces natively
            if self.piece_amount > 0 and not self.setup_pieces():
                self.Error = True
            
            self.setup_download_files()
            self.verify_existing_files()
            
            # Reconstruct the missing file array in user info
            self.user_info['Pieces'] = f"{self.piece_amount} x {self.piece_size} bytes"
            self.user_info['Total_Size'] = f"{self.total_size} bytes"
            self.user_info['Name'] = self.name
            
            self.status = "Downloading"
            
            # Distribute updated piece logic into existing connected peers natively
            for peer in self.connected_peers:
                peer.piece_size = self.piece_size
                peer.piece_amount = self.piece_amount
                peer.total_size = self.total_size
                peer.torrent_pieces = self.pieces
                peer.torrent_download_files = self.download_files
                
        except Exception as e:
            print(f"Failed to install metadata natively: {e}")
            
    def verify_existing_files(self):
        self.status = 'Verifying'
        print("Checking existing files data from disk...")
        if not getattr(self, 'pieces', None) or not getattr(self, 'download_files', None):
            return
            
        verified_count = 0
        for i in range(self.piece_amount):
            try:
                if self.download_files.check_piece_hash(i):
                    verified_count += 1
                    for block_idx in range(len(self.pieces.received[i])):
                        if not self.pieces.received[i][block_idx]:
                            self.pieces.received[i][block_idx] = True
                            self.pieces.requested[i][block_idx] = True
                            if hasattr(self.pieces, 'unrequested_blocks_count'):
                                self.pieces.unrequested_blocks_count -= 1
            except Exception:
                pass
                
        print(f"Verified {verified_count}/{self.piece_amount} existing pieces natively.")
        # Don't set Seeding here, let the _download_loop handle the transition
        # so it can properly announce 'completed' to trackers and peers.

    def get_gui_data(self):
        progress = self.pieces.get_progress() if self.pieces else 0
        return {
            'id': id(self),
            'name': self.name,
            'status': self.status,
            'progress': round(progress, 2),
            'downloadSpeed': f"{self.download_speed / 1048576:.2f} MB/s",
            'uploadSpeed': f"{self.upload_speed / 1048576:.2f} MB/s"
        }

    def start(self):
        if self.Error: return
        self.is_downloading = True
        self.status = 'Downloading'
        import threading
        self.setup_listener()
        self.thread = threading.Thread(target=self._download_loop, daemon=True)
        self.thread.start()

    def setup_listener(self):
        import socket
        import threading
        self.listen_port = 6881
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for port in range(6881, 6890):
            try:
                self.server_sock.bind(('0.0.0.0', port))
                self.listen_port = port
                break
            except Exception:
                continue
                
        self.server_sock.listen(5)
        self.server_sock.settimeout(1)
        
        def accept_clients():
            from peer_manager import Peer
            while getattr(self, 'is_downloading', False):
                try:
                    client, addr = self.server_sock.accept()
                    # Use live references so we always have current piece state
                    peer_obj = Peer(addr, self.info_hash, self.peer_id, 
                                    pieces=self.pieces,
                                    piece_size=self.piece_size, 
                                    piece_amount=self.piece_amount, 
                                    total_size=self.total_size, 
                                    download_files=self.download_files,
                                    client=self)  # Pass client so peer gets live piece state
                    
                    if peer_obj.accept_connection(client):
                        # Always update to latest pieces state before adding
                        peer_obj.torrent_pieces = self.pieces
                        peer_obj.torrent_download_files = self.download_files
                        # Verify info_hash matches what we are hosting
                        if peer_obj.info_hash != self.info_hash:
                            print(f"Info hash mismatch from {addr}")
                            continue
                        self.connected_peers.append(peer_obj)
                        print(f"Accepted incoming connection from {addr}")
                except socket.timeout:
                    pass
                except Exception as e:
                    break
                    
        self.listener_thread = threading.Thread(target=accept_clients, daemon=True)
        self.listener_thread.start()

    def _download_loop(self):
        import time
        from peer_handling import pack_message, MessageID
        last_time = time.time()
        last_downloaded = 0
        has_announced_completed = False
        last_choke_time = 0
        
        while self.is_downloading:
            if self.pieces and self.pieces.is_done() and self.status != 'Seeding':
                print(f"Torrent {self.name} is complete! Transitioning to Seeding.")
                self.status = 'Seeding'
                self.download_speed = 0
                if not has_announced_completed:
                    self.get_peer_list(event=1)  # announce completed to tracker
                    has_announced_completed = True
                
                # Broadest BITFIELD only once to all connected peers
                from peer_handling import pack_bitfield
                bitfield = bytearray((self.piece_amount + 7) // 8)
                for i in range(self.piece_amount):
                    bitfield[i // 8] |= (1 << (7 - (i % 8)))
                bitfield_msg = pack_bitfield(bytes(bitfield))
                
                for p in self.connected_peers:
                    try:
                        if p.sock:
                            p.sock.send(bitfield_msg)
                    except Exception:
                        pass
                
            if not self.pieces:
                self.status = 'Fetching Metadata'
            elif last_downloaded == 0 and self.pieces:
                # Pieces just became available - reset timing so first speed sample is accurate
                last_downloaded = sum(sum(1 for b in p if b) for p in self.pieces.received) * 16384
                last_time = time.time()
                
            if getattr(self, 'peer_list_timer', 0) <= time.time():
                if not self.peer_list or len(self.connected_peers) < self.MAX_PEER_CONNECTIONS // 2:
                    self.get_peer_list()
                    self.peer_list_timer = time.time() + 30 # Ping tracker at most every 30s
                
            if len(self.connected_peers) < self.MAX_PEER_CONNECTIONS:
                self.connect_peers()
                
            self.communicate_peers()
            
            # Periodic choke management for seeding
            current_time = time.time()
            if current_time - last_choke_time >= 10:
                last_choke_time = current_time
                if self.status == 'Seeding':
                    for p in self.connected_peers:
                        if p.peer_interested and p.am_choking and p.sock:
                            try:
                                p.sock.send(pack_message(MessageID.UNCHOKE))
                                p.am_choking = False
                                print(f"Seeding: Unchoked interested peer {p.peer}")
                            except Exception:
                                pass
            
            elapsed = current_time - last_time
            if elapsed >= 1.0:
                if self.pieces:
                    if self.status == 'Seeding':
                        current_downloaded = self.total_size
                    else:
                        current_downloaded = sum(sum(1 for b in p if b) for p in self.pieces.received) * 16384
                    
                    delta = current_downloaded - last_downloaded
                    self.download_speed = max(0, delta / elapsed)
                    last_downloaded = current_downloaded
                    
                    current_uploaded_tick = 0
                    for p in self.connected_peers:
                        if hasattr(p, 'uploaded'):
                            current_uploaded_tick += p.uploaded
                            p.uploaded = 0
                            
                    self.upload_speed = current_uploaded_tick / elapsed
                    self.total_uploaded = getattr(self, 'total_uploaded', 0) + current_uploaded_tick
                    
                    if not hasattr(self, 'verified_pieces'):
                        self.verified_pieces = set()
                        
                    for i in range(self.pieces.piece_amount):
                        if i not in self.verified_pieces:
                            if all(self.pieces.received[i]):
                                if self.download_files.check_piece_hash(i):
                                    if i % 10 == 0:
                                        print(f"Piece {i} verified!")
                                    self.verified_pieces.add(i)
                                    from peer_handling import pack_have
                                    have_msg = pack_have(i)
                                    for p in self.connected_peers:
                                        try:
                                            if p.sock:
                                                p.sock.send(have_msg)
                                        except Exception:
                                            pass
                                else:
                                    print(f"Piece {i} failed hash check! Resetting.")
                                    for block_idx in range(len(self.pieces.received[i])):
                                        self.pieces.received[i][block_idx] = False
                                        self.pieces.requested[i][block_idx] = False
                else:
                    self.download_speed = 0
                    self.upload_speed = 0
                    
                last_time = current_time
                
            time.sleep(0.01)

    def setup_pieces(self):
        from pieces_manager import Pieces, PieceManager
        try:
            self.pieces = Pieces(self.piece_size, self.piece_amount, self.total_size)
            self.piece_manager = PieceManager(self.pieces)
            return True
        except Exception as e:
            print("Failed to setup pieces.", e)
            return False

    def get_peer_list(self, event=0):
        from get_peer_list import TrackerClass
        """Iterates over the announce urls to obtain a list of peers."""
        try:
            safe_name = str(self.name).encode('ascii', 'replace').decode()
            print(f"Contacting trackers for {safe_name}...")
        except Exception:
            print("Contacting tracker...")
        breaking = False
        
        current_downloaded = sum(sum(1 for b in p if b) for p in self.pieces.received) * 16384 if self.pieces else 0
        current_uploaded = getattr(self, 'total_uploaded', 0)
        
        for url in self.announce_urls:
            if not url.startswith('udp:'):
                print(f"Skipping unsupported tracker format: {url}")
                continue
                
            try:
                parsed = urllib.parse.urlparse(url)
                ip = socket.gethostbyname(parsed.hostname)
                port = parsed.port or 1337
                tracker_addr = (ip, port)
                
                tracker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                tracker_sock.settimeout(3)
                
                listen_port = getattr(self, 'listen_port', 6881)
                tracker = TrackerClass(tracker_addr, tracker_sock, self.parsed_torrent, self.peer_id, listen_port=listen_port)
                peer_list = tracker.start_communicating(event=event, downloaded=current_downloaded, uploaded=current_uploaded)
                
                if peer_list:
                    self.peer_list = [(p['ip'], p['port']) for p in peer_list]
                    breaking = True
                    break
            except Exception as e:
                print(f"Problem with tracker {url}: {e}")
                
            if breaking:
                break

        if not self.peer_list:
            print("The torrent is not available due to out dated trackers or lack of peers.")
            return True
        else:
            print(f"Obtained {len(self.peer_list)} peers.")
            return False

    def connect_peers(self):
        from peer_manager import Peer
        import concurrent.futures
        
        peers_to_connect = [peer for peer in self.peer_list if peer not in [p.peer for p in self.connected_peers]]
        if not peers_to_connect:
            return True
            
        def try_connect(peer_addr):
            if len(self.connected_peers) >= self.MAX_PEER_CONNECTIONS:
                return
            try:
                peer_obj = Peer(peer_addr, self.info_hash, self.peer_id, 
                                pieces=getattr(self, 'pieces', None), 
                                piece_size=getattr(self, 'piece_size', 0), 
                                piece_amount=getattr(self, 'piece_amount', 0), 
                                total_size=getattr(self, 'total_size', 0), 
                                download_files=getattr(self, 'download_files', None),
                                client=self)

                if peer_obj.connect():
                    if len(self.connected_peers) < self.MAX_PEER_CONNECTIONS:
                        self.connected_peers.append(peer_obj)
                        if self.piece_manager:
                            self.piece_manager.add_seeder(peer_obj.peer)
                else:
                    if peer_obj.peer in self.peer_list:
                        self.peer_list.remove(peer_obj.peer)
            except Exception as e:
                print(f"Failed to initialize/connect peer {peer_addr}: {e}")

        threads = []
        import threading
        for peer_addr in peers_to_connect[:min(30, len(peers_to_connect))]:
            if len(self.connected_peers) >= self.MAX_PEER_CONNECTIONS:
                break
            t = threading.Thread(target=try_connect, args=(peer_addr,))
            t.daemon = True
            t.start()
            threads.append(t)
            
        for t in threads:
            t.join()

    def communicate_peers(self):
        import concurrent.futures
        self.communication_peers_per_loop = self.connected_peers.copy()

        if not self.communication_peers_per_loop:
            return "Connect"
            
        def _comm(peer):
            try:
                if not peer.communicate():
                    return peer
            except Exception:
                return peer
            return None
                
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(128, max(1, len(self.communication_peers_per_loop)))) as executor:
            to_remove = list(executor.map(_comm, self.communication_peers_per_loop))
            
        for bad_peer in filter(None, to_remove):
            if bad_peer in self.connected_peers:
                self.connected_peers.remove(bad_peer)
                if bad_peer.peer in self.peer_list:
                    self.peer_list.remove(bad_peer.peer)
                print("Problem communicating peer: ", bad_peer.peer)
                return "Connect"

        return False

    def keep_alive_peers(self):
        if not self.pieces:
            return
            
        while self.pieces and not self.pieces.is_done():
            time.sleep(1)
            for peer in self.connected_peers.copy():
                try:
                    if not peer.keep_alive():
                        print("Problem keeping alive peer: ", peer.peer)
                        if peer in self.connected_peers:
                            self.connected_peers.remove(peer)
                        if peer.peer in self.peer_list:
                            self.peer_list.remove(peer.peer)
                except Exception as e:
                    print(f"Error keeping alive {peer.peer}: {e}")

    def setup_download_files(self):
        try:
            if self.torrent_path:
                directory_name = Path(self.torrent_path).stem
            else:
                directory_name = self.name or "Unknown_Torrent"
                
            path = os.path.join(self.relative_directory, directory_name)
            
            from files_manager import Files
            self.download_files = Files(self.piece_size, self.pieces_hash, self.files, path)
            self.download_files.create_directory()
            self.download_files.create_files()
            
            print("Files setup complete.")
            return False

        except Exception as e:
            print("Problem setting up download files ", e)
            return True


def main():
    torrent_path = "ubuntu-24.04.4-desktop-amd64.iso.torrent"
    print(f"Initializing Torrent Client for {torrent_path}")
    
    my_torrent = TorrentClient(torrent_path=torrent_path)

    if my_torrent.Error:
        print("Initialization failed.")
        return

    print("Torrent info:")
    for k, v in my_torrent.user_info.items():
        print(f"  {k}: {v}")
        

if __name__ == "__main__":
    main()
