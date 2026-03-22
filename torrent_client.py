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
    
    def __init__(self, name: str, info_hash: bytes, size: int, announce: str, trackers: list = None, piece_length: int = 0, pieces: bytes = b''):
        self.name = name
        self.info_hash = info_hash
        self.size = size
        self.announce = announce  # Primary tracker URL
        self.trackers = trackers if trackers else [announce] if announce else []
        self.piece_length = piece_length
        self.pieces = pieces
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
    else:
        size = sum(f[b'length'] for f in info[b'files'])
    
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
    
    return TorrentClass(name, info_hash, size, announce, trackers, piece_length, pieces)


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

        self.relative_directory = str(Path.home() / "Downloads")
        self.files = None
        self.download_files = None
        
        # We will use our new PieceManager wrapper that encompasses the Pieces object
        self.pieces = None
        self.piece_manager = None
        
        if self.piece_amount > 0 and not self.setup_pieces():
            self.Error = True
        
        self.peer_list = list()
        self.connected_peers = list()
        self.communication_peers_per_loop = list()
        self.MAX_PEER_CONNECTIONS = 4

        self.statuses = list()
        self.is_downloading = False
        
        self.user_info = {
            'Name': self.name,
            'Path': self.torrent_path or self.magnet_url,
            'Created On': self.creation_date,
            "Created By": self.creator,
            'Comment': self.comment,
            'Total_Size': f"{self.total_size} bytes",
            'Hash': self.info_hash.hex(),
            'Pieces': f"{self.piece_amount} x {self.piece_size} bytes"
        }

    def setup_pieces(self):
        from pieces_manager import Pieces, PieceManager
        try:
            self.pieces = Pieces(self.piece_size, self.piece_amount, self.total_size)
            self.piece_manager = PieceManager(self.pieces)
            return True
        except Exception as e:
            print("Failed to setup pieces.", e)
            return False

    def get_peer_list(self):
        from get_peer_list import TrackerClass
        """Iterates over the announce urls to obtain a list of peers."""
        print(f"Contacting trackers for {self.name}...")
        breaking = False
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
                
                tracker = TrackerClass(tracker_addr, tracker_sock, self.parsed_torrent, self.peer_id)
                peer_list = tracker.start_communicating()
                
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
        print("Connecting to peers...")

        if not self.peer_list:
            return True
            
        for peer_addr in self.peer_list[:]:
            try:
                peer_obj = Peer(peer_addr, self.info_hash, self.peer_id, 
                                pieces=self.pieces, 
                                piece_size=self.piece_size, 
                                piece_amount=self.piece_amount, 
                                total_size=self.total_size, 
                                download_files=self.download_files)

                if len(self.connected_peers) < self.MAX_PEER_CONNECTIONS:
                    if peer_obj.connect():
                        self.connected_peers.append(peer_obj)
                        if self.piece_manager:
                            self.piece_manager.add_seeder(peer_obj.peer)
                    else:
                        self.peer_list.remove(peer_obj.peer)
                else:
                    print(f"Connected {len(self.connected_peers)}")
                    break
            except Exception as e:
                print(f"Failed to initialize/connect peer {peer_addr}: {e}")

        print("Done connecting!", [peer.peer for peer in self.connected_peers])

    def communicate_peers(self):
        self.communication_peers_per_loop = self.connected_peers.copy()

        if not self.communication_peers_per_loop:
            return "Connect"
            
        for peer in self.communication_peers_per_loop:
            try:
                if not peer.communicate():
                    self.connected_peers.remove(peer)
                    if peer.peer in self.peer_list:
                        self.peer_list.remove(peer.peer)
                    print("Problem communicating peer: ", peer.peer)
                    return "Connect"
            except Exception as e:
                print(f"Error communicating with {peer.peer}: {e}")
                self.connected_peers.remove(peer)

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
            
            # self.download_files = Files(self.piece_size, self.pieces_hash, self.files, path)
            print("Files setup placeholder passed. Ensure files_manager.py is included later.")
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
