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
import winreg

logger = logging.getLogger('TorrentClient')


def get_default_download_path():
    """Returns the user's default downloads path on Windows."""
    try:
        # Access Windows registry key containing user shells folder paths
        sub_key = r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
        # The GUID for the Downloads folder
        # https://learn.microsoft.com/en-us/dotnet/desktop/winforms/controls/known-folder-guids-for-file-dialog-custom-places
        downloads_guid = '{374DE290-123F-4565-9164-39C4925E467B}'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
            location, _ = winreg.QueryValueEx(key, downloads_guid)
        # Expand environment variables like %USERPROFILE% if present in path
        return os.path.expandvars(location)
    except Exception as e:
        logger.warning(f"Failed to get system downloads folder, falling back to project Downloads: {e}")
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")


# ── Torrent Parsers ───────────────────────────────────────────────────────────

class TorrentClass:
    """Parsed torrent metadata."""
    # announce is the tracker URL
    # trackers is a list of all tracker URLs (optional)
    # pieces is a string of all piece info hashes
    # files is a list of all files in the torrent
    def __init__(self, name, info_hash, size, announce, trackers=None,
                 piece_length=0, pieces=b'', files=None):
        self.name = name
        self.info_hash = info_hash
        self.size = size
        self.announce = announce
        # Standardize tracker endpoints into a single list
        self.trackers = trackers if trackers else ([announce] if announce else [])
        self.piece_length = piece_length
        self.pieces = pieces
        self.files = files if files is not None else []
        # Each piece is represented by a 20-byte SHA-1 hash in the pieces string
        self.piece_amount = len(pieces) // 20 if pieces else 0


def parse_torrent_file(file_path: str) -> TorrentClass:
    """Reads and parses a .torrent file, returning a TorrentClass metadata container."""
    with open(file_path, 'rb') as f:
        # Torrent files are bencoded binaries
        data = bencodepy.decode(f.read())

    # Extract 'info' dictionary and encode it into bytes
    info = data[b'info']
    # The info_hash is the SHA-1 of the bencoded info dictionary, used to verify torrent identity
    info_hash = hashlib.sha1(bencodepy.encode(info)).digest()
    name = info[b'name'].decode('utf-8')

    # Single-file torrents have a 'length' key, multi-file torrents have a 'files' list
    if b'length' in info:
        # Extract 'length' key (total size of single file)
        size = info[b'length']
        # Create files list with single file
        files = [{'name': name, 'length': size}]
    else:
        # Multi-file torrents have a 'files' list
        size = sum(f[b'length'] for f in info[b'files'])
        # Create files list with multiple files
        files = [{'path': [p.decode('utf-8') for p in f[b'path']], 'length': f[b'length']}
                 for f in info[b'files']]

    announce = data.get(b'announce', b'').decode('utf-8')
    trackers = []
    # Collect all fallback or alternative trackers from the announce-list
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
    """Parses standard metadata (info_hash, trackers, display name) from a magnet URL."""
    if not magnet_url.startswith('magnet:?'):
        raise ValueError("Invalid magnet link")

    # Parse query parameters from the magnet URI
    params = urllib.parse.parse_qs(urllib.parse.urlparse(magnet_url).query)

    # 'xt' (exact topic) contains the info hash (BTIH: BitTorrent Info Hash)
    if 'xt' not in params:
        raise ValueError("Magnet link missing xt parameter")
    xt = params['xt'][0]
    if not xt.startswith('urn:btih:'):
        raise ValueError("Invalid xt parameter")

    # Extract the info hash string following 'urn:btih:'
    hash_str = xt[9:]
    # If hex encoded (40 characters)
    if len(hash_str) == 40:
        info_hash = bytes.fromhex(hash_str)
    # If base32 encoded (32 characters)
    elif len(hash_str) == 32:
        import base64
        info_hash = base64.b32decode(hash_str.upper())
    else:
        raise ValueError("Invalid info hash length")

    # 'dn' (display name) provides a tentative name before metadata download
    name = params.get('dn', ['Unknown'])[0]
    # 'tr' (tracker) lists initial swarm trackers
    trackers = params.get('tr', [])
    return TorrentClass(name, info_hash, 0, trackers[0] if trackers else '', trackers)


class TorrentClient:
    """Main torrent download/seed controller."""

    MAX_PEER_CONNECTIONS = 200

    def __init__(self, torrent_path=None, magnet_url=None):
        # Store input paths/links
        self.torrent_path = torrent_path
        self.magnet_url = magnet_url
        # Unique 20-byte random client ID (sent to trackers and peers)
        self.peer_id = os.urandom(20)
        # Error flag to track if initialization failed
        self.Error = False

        try:
            # Load metadata from torrent file or initial magnet link details
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

        # Base path for downloads
        self.relative_directory = get_default_download_path()
        self.files = getattr(self.parsed_torrent, 'files', [])
        # download_files handles disk reads/writes (instance of Files class)
        self.download_files = None
        # pieces tracks block levels (instance of Pieces class)
        self.pieces = None
        # piece_manager handles piece/block selection strategy (instance of PieceManager)
        self.piece_manager = None

        # Swarm peer address candidates (ip, port)
        self.peer_list = []
        # Currently active Peer connections
        self.connected_peers = []
        # Running status flag for download loop
        self.is_downloading = False

        # Speed and session transfer statistics
        self.download_speed = 0
        self.upload_speed = 0
        self.session_downloaded = 0
        self.session_uploaded = 0
        self.total_uploaded = 0
        self.start_time = time.time()
        self.status = 'Paused'
        self.is_locked = False

        # Dictionary accumulating metadata payload pieces for magnet links
        self.metadata_buffer = {}

        # piece_index -> { 'ip': str|None, 'port': int|None }
        # Best-effort updated whenever a PIECE block is received.
        self.piece_sources = {}

        # Setup piece/block managers if metadata is already available
        if self.piece_amount > 0 and not self._setup_pieces():
            self.Error = True

        self._setup_download_files()
        self._verify_existing_files()

    # ── Metadata (magnet) ─────────────────────────────────────────────────────

    def verify_and_install_metadata(self, metadata_size):
        """Assembles, verifies, and installs torrent metadata fetched via extension protocol."""
        # Check if the metadata buffer contains any downloaded pieces; abort if empty
        if not self.metadata_buffer:
            return

        # Concatenate all downloaded metadata buffer chunks in ascending index order
        metadata_bytes = b''.join(self.metadata_buffer[i] for i in sorted(self.metadata_buffer))

        # Perform SHA-1 hash validation of the full metadata bytes against info_hash
        if hashlib.sha1(metadata_bytes).digest() != self.info_hash:
            logger.error("Metadata hash mismatch!")
            return

        logger.info("Metadata verified successfully.")

        try:
            # Decode bencoded metadata info dictionary retrieved from the swarm
            info = bencodepy.decode(metadata_bytes)

            # Extract the actual name of the torrent and update display name if unknown
            name = info.get(b'name', b'').decode('utf-8')
            if name and self.name == 'Unknown':
                self.name = name

            # Determine whether single-file (has 'length') or multi-file torrent
            if b'length' in info:
                # Store the size of the single file
                self.total_size = info[b'length']
                # Create a list containing the single file path mapping
                self.files = [{'name': name, 'length': self.total_size}]
            else:
                # Sum lengths of all files listed in the metadata
                self.total_size = sum(f[b'length'] for f in info.get(b'files', []))
                # Map raw multi-file details (paths and lengths) to files list
                self.files = [{'path': [p.decode('utf-8') for p in f.get(b'path', [])],
                                'length': f.get(b'length', 0)}
                               for f in info.get(b'files', [])]

            # Store the standard size of each piece block
            self.piece_size = info.get(b'piece length', 0)
            # Store the byte string containing the SHA-1 hash for all pieces
            self.pieces_hash = info.get(b'pieces', b'')
            # Compute total piece amount by dividing hashes string length by 20 bytes
            self.piece_amount = len(self.pieces_hash) // 20

            # Instantiate tracking engines and pieces manager components
            if self.piece_amount > 0 and not self._setup_pieces():
                self.Error = True

            # Prepare downloads directory structures and allocate files on disk
            self._setup_download_files()
            # Perform verification check on any pre-existing files/blocks on disk
            self._verify_existing_files()
            # Transition status from metadata fetching to active downloading
            self.status = 'Downloading'

            # Feed new metadata parameters to already established peer connections
            for peer in self.connected_peers:
                peer.piece_size = self.piece_size
                peer.piece_amount = self.piece_amount
                peer.total_size = self.total_size
                peer.torrent_pieces = self.pieces
                peer.torrent_download_files = self.download_files
                
            # Transition to seeding immediately if the files are already completed on disk
            if self.pieces and self.pieces.is_done():
                self.status = 'Seeding'
                self.broadcast_bitfield()

        except Exception as e:
            logger.error(f"Failed to install metadata: {e}")

    # ── File / Piece Setup ────────────────────────────────────────────────────

    def _verify_existing_files(self):
        """Checks hashes of existing files on disk to resume partial downloads."""
        self.status = 'Verifying'
        if not self.pieces or not self.download_files:
            return

        verified_count = 0
        for i in range(self.piece_amount):
            try:
                # Compare SHA-1 hash of data on disk with the metadata hash for piece i
                if self.download_files.check_piece_hash(i):
                    verified_count += 1
                    # Mark all blocks of this piece as received and requested
                    for block_idx in range(len(self.pieces.received[i])):
                        if not self.pieces.received[i][block_idx]:
                            self.pieces.received[i][block_idx] = True
                            self.pieces.requested[i][block_idx] = True
                            self.pieces._received_count += 1
            except Exception:
                pass

        logger.info(f"Verified {verified_count}/{self.piece_amount} existing pieces.")

    def _setup_pieces(self) -> bool:
        """Initializes components tracking downloaded and requested blocks."""
        # Import Pieces and PieceManager dynamically from pieces_manager
        from pieces_manager import Pieces, PieceManager
        try:
            # Instantiate Pieces helper tracking received and requested block levels
            self.pieces = Pieces(self.piece_size, self.piece_amount, self.total_size)
            # Instantiate PieceManager coordinator handling chunk requesting logic
            self.piece_manager = PieceManager(self.pieces)
            return True
        except Exception as e:
            logger.error(f"Failed to setup pieces: {e}")
            return False

    def _setup_download_files(self):
        """Initializes the files system handler and sets up placeholders on disk."""
        try:
            # Use torrent file stem or torrent name as target folder name
            directory_name = Path(self.torrent_path).stem if self.torrent_path else (self.name or "Unknown_Torrent")
            # Build target download folder path
            path = os.path.join(self.relative_directory, directory_name)

            # Import Files dynamically from files_manager
            from files_manager import Files
            # Instantiate Files helper to handle files creation and random-access disk writing
            self.download_files = Files(self.piece_size, self.pieces_hash, self.files, path)
            # Create download root directory if it does not exist
            self.download_files.create_directory()
            # Allocate files of correct size on disk (placeholders)
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
            # Keep only the latest peer source that successfully uploaded this piece
            self.piece_sources[int(piece_index)] = {
                'ip': ip,
                'port': port,
            }
        except Exception:
            pass

    def get_gui_data(self) -> dict:
        """Assembles state snapshot for display/update in GUI."""
        progress = self.pieces.get_progress() if self.pieces else 0

        # Helper to format speed into human-readable B/s, KB/s, or MB/s
        def format_speed(b):
            if b >= 1_048_576:
                return f"{b / 1_048_576:.2f} MB/s"
            if b >= 1024:
                return f"{b / 1024:.2f} KB/s"
            return f"{b:.0f} B/s"

        # Generate a bitfield representation showing fully received pieces (1) and missing pieces (0)
        bitfield = []
        if self.pieces:
            bitfield = [1 if all(self.pieces.received[i]) else 0 for i in range(self.piece_amount)]

        elapsed = time.time() - self.start_time
        remaining = self.total_size * (1 - progress / 100)
        eta = int(remaining / self.download_speed) if self.download_speed > 0 else -1

        # Aggregate piece origins to display peers contributing to the download
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
            'is_locked': getattr(self, 'is_locked', False),
        }


    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Starts client operation (registers with NetworkEngine, spins download loop thread)."""
        if self.Error:
            return
        self.is_downloading = True
        if self.pieces and self.pieces.is_done():
            self.status = 'Seeding'
        else:
            self.status = 'Downloading'
            
        # Register info_hash with global NetworkEngine to routing incoming connections
        from network_engine import network_engine
        network_engine.register(self.info_hash, self)
        self.listen_port = network_engine.port
        
        # Start download loop as a daemon background thread
        self.thread = threading.Thread(target=self._download_loop, daemon=True)
        self.thread.start()
        
        # Immediately notify existing peers if starting in Seeding state
        if self.status == 'Seeding':
            self.broadcast_bitfield()

    def broadcast_bitfield(self):
        """Send a full Bitfield message to all connected peers to signal completion."""
        from peer_handling import pack_bitfield
        if not self.pieces or self.piece_amount <= 0:
            return
            
        # Build bitfield showing completion of all pieces
        bitfield = bytearray((self.piece_amount + 7) // 8)
        for i in range(self.piece_amount):
            if all(self.pieces.received[i]):
                bitfield[i // 8] |= (1 << (7 - (i % 8)))
        
        bf_msg = pack_bitfield(bytes(bitfield))
        # Send full completion bitfield to notify peers we are now seeding
        for p in self.connected_peers:
            try:
                if p.sock:
                    p.sock.send(bf_msg)
            except Exception:
                pass

    def stop(self):
        """Pauses download operations and unregisters from the NetworkEngine routing table."""
        self.is_downloading = False
        from network_engine import network_engine
        network_engine.unregister(self.info_hash)
        self.status = 'Paused'

    def _download_loop(self):
        """Core client execution thread orchestrating trackers, peers, speeds, and hash verification."""
        # Import protocol encoders and action IDs dynamically
        from peer_handling import pack_message, MessageID, pack_bitfield, pack_have
        
        # Store initial loop timing reference
        last_time = time.time()
        # Flag to prevent multiple complete announces sent to the trackers
        has_announced_completed = False
        # Timestamp reference tracking choke management intervals (every 10s)
        last_choke_time = 0
        # Set storing indices of verified downloaded pieces to avoid re-verification
        verified_pieces = set()

        # Execute loop continuously until client download state is disabled
        while self.is_downloading:
            now = time.time()

            # ── Seeding transition ────────────────────────────────────────────
            # Transition from active download to seeding once all pieces are verified
            if self.pieces and self.pieces.is_done() and self.status != 'Seeding':
                logger.info(f"{self.name} complete -- switching to Seeding.")
                self.status = 'Seeding'
                # Reset download speed metric since we are no longer downloading
                self.download_speed = 0
                if not has_announced_completed:
                    # Notify trackers about successful download completion (event=1)
                    self._get_peer_list(event=1)
                    has_announced_completed = True
                # Broadcast seeding state to active peers
                self.broadcast_bitfield()

            # For magnet links that do not have metadata downloaded yet, update status
            if not self.pieces:
                self.status = 'Fetching Metadata'

            # ── Tracker / peer management ─────────────────────────────────────
            # Periodically query trackers to find new peers in the swarm (every 30 seconds)
            if getattr(self, 'peer_list_timer', 0) <= now:
                # Poll list if empty or if active connections dropped below half capacity
                if not self.peer_list or len(self.connected_peers) < self.MAX_PEER_CONNECTIONS // 2:
                    self._get_peer_list()
                self.peer_list_timer = now + 30

            # Establish outgoing connections to new peers if capacity permits
            if len(self.connected_peers) < self.MAX_PEER_CONNECTIONS:
                self._connect_peers()

            # Process pending socket messages for all established peer connections
            self._communicate_peers()

            # ── Periodic choke management (seeding) ───────────────────────────
            # Every 10 seconds, unchoke interested peers so they can download from us
            if now - last_choke_time >= 10:
                last_choke_time = now
                if self.status == 'Seeding':
                    # Pack UNCHOKE message payload
                    unchoke_msg = pack_message(MessageID.UNCHOKE)
                    # Send UNCHOKE message to all interested choked peers
                    for p in self.connected_peers:
                        if p.peer_interested and p.am_choking and p.sock:
                            try:
                                p.sock.send(unchoke_msg)
                                p.am_choking = False
                            except Exception:
                                pass

            # ── 1-second stats tick ───────────────────────────────────────────
            # Update data speeds and perform cryptographic hash verification for completed pieces
            elapsed = now - last_time
            if elapsed >= 1.0:
                if self.pieces:
                    dl_tick = 0
                    ul_tick = 0
                    # Aggregate downloaded/uploaded bytes per peer and reset their counters
                    for p in self.connected_peers:
                        dl_tick += p.downloaded;  p.downloaded = 0
                        ul_tick += p.uploaded;    p.uploaded = 0

                    # Compute current transfer speeds (Bytes per second)
                    self.download_speed = dl_tick / elapsed
                    self.upload_speed = ul_tick / elapsed
                    # Accumulate session transfer metrics
                    self.session_downloaded += dl_tick
                    self.session_uploaded += ul_tick
                    self.total_uploaded += ul_tick

                    # Verify piece SHA-1 integrity once all blocks of the piece are received
                    have_msg_cache = {}
                    for i in range(self.pieces.piece_amount):
                        # Identify completed pieces that haven't been cryptographically verified yet
                        if i not in verified_pieces and all(self.pieces.received[i]):
                            # Read piece data from disk and compare its SHA-1 hash to metadata
                            if self.download_files.check_piece_hash(i):
                                verified_pieces.add(i)
                                # Prepare HAVE message to broadcast to all connected peers
                                if i not in have_msg_cache:
                                    have_msg_cache[i] = pack_have(i)
                                for p in self.connected_peers:
                                    try:
                                        if p.sock:
                                            p.sock.send(have_msg_cache[i])
                                    except Exception:
                                        pass
                            else:
                                # Revert block status if verification fails (discard bad blocks)
                                logger.warning(
                                    f"Piece {i} failed hash check; resetting blocks. "
                                    f"total_size={getattr(self, 'total_size', None)} piece_size={getattr(self, 'piece_size', None)} "
                                    f"blocks={len(self.pieces.received[i])} received_count={self.pieces._received_count}"
                                )
                                for block_idx in range(len(self.pieces.received[i])):
                                    if self.pieces.received[i][block_idx]:
                                        self.pieces.received[i][block_idx] = False
                                        self.pieces._received_count -= 1
                                    self.pieces.requested[i][block_idx] = False

                else:
                    self.download_speed = 0
                    self.upload_speed = 0

                last_time = now

            # Sleep briefly to yield CPU execution context
            time.sleep(0.01)

    # ── Tracker ───────────────────────────────────────────────────────────────

    def _get_peer_list(self, event=0):
        """Contact trackers via UDP to obtain new swarm peer address details."""
        from tracker_client import TrackerClass
        logger.info(f"Contacting trackers for {self.name}...")

        current_downloaded = self.session_downloaded
        current_uploaded = self.total_uploaded

        for url in self.announce_urls:
            # Our client supports UDP trackers ('udp://...')
            if not url.startswith('udp:'):
                continue
            try:
                # Parse UDP tracker hostname and port
                parsed = urllib.parse.urlparse(url)
                ip = socket.gethostbyname(parsed.hostname)
                port = parsed.port or 1337
                # Open socket and request peer list from UDP tracker
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(3)
                tracker = TrackerClass((ip, port), sock, self.parsed_torrent, self.peer_id,
                                       listen_port=getattr(self, 'listen_port', 6881))
                peer_list = tracker.start_communicating(event=event,
                                                        downloaded=current_downloaded,
                                                        uploaded=current_uploaded)
                if peer_list:
                    # Map result to a list of (ip, port) pairs
                    self.peer_list = [(p['ip'], p['port']) for p in peer_list]
                    logger.info(f"Obtained {len(self.peer_list)} peers.")
                    return
            except Exception as e:
                logger.warning(f"Tracker {url}: {e}")

        if not self.peer_list:
            logger.warning("No peers obtained from any tracker.")

    # ── Peers ─────────────────────────────────────────────────────────────────

    def _connect_peers(self):
        """Initiates concurrent outgoing handshake connections to candidates in the peer list."""
        from peer_manager import Peer
        connected_addrs = {p.peer for p in self.connected_peers}
        candidates = [p for p in self.peer_list if p not in connected_addrs]
        if not candidates:
            return

        def try_connect(peer_addr):
            # Check maximum capacity check
            if len(self.connected_peers) >= self.MAX_PEER_CONNECTIONS:
                return
            peer_obj = Peer(peer_addr, self.info_hash, self.peer_id,
                            pieces=self.pieces, piece_size=self.piece_size,
                            piece_amount=self.piece_amount, total_size=self.total_size,
                            download_files=self.download_files, client=self)
            # Perform handshaking logic (connect() blocking call)
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

        # Connect to up to 30 candidate addresses simultaneously using ThreadPool
        batch = candidates[:min(30, len(candidates))]
        if not hasattr(self, '_connect_executor'):
            import concurrent.futures
            self._connect_executor = concurrent.futures.ThreadPoolExecutor(max_workers=30)
            
        # Dispatch connection tasks asynchronously so we don't block the main loop thread
        for addr in batch:
            self._connect_executor.submit(try_connect, addr)

    def _communicate_peers(self):
        """Dispatches communication threads to read and parse socket packets from peers."""
        peers = self.connected_peers.copy()
        if not peers:
            return

        if not hasattr(self, '_comm_executor'):
            import concurrent.futures
            # Maintain a pool of workers for concurrent I/O operations
            self._comm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=64)
            self._active_tasks = set()

        def _comm(peer):
            try:
                # If communicate() returns False, the peer socket failed or timed out
                return peer if not peer.communicate() else None
            except Exception:
                return peer

        def _on_done(future):
            # Future callback to clean up dead connections or failed peers
            peer = getattr(future, '_peer', None)
            if peer:
                self._active_tasks.discard(peer)
            bad = future.result()
            if bad:
                if bad in self.connected_peers:
                    try:
                        self.connected_peers.remove(bad)
                    except ValueError:
                        pass
                if bad.peer in self.peer_list:
                    try:
                        self.peer_list.remove(bad.peer)
                    except ValueError:
                        pass

        # Submit task for each peer that is not already processing
        for peer in peers:
            if peer not in self._active_tasks:
                self._active_tasks.add(peer)
                future = self._comm_executor.submit(_comm, peer)
                future._peer = peer
                future.add_done_callback(_on_done)

    # ── Deletion ──────────────────────────────────────────────────────────────

    def delete_files(self):
        """Deletes downloaded file payload from local disk."""
        if self.download_files:
            return self.download_files.delete_files()
        return False
