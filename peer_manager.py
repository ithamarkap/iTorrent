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


def pack_handshake(info_hash: bytes, peer_id: bytes) -> bytes:
    return bytes([len(HANDSHAKE_PSTR)]) + HANDSHAKE_PSTR + HANDSHAKE_RESERVED + info_hash + peer_id


def recv_handshake(sock, length=68) -> bytes | None:
    try:
        data = bytearray(length)
        view = memoryview(data)
        received = 0
        while received < length:
            n = sock.recv_into(view[received:], length - received)
            if not n:
                return None
            received += n
        return bytes(data)
    except socket.timeout:
        return None


def is_handshake(data: bytes) -> bool:
    return bool(data) and len(data) >= 68 and data[1:20] == HANDSHAKE_PSTR


def _build_bitfield_msg(pieces) -> bytes | None:
    """Build a BITFIELD message for all pieces we've verified. Returns None if nothing to send."""
    if not pieces or pieces.piece_amount == 0:
        return None
    bitfield = bytearray((pieces.piece_amount + 7) // 8)
    has_any = False
    for i in range(pieces.piece_amount):
        if all(pieces.received[i]):
            bitfield[i // 8] |= (1 << (7 - (i % 8)))
            has_any = True
    return pack_bitfield(bytes(bitfield)) if has_any else None


class Peer:
    __slots__ = (
        'peer', 'sock', 'queue', 'pending_requests', 'choked',
        'torrent_pieces', 'piece_size', 'piece_amount', 'total_size',
        'torrent_download_files', 'torrent_info_hash', 'torrent_peer_id',
        'client', 'uploaded', 'downloaded', 'supports_extensions',
        'ut_metadata_id', 'metadata_size', 'peer_interested', 'am_choking',
        'available_pieces'
    )

    def __init__(self, peer, info_hash, peer_id, pieces=None, piece_size=0,
                 piece_amount=0, total_size=0, download_files=None, client=None):
        self.peer = peer
        self.sock = None
        self.queue = []
        self.pending_requests = 0
        self.choked = True
        self.torrent_pieces = pieces
        self.piece_size = piece_size
        self.piece_amount = piece_amount
        self.total_size = total_size
        self.torrent_download_files = download_files
        self.torrent_info_hash = info_hash
        self.torrent_peer_id = peer_id
        self.client = client
        self.uploaded = 0
        self.downloaded = 0
        self.supports_extensions = False
        self.ut_metadata_id = None
        self.metadata_size = None
        self.peer_interested = False
        self.am_choking = True
        self.available_pieces = set()

    # ── Queue helpers ─────────────────────────────────────────────────────────

    def empty(self):
        return len(self.queue) == 0

    def pop(self, index=0):
        return self.queue.pop(index)

    def add_piece_blocks(self, piece_index):
        if piece_index in self.available_pieces:
            return
        self.available_pieces.add(piece_index)
        
        piece_sz = self.determine_piece_size(piece_index)
        num_full = piece_sz // BLOCK_SIZE
        for i in range(num_full):
            self.queue.append({'piece_index': piece_index, 'begin': i * BLOCK_SIZE, 'length': BLOCK_SIZE})
        remainder = piece_sz % BLOCK_SIZE
        if remainder:
            self.queue.append({'piece_index': piece_index, 'begin': num_full * BLOCK_SIZE, 'length': remainder})

    def determine_piece_size(self, piece_index):
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
            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))

            response = recv_handshake(self.sock, 68)
            if not response or not is_handshake(response):
                logger.debug(f"Bad/no handshake from {self.peer}")
                return False

            self.supports_extensions = (response[25] & 0x10) != 0
            logger.debug(f"Handshake SUCCESS with {self.peer}. Extensions: {self.supports_extensions}")

            if self.supports_extensions:
                send_extended_handshake(self.sock)
            self.sock.send(pack_message(MessageID.UNCHOKE))
            self.am_choking = False
            self.sock.send(pack_message(MessageID.INTERESTED))

            bf_msg = _build_bitfield_msg(self.torrent_pieces)
            if bf_msg:
                self.sock.send(bf_msg)
            return True

        except Exception as e:
            err = str(e)
            if '[WinError 10061]' not in err and 'timed out' not in err:
                logger.debug(f"Connection failed to {self.peer}: {e}")
            return False

    def accept_connection(self, client_sock) -> bool:
        try:
            self.sock = client_sock
            self.sock.settimeout(3)

            response = recv_handshake(self.sock, 68)
            if not response or not is_handshake(response):
                logger.error(f"Invalid handshake from incoming peer {self.peer}")
                return False

            received_info_hash = response[28:48]
            if received_info_hash != self.torrent_info_hash:
                logger.error(f"Info hash mismatch from {self.peer}")
                return False

            self.supports_extensions = (response[25] & 0x10) != 0
            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))
            self.sock.send(pack_message(MessageID.UNCHOKE))
            self.am_choking = False

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

    # ── Communication ─────────────────────────────────────────────────────────

    def communicate(self) -> bool:
        """
        Drains up to 50 messages from the socket without blocking.
        Returns False if the connection should be dropped.
        """
        try:
            for _ in range(50):
                readable, _, _ = select.select([self.sock], [], [], 0.002)
                if not readable:
                    break
                msg = recv_by_length(self.sock)
                if msg is False or msg is None:
                    return self.torrent_pieces is None  # OK only if still fetching metadata
                handle_response(self.sock, msg, self.torrent_pieces, self, self.torrent_download_files)

            if self.torrent_pieces is not None and not self.torrent_pieces.is_done():
                request_piece_block(self.sock, self.torrent_pieces, self)
            return True

        except Exception:
            return self.torrent_pieces is None
