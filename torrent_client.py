import os
import time
import hashlib
import threading
import concurrent.futures
import urllib.parse
import bencodepy
import socket
import logging
from pathlib import Path

logger = logging.getLogger('TorrentClient')


class TorrentClass:
    """Parsed torrent metadata."""
    def __init__(self, name, info_hash, size, announce, trackers=None,
                 piece_length=0, pieces=b'', files=None):
        self.name = name
        self.info_hash = info_hash
        self.size = size
        self.announce = announce
        self.trackers = trackers if trackers else ([announce] if announce else [])
        self.piece_length = piece_length
        self.pieces = pieces
        self.files = files if files is not None else []
        self.piece_amount = len(pieces) // 20 if pieces else 0


def parse_torrent_file(file_path: str) -> TorrentClass:
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
        files = [{'path': [p.decode('utf-8') for p in f[b'path']], 'length': f[b'length']}
                 for f in info[b'files']]

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
    if not magnet_url.startswith('magnet:?'):
        raise ValueError("Invalid magnet link")

    params = urllib.parse.parse_qs(urllib.parse.urlparse(magnet_url).query)

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
    return TorrentClass(name, info_hash, 0, trackers[0] if trackers else '', trackers)


class TorrentClient:
    """Main torrent download/seed controller."""

    MAX_PEER_CONNECTIONS = 200

    def __init__(self, torrent_path=None, magnet_url=None):

        self.torrent_path = torrent_path
        self.magnet_url = magnet_url
        self.peer_id = os.urandom(20)
        self.Error = False

        try:
            if torrent_path:
                self.parsed_torrent = parse_torrent_file(torrent_path)
            elif magnet_url:
                self.parsed_torrent = parse_magnet_link(magnet_url)
            else:
                raise ValueError("Must provide torrent_path or magnet_url")

            self.name = self.parsed_torrent.name
            self.info_hash = self.parsed_torrent.info_hash
            self.total_size = self.parsed_torrent.size
            self.announce_urls = self.parsed_torrent.trackers
            self.pieces_hash = getattr(self.parsed_torrent, 'pieces', b'')
            self.piece_size = getattr(self.parsed_torrent, 'piece_length', 0)
            self.piece_amount = getattr(self.parsed_torrent, 'piece_amount', 0)

        except Exception as e:
            logger.error(f"Failed to setup torrent: {e}")
            self.Error = True
            return

        self.relative_directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")
        self.files = getattr(self.parsed_torrent, 'files', [])
        self.download_files = None
        self.pieces = None
        self.piece_manager = None

        self.peer_list = []
        self.connected_peers = []
        self.is_downloading = False

        self.download_speed = 0
        self.upload_speed = 0
        self.session_downloaded = 0
        self.session_uploaded = 0
        self.total_uploaded = 0
        self.start_time = time.time()
        self.status = 'Paused'

        self.metadata_buffer = {}

        # piece_index -> { 'ip': str|None, 'port': int|None }
        # Best-effort updated whenever a PIECE block is received.
        self.piece_sources = {}


        if self.piece_amount > 0 and not self._setup_pieces():
            self.Error = True

        self._setup_download_files()
        self._verify_existing_files()

    # ── Metadata (magnet) ─────────────────────────────────────────────────────

    def verify_and_install_metadata(self, metadata_size):
        if not self.metadata_buffer:
            return

        metadata_bytes = b''.join(self.metadata_buffer[i] for i in sorted(self.metadata_buffer))

        if hashlib.sha1(metadata_bytes).digest() != self.info_hash:
            logger.error("Metadata hash mismatch!")
            return

        logger.info("Metadata verified successfully.")

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
                self.files = [{'path': [p.decode('utf-8') for p in f.get(b'path', [])],
                                'length': f.get(b'length', 0)}
                               for f in info.get(b'files', [])]

            self.piece_size = info.get(b'piece length', 0)
            self.pieces_hash = info.get(b'pieces', b'')
            self.piece_amount = len(self.pieces_hash) // 20

            if self.piece_amount > 0 and not self._setup_pieces():
                self.Error = True

            self._setup_download_files()
            self._verify_existing_files()
            self.status = 'Downloading'

            # Push updated state to already-connected peers
            for peer in self.connected_peers:
                peer.piece_size = self.piece_size
                peer.piece_amount = self.piece_amount
                peer.total_size = self.total_size
                peer.torrent_pieces = self.pieces
                peer.torrent_download_files = self.download_files
                
            if self.pieces and self.pieces.is_done():
                self.status = 'Seeding'
                self.broadcast_bitfield()

        except Exception as e:
            logger.error(f"Failed to install metadata: {e}")

    # ── File / Piece Setup ────────────────────────────────────────────────────

    def _verify_existing_files(self):
        self.status = 'Verifying'
        if not self.pieces or not self.download_files:
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
                            self.pieces._received_count += 1
            except Exception:
                pass

        logger.info(f"Verified {verified_count}/{self.piece_amount} existing pieces.")

    def _setup_pieces(self) -> bool:
        from pieces_manager import Pieces, PieceManager
        try:
            self.pieces = Pieces(self.piece_size, self.piece_amount, self.total_size)
            self.piece_manager = PieceManager(self.pieces)
            return True
        except Exception as e:
            logger.error(f"Failed to setup pieces: {e}")
            return False

    def _setup_download_files(self):
        try:
            directory_name = Path(self.torrent_path).stem if self.torrent_path else (self.name or "Unknown_Torrent")
            path = os.path.join(self.relative_directory, directory_name)

            from files_manager import Files
            self.download_files = Files(self.piece_size, self.pieces_hash, self.files, path)
            self.download_files.create_directory()
            self.download_files.create_files()
        except Exception as e:
            logger.error(f"Problem setting up download files: {e}")

    # ── GUI Data ──────────────────────────────────────────────────────────────

    def record_piece_source(self, piece_index: int, ip: str | None, port: int | None):
        """Best-effort tracking of which remote peer supplied a piece (per-block).

        Called from peer_handling.handle_piece() when a PIECE block is received.
        """
        try:
            if piece_index is None:
                return
            # Only keep latest source for that piece.
            self.piece_sources[int(piece_index)] = {
                'ip': ip,
                'port': port,
            }
        except Exception:
            pass

    def get_gui_data(self) -> dict:
        progress = self.pieces.get_progress() if self.pieces else 0


        def format_speed(b):
            if b >= 1_048_576:
                return f"{b / 1_048_576:.2f} MB/s"
            if b >= 1024:
                return f"{b / 1024:.2f} KB/s"
            return f"{b:.0f} B/s"

        bitfield = []
        if self.pieces:
            bitfield = [1 if all(self.pieces.received[i]) else 0 for i in range(self.piece_amount)]

        elapsed = time.time() - self.start_time
        remaining = self.total_size * (1 - progress / 100)
        eta = int(remaining / self.download_speed) if self.download_speed > 0 else -1

        # GUI-friendly aggregation: per-piece latest source ip:port
        piece_sources = []
        if self.piece_amount > 0:
            for i in range(self.piece_amount):
                src = self.piece_sources.get(i)
                if not src or src.get('ip') is None:
                    piece_sources.append(None)
                else:
                    piece_sources.append({'ip': src.get('ip'), 'port': src.get('port')})

        return {
            'id': id(self),
            'name': self.name,
            'status': self.status,
            'progress': round(progress, 2),
            'downloadSpeed': format_speed(self.download_speed),
            'uploadSpeed': format_speed(self.upload_speed),
            'totalDownloaded': self.session_downloaded,
            'totalUploaded': self.session_uploaded,
            'elapsedTime': int(elapsed),
            'peerCount': len(self.connected_peers),
            'bitfield': bitfield,
            'pieceAmount': self.piece_amount,
            'pieceSources': piece_sources,
            'totalSize': self.total_size,
            'eta': eta,
            'infoHash': self.info_hash.hex(),
        }


    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self.Error:
            return
        self.is_downloading = True
        if self.pieces and self.pieces.is_done():
            self.status = 'Seeding'
        else:
            self.status = 'Downloading'
            
        # Register with the shared NetworkEngine
        from network_engine import network_engine
        network_engine.register(self.info_hash, self)
        self.listen_port = network_engine.port
        
        self.thread = threading.Thread(target=self._download_loop, daemon=True)
        self.thread.start()
        
        if self.status == 'Seeding':
            self.broadcast_bitfield()

    def broadcast_bitfield(self):
        """Send a full Bitfield message to all connected peers to signal completion."""
        from peer_handling import pack_bitfield
        if not self.pieces or self.piece_amount <= 0:
            return
            
        bitfield = bytearray((self.piece_amount + 7) // 8)
        for i in range(self.piece_amount):
            if all(self.pieces.received[i]):
                bitfield[i // 8] |= (1 << (7 - (i % 8)))
        
        bf_msg = pack_bitfield(bytes(bitfield))
        for p in self.connected_peers:
            try:
                if p.sock:
                    p.sock.send(bf_msg)
            except Exception:
                pass

    def stop(self):
        self.is_downloading = False
        from network_engine import network_engine
        network_engine.unregister(self.info_hash)
        self.status = 'Paused'

    def _download_loop(self):
        from peer_handling import pack_message, MessageID, pack_bitfield, pack_have
        last_time = time.time()
        has_announced_completed = False
        last_choke_time = 0
        verified_pieces = set()

        while self.is_downloading:
            now = time.time()

            # ── Seeding transition ────────────────────────────────────────────
            if self.pieces and self.pieces.is_done() and self.status != 'Seeding':
                logger.info(f"{self.name} complete -- switching to Seeding.")
                self.status = 'Seeding'
                self.download_speed = 0
                if not has_announced_completed:
                    self._get_peer_list(event=1)
                    has_announced_completed = True
                self.broadcast_bitfield()

            if not self.pieces:
                self.status = 'Fetching Metadata'

            # ── Tracker / peer management ─────────────────────────────────────
            if getattr(self, 'peer_list_timer', 0) <= now:
                if not self.peer_list or len(self.connected_peers) < self.MAX_PEER_CONNECTIONS // 2:
                    self._get_peer_list()
                self.peer_list_timer = now + 30

            if len(self.connected_peers) < self.MAX_PEER_CONNECTIONS:
                self._connect_peers()

            self._communicate_peers()

            # ── Periodic choke management (seeding) ───────────────────────────
            if now - last_choke_time >= 10:
                last_choke_time = now
                if self.status == 'Seeding':
                    unchoke_msg = pack_message(MessageID.UNCHOKE)
                    for p in self.connected_peers:
                        if p.peer_interested and p.am_choking and p.sock:
                            try:
                                p.sock.send(unchoke_msg)
                                p.am_choking = False
                            except Exception:
                                pass

            # ── 1-second stats tick ───────────────────────────────────────────
            elapsed = now - last_time
            if elapsed >= 1.0:
                if self.pieces:
                    dl_tick = 0
                    ul_tick = 0
                    for p in self.connected_peers:
                        dl_tick += p.downloaded;  p.downloaded = 0
                        ul_tick += p.uploaded;    p.uploaded = 0

                    self.download_speed = dl_tick / elapsed
                    self.upload_speed = ul_tick / elapsed
                    self.session_downloaded += dl_tick
                    self.session_uploaded += ul_tick
                    self.total_uploaded += ul_tick

                    # Piece hash verification
                    have_msg_cache = {}
                    for i in range(self.pieces.piece_amount):
                        if i not in verified_pieces and all(self.pieces.received[i]):
                            if self.download_files.check_piece_hash(i):
                                verified_pieces.add(i)
                                if i not in have_msg_cache:
                                    have_msg_cache[i] = pack_have(i)
                                for p in self.connected_peers:
                                    try:
                                        if p.sock:
                                            p.sock.send(have_msg_cache[i])
                                    except Exception:
                                        pass
                            else:
                                logger.warning(f"Piece {i} failed hash check -- resetting.")
                                for block_idx in range(len(self.pieces.received[i])):
                                    if self.pieces.received[i][block_idx]:
                                        self.pieces.received[i][block_idx] = False
                                        self.pieces._received_count -= 1
                                    self.pieces.requested[i][block_idx] = False
                else:
                    self.download_speed = 0
                    self.upload_speed = 0

                last_time = now

            time.sleep(0.01)

    # ── Tracker ───────────────────────────────────────────────────────────────

    def _get_peer_list(self, event=0):
        from get_peer_list import TrackerClass
        logger.info(f"Contacting trackers for {self.name}...")

        current_downloaded = self.session_downloaded
        current_uploaded = self.total_uploaded

        for url in self.announce_urls:
            if not url.startswith('udp:'):
                continue
            try:
                parsed = urllib.parse.urlparse(url)
                ip = socket.gethostbyname(parsed.hostname)
                port = parsed.port or 1337
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(3)
                tracker = TrackerClass((ip, port), sock, self.parsed_torrent, self.peer_id,
                                       listen_port=getattr(self, 'listen_port', 6881))
                peer_list = tracker.start_communicating(event=event,
                                                        downloaded=current_downloaded,
                                                        uploaded=current_uploaded)
                if peer_list:
                    self.peer_list = [(p['ip'], p['port']) for p in peer_list]
                    logger.info(f"Obtained {len(self.peer_list)} peers.")
                    return
            except Exception as e:
                logger.warning(f"Tracker {url}: {e}")

        if not self.peer_list:
            logger.warning("No peers obtained from any tracker.")

    # ── Peers ─────────────────────────────────────────────────────────────────

    def _connect_peers(self):
        from peer_manager import Peer
        connected_addrs = {p.peer for p in self.connected_peers}
        candidates = [p for p in self.peer_list if p not in connected_addrs]
        if not candidates:
            return

        def try_connect(peer_addr):
            if len(self.connected_peers) >= self.MAX_PEER_CONNECTIONS:
                return
            peer_obj = Peer(peer_addr, self.info_hash, self.peer_id,
                            pieces=self.pieces, piece_size=self.piece_size,
                            piece_amount=self.piece_amount, total_size=self.total_size,
                            download_files=self.download_files, client=self)
            if peer_obj.connect():
                if len(self.connected_peers) < self.MAX_PEER_CONNECTIONS:
                    self.connected_peers.append(peer_obj)
                    if getattr(self, 'piece_manager', None):
                        self.piece_manager.add_seeder(peer_obj.peer)
            else:
                try:
                    self.peer_list.remove(peer_addr)
                except ValueError:
                    pass

        batch = candidates[:min(30, len(candidates))]
        # Use our persistent thread pool to avoid thread creation overhead
        if not hasattr(self, '_connect_executor'):
            import concurrent.futures
            self._connect_executor = concurrent.futures.ThreadPoolExecutor(max_workers=30)
            
        # Fire and forget -- don't block the main loop waiting for 3-second connect timeouts!
        for addr in batch:
            self._connect_executor.submit(try_connect, addr)

    def _communicate_peers(self):
        peers = self.connected_peers.copy()
        if not peers:
            return

        if not hasattr(self, '_comm_executor'):
            import concurrent.futures
            self._comm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=128)
            self._active_tasks = set()

        def _comm(peer):
            try:
                return peer if not peer.communicate() else None
            except Exception:
                return peer

        def _on_done(future):
            peer = getattr(future, '_peer', None)
            if peer:
                self._active_tasks.discard(peer)
            bad = future.result()
            if bad:
                try:
                    self.connected_peers.remove(bad)
                except ValueError:
                    pass
                try:
                    self.peer_list.remove(bad.peer)
                except ValueError:
                    pass

        for peer in peers:
            if peer not in self._active_tasks:
                self._active_tasks.add(peer)
                future = self._comm_executor.submit(_comm, peer)
                future._peer = peer
                future.add_done_callback(_on_done)

    # ── Deletion ──────────────────────────────────────────────────────────────

    def delete_files(self):
        if self.download_files:
            return self.download_files.delete_files()
        return False
