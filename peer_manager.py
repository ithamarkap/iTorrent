import socket
import select
import logging
from peer_handling import (
    recv_by_length, handle_response, request_piece_block,
    pack_message, pack_bitfield, MessageID,
    send_extended_handshake
)

logger = logging.getLogger('PeerManager')

BLOCK_SIZE = 16384
HANDSHAKE_PSTR = b'BitTorrent protocol'
HANDSHAKE_RESERVED = b'\x00\x00\x00\x00\x00\x10\x00\x00'  # bit 43: extension protocol


# info_hash is the 20-byte SHA-1 hash of the torrent info dict
# peer_id is the 20-byte client identifier
def pack_handshake(info_hash: bytes, peer_id: bytes) -> bytes:
    """Packs the standard 68-byte BitTorrent handshake message."""
    # 1 byte for length of protocol string (19) + protocol string + 8 reserved bytes + 20-byte info_hash + 20-byte peer_id
    return bytes([len(HANDSHAKE_PSTR)]) + HANDSHAKE_PSTR + HANDSHAKE_RESERVED + info_hash + peer_id


def recv_handshake(sock, length=68) -> bytes | None:
    """Reads the handshake response from the socket in a loop."""
    try:
        data = bytearray(length)
        view = memoryview(data)
        received = 0
        while received < length:
            # read into the memoryview buffer slice to avoid copying memory
            n = sock.recv_into(view[received:], length - received)
            if not n:
                return None
            received += n
        return bytes(data)
    except socket.timeout:
        return None


def is_handshake(data: bytes) -> bool:
    """Validates if received data starts with a valid BitTorrent handshake protocol string."""
    return bool(data) and len(data) >= 68 and data[1:20] == HANDSHAKE_PSTR


def _build_bitfield_msg(pieces) -> bytes | None:
    """Build a BITFIELD message for all pieces we've verified. Returns None if nothing to send."""
    if not pieces or pieces.piece_amount == 0:
        return None
    # Calculate how many bytes are needed for the bitfield (8 pieces per byte)
    bitfield = bytearray((pieces.piece_amount + 7) // 8)
    has_any = False
    for i in range(pieces.piece_amount):
        # If we have downloaded all blocks of this piece, set the corresponding bit to 1
        if all(pieces.received[i]):
            # i // 8 gives us the byte index for this piece
            # 7 - (i % 8) gives us the bit index within that byte (from left, 0-7)
            # |= sets that bit to 1 without affecting other bits in the byte
            bitfield[i // 8] |= (1 << (7 - (i % 8)))
            has_any = True
    return pack_bitfield(bytes(bitfield)) if has_any else None


# Peer represents a connection to a single remote client/seeder in the swarm.
class Peer:
    __slots__ = (
        'peer', 'sock', 'queue', 'pending_requests', 'choked',
        'torrent_pieces', 'piece_size', 'piece_amount', 'total_size',
        'torrent_download_files', 'torrent_info_hash', 'torrent_peer_id',
        'client', 'uploaded', 'downloaded', 'supports_extensions',
        'ut_metadata_id', 'metadata_size', 'peer_interested', 'am_choking',
        'available_pieces', 'in_flight_requests', 'last_action_time'
    )

    def __init__(self, peer, info_hash, peer_id, pieces=None, piece_size=0,
                 piece_amount=0, total_size=0, download_files=None, client=None):
        # peer is a tuple (ip, port)
        self.peer = peer
        self.sock = None
        # queue of blocks we want to request from this peer (dicts with piece_index, begin, length)
        self.queue = []
        # number of request messages sent to this peer that have not yet been answered
        self.pending_requests = 0
        # choked represents whether the peer has choked us (True = we cannot request from them)
        self.choked = True
        # torrent_pieces references the global pieces tracker
        self.torrent_pieces = pieces
        self.piece_size = piece_size
        self.piece_amount = piece_amount
        self.total_size = total_size
        # torrent_download_files is the file writer/handler (Files instance)
        self.torrent_download_files = download_files
        self.torrent_info_hash = info_hash
        self.torrent_peer_id = peer_id
        self.client = client
        self.uploaded = 0
        self.downloaded = 0
        # supports_extensions is True if peer handshake reserved bytes indicate extension protocol support
        self.supports_extensions = False
        self.ut_metadata_id = None
        self.metadata_size = None
        # peer_interested is True if the peer is interested in downloading from us
        self.peer_interested = False
        # am_choking is True if we are choking the peer (preventing them from downloading from us)
        self.am_choking = True
        # available_pieces tracks which piece indices this peer has (based on BITFIELD/HAVE)
        self.available_pieces = set()
        # in_flight_requests tracks requests sent to this peer (maps (piece_idx, begin) -> block)
        self.in_flight_requests = {}
        import time as _time
        self.last_action_time = _time.time()

    # ── Queue helpers ─────────────────────────────────────────────────────────

    def empty(self):
        return len(self.queue) == 0

    def pop(self, index=0):
        return self.queue.pop(index)

    def add_piece_blocks(self, piece_index):
        # Only process if we haven't already marked this piece as available from this peer
        if piece_index in self.available_pieces:
            return
        self.available_pieces.add(piece_index)
        
        piece_sz = self.determine_piece_size(piece_index)
        # break the piece down into BLOCK_SIZE (16KB) blocks
        num_full = piece_sz // BLOCK_SIZE
        for i in range(num_full):
            self.queue.append({'piece_index': piece_index, 'begin': i * BLOCK_SIZE, 'length': BLOCK_SIZE})
        # handle remainder block if piece size is not a multiple of BLOCK_SIZE
        remainder = piece_sz % BLOCK_SIZE
        if remainder:
            self.queue.append({'piece_index': piece_index, 'begin': num_full * BLOCK_SIZE, 'length': remainder})

    def determine_piece_size(self, piece_index):
        # The last piece index may have a smaller size than the standard piece_size
        if piece_index == self.piece_amount - 1:
            last = self.total_size % self.piece_size
            return last if last != 0 else self.piece_size
        return self.piece_size

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            logger.debug(f"Connecting to {self.peer}...")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect(self.peer)
            # Send standard handshake
            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))

            # Receive standard handshake response
            response = recv_handshake(self.sock, 68)
            if not response or not is_handshake(response):
                logger.debug(f"Bad/no handshake from {self.peer}")
                return False

            # Check reserved bytes bit 43 (extension protocol support)
            self.supports_extensions = (response[25] & 0x10) != 0
            logger.debug(f"Handshake SUCCESS with {self.peer}. Extensions: {self.supports_extensions}")

            # Send extended handshake if supported
            if self.supports_extensions:
                send_extended_handshake(self.sock)
            # Unchoke peer and tell them we are interested in downloading
            self.sock.send(pack_message(MessageID.UNCHOKE))
            self.am_choking = False
            self.sock.send(pack_message(MessageID.INTERESTED))

            # Send our bitfield of downloaded pieces to peer if we have any
            bf_msg = _build_bitfield_msg(self.torrent_pieces)
            if bf_msg:
                self.sock.send(bf_msg)
            return True

        except Exception as e:
            err = str(e)
            # Quietly handle common network noise (Connection Refused, Timeout, Reset)
            if any(code in err for code in ['10061', '10054', '10060', 'timed out']):
                logger.debug(f"Connection noise from {self.peer}: {e}")
            else:
                logger.warning(f"Connection failed to {self.peer}: {e}")
            return False

    def accept_connection(self, client_sock, handshake_data=None) -> bool:
        try:
            self.sock = client_sock
            self.sock.settimeout(3)

            # If handshake data was already read by select loop, use it; otherwise read now
            if handshake_data:
                response = handshake_data
            else:
                response = recv_handshake(self.sock, 68)

            if not response or not is_handshake(response):
                logger.error(f"Invalid handshake from incoming peer {self.peer}")
                return False

            # Verify that peer's info_hash matches our torrent info_hash
            received_info_hash = response[28:48]
            if received_info_hash != self.torrent_info_hash:
                logger.error(f"Info hash mismatch from {self.peer}")
                return False

            # Determine extension protocol support and send handshake back
            self.supports_extensions = (response[25] & 0x10) != 0
            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))
            self.sock.send(pack_message(MessageID.UNCHOKE))
            self.am_choking = False

            # Send our bitfield to incoming peer
            bf_msg = _build_bitfield_msg(self.torrent_pieces)
            if bf_msg:
                self.sock.send(bf_msg)
            return True

        except Exception as e:
            logger.warning(f"Failed to handshake with incoming peer {self.peer}: {e}")
            return False

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        
        # Reclaim orphaned/in-flight requests back to global pieces tracker
        if self.in_flight_requests and self.torrent_pieces:
            for block in self.in_flight_requests.values():
                self.torrent_pieces.remove_request(block)
            self.in_flight_requests = {}
            self.pending_requests = 0

    # ── Communication ─────────────────────────────────────────────────────────

    def communicate(self) -> bool:
        """
        Drains up to 50 messages from the socket without blocking.
        Returns False if the connection should be dropped.
        """
        import time as _time
        now = _time.time()
        
        # Timeout: if we have pending requests but no action for 60 seconds, drop.
        if self.pending_requests > 0 and (now - self.last_action_time) > 60:
            logger.debug(f"Peer {self.peer} stalled (no data for 60s) -- dropping.")
            self.close()
            return False

        try:
            # Drain up to 50 messages in one pass to clear buffer rapidly
            for _ in range(50):
                readable, _, _ = select.select([self.sock], [], [], 0.002)
                if not readable:
                    break
                # Read complete message prefix + body
                msg = recv_by_length(self.sock)
                if msg is False or msg is None:
                    self.close()
                    # If still fetching metadata, don't abort immediately unless needed
                    return self.torrent_pieces is None
                
                self.last_action_time = now
                # Route and handle received message
                handle_response(self.sock, msg, self.torrent_pieces, self, self.torrent_download_files)

            # If connected, not choked, and download not complete, request more blocks
            if self.torrent_pieces is not None and not self.torrent_pieces.is_done():
                request_piece_block(self.sock, self.torrent_pieces, self)
            return True

        except Exception:
            self.close()
            return self.torrent_pieces is None
