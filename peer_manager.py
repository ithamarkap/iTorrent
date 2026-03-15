import socket
import logging

logger = logging.getLogger('PeerManager')

BLOCK_SIZE = 16384

# Message Utility Functions

def pack_handshake(info_hash: bytes, peer_id: bytes) -> bytes:
    """Creates the 68-byte BitTorrent handshake payload."""
    pstr = b"BitTorrent protocol"
    pstrlen = bytes([len(pstr)])
    reserved = b'\x00' * 8
    return pstrlen + pstr + reserved + info_hash + peer_id

def recv_handshake(sock, length=68):
    """Receives exactly `length` bytes from the socket."""
    try:
        data = b''
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        return data
    except socket.timeout:
        return None

def is_handshake(data: bytes) -> bool:
    """Validates the BitTorrent handshake response."""
    if not data or len(data) < 68:
        return False
    return data[1:20] == b"BitTorrent protocol"

def pack_keep_alive() -> bytes:
    """Keep-alive message is just 4 zero bytes."""
    return b'\x00\x00\x00\x00'

# Placeholders for future client development
def recv_by_length(sock):
    pass

def handle_response(sock, response_data, torrent_pieces, peer, torrent_download_files):
    pass

def request_piece_block(sock, torrent_pieces, peer):
    pass

class Peer:
    def __init__(self, peer, info_hash, peer_id, pieces=None, piece_size=0, piece_amount=0, total_size=0, download_files=None):
        self.peer = peer  # Tuples of (ip, port)
        self.sock = None
        self.queue = list()
        self.choked = True
        self.torrent_pieces = pieces
        self.piece_size = piece_size
        self.piece_amount = piece_amount
        self.total_size = total_size
        self.torrent_download_files = download_files
        self.torrent_info_hash = info_hash
        self.torrent_peer_id = peer_id
        self.is_not_listening = False

    def add_piece_blocks(self, piece_index):
        number_of_full_blocks = self.determine_piece_size(piece_index) // BLOCK_SIZE  # Taking the integer value.
        for i in range(number_of_full_blocks):
            piece_block = {'piece_index': piece_index, 'begin': i * BLOCK_SIZE, 'length': BLOCK_SIZE}
            self.queue.append(piece_block)

        if self.determine_piece_size(piece_index) % BLOCK_SIZE:
            piece_block = {'piece_index': piece_index, 'begin': number_of_full_blocks * BLOCK_SIZE,
                           'length': self.determine_piece_size(piece_index) % BLOCK_SIZE}
            self.queue.append(piece_block)

    def determine_piece_size(self, piece_index):
        if piece_index == self.piece_amount - 1:
            return self.total_size % self.piece_size
        return self.piece_size

    def connect(self):
        try:
            logger.info(f"Attempting connection to peer {self.peer}...")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # Internet, TCP
            self.sock.settimeout(3)
            self.sock.connect(self.peer)

            logger.info("TCP Connection established. Sending BitTorrent handshake...")
            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))
            
            logger.info("Handshake sent. Waiting for response...")
            response_data = recv_handshake(self.sock, 68)  # Receive handshake (68 bytes).
            
            if not response_data:
                logger.error("No response from peer.")
                return False
                
            if is_handshake(response_data):
                # We can also check if the peer returned the same info_hash here
                logger.info(f"SUCCESS: Valid BitTorrent handshake received from {self.peer}!")
                return True
            else:
                logger.error("Received data is not a valid BitTorrent handshake.")
                return False

        except Exception as e:
            logger.warning(f"Failed to connect or handshake with {self.peer}: {e}")
            return False

    def close(self):
        if self.sock:
            self.sock.close()

    def keep_alive(self):
        try:
            if self.sock:
                self.sock.send(pack_keep_alive())
                return True
            return False
        except Exception as e:
            logger.error(f"Keep-alive failed: {e}")
            return False

    def communicate(self):
        try:
            logger.debug("Communicating")
            response_data = recv_by_length(self.sock)
            if response_data:
                self.is_not_listening = False
                logger.debug("Hi There Response!")
                handle_response(self.sock, response_data, self.torrent_pieces, self, self.torrent_download_files)
                logger.debug("Hello There Response!")
            else:
                if self.is_not_listening:  # Meaning didn't respond twice in a row.
                    self.sock.shutdown(socket.SHUT_RDWR)
                    return False
                self.is_not_listening = True

                logger.debug("Hi There!")
                request_piece_block(self.sock, self.torrent_pieces, self)
                logger.debug("Hello There!")
            return True

        except Exception as e:
            logger.error(f"Communicate failed: {e}")
            return False

    def empty(self):
        return len(self.queue) == 0

    def pop(self, index=0):
        return self.queue.pop(index)

    def remove(self, block):
        return self.queue.remove(block)

    def peek(self):
        return self.queue[0]
