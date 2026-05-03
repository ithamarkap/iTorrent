import socket
import logging

logger = logging.getLogger('PeerManager')

BLOCK_SIZE = 16384

# Message Utility Functions

def pack_handshake(info_hash: bytes, peer_id: bytes) -> bytes:
    """Creates the 68-byte BitTorrent handshake payload."""
    pstr = b"BitTorrent protocol"
    pstrlen = bytes([len(pstr)])
    reserved = b'\x00\x00\x00\x00\x00\x10\x00\x00'  # 43rd bit signals extension protocol (BEP 0009) support
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

from peer_handling import recv_by_length, handle_response, request_piece_block

class Peer:
    def __init__(self, peer, info_hash, peer_id, pieces=None, piece_size=0, piece_amount=0, total_size=0, download_files=None, client=None):
        self.peer = peer  # Tuples of (ip, port)
        self.sock = None
        self.queue = list()
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
        self.is_not_listening = False
        self.uploaded = 0
        self.downloaded = 0
        self.supports_extensions = False
        self.ut_metadata_id = None
        self.metadata_size = None
        self.peer_interested = False  # Peer wants to download FROM us
        self.am_choking = True        # We are choking the peer (not sending to them)

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
            print(f"DEBUG: Connecting to {self.peer}...")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10) # Increased timeout
            self.sock.connect(self.peer)

            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))
            
            response_data = recv_handshake(self.sock, 68)
            
            if not response_data:
                print(f"DEBUG: No handshake response from {self.peer}")
                return False
                
            if is_handshake(response_data):
                self.supports_extensions = (response_data[25] & 0x10) != 0
                print(f"DEBUG: Handshake SUCCESS with {self.peer}. Extensions: {self.supports_extensions}")
                
                try:
                    from peer_handling import pack_message, MessageID, pack_bitfield, send_extended_handshake
                    
                    if self.supports_extensions:
                        send_extended_handshake(self.sock)
                        
                    self.sock.send(pack_message(MessageID.UNCHOKE))
                    self.am_choking = False
                    self.sock.send(pack_message(MessageID.INTERESTED)) # Signify interest for pieces/metadata
                    
                    if self.torrent_pieces and self.torrent_pieces.piece_amount > 0:
                        bitfield = bytearray((self.torrent_pieces.piece_amount + 7) // 8)
                        has_any = False
                        for i in range(self.torrent_pieces.piece_amount):
                            if all(self.torrent_pieces.received[i]):
                                bitfield[i // 8] |= (1 << (7 - (i % 8)))
                                has_any = True
                        if has_any:
                            self.sock.send(pack_bitfield(bytes(bitfield)))
                except Exception as e:
                    print(f"DEBUG: Error during post-handshake for {self.peer}: {e}")
                    
                return True
            else:
                print(f"DEBUG: Invalid handshake from {self.peer}")
                return False

        except Exception as e:
            # Keep this more quiet to avoid spamming the same error
            # but print if it's not a generic timeout/refusal
            if "[WinError 10061]" not in str(e) and "timed out" not in str(e):
                print(f"DEBUG: Connection failed to {self.peer}: {e}")
            return False

    def accept_connection(self, client_sock):
        """
        Passive accept of an incoming TCP connection.
        Skips the outgoing connect block, but immediately waits for a handshake.
        """
        try:
            self.sock = client_sock
            self.sock.settimeout(3)
            
            logger.info(f"Accepted TCP connection from {self.peer}. Waiting for handshake...")
            
            response_data = recv_handshake(self.sock, 68)
            
            if not response_data or not is_handshake(response_data):
                logger.error("Invalid or empty handshake from incoming peer.")
                return False
                
            self.supports_extensions = (response_data[25] & 0x10) != 0
            
            # Verify info_hash
            received_info_hash = response_data[28:48]
            if received_info_hash != self.torrent_info_hash:
                logger.error(f"Info hash mismatch from {self.peer}. Expected {self.torrent_info_hash.hex()}, got {received_info_hash.hex()}")
                return False
                
            logger.info(f"SUCCESS: Valid incoming handshake received from {self.peer}! Extensions supported: {self.supports_extensions}")
            
            # Send our handshake back
            # info_hash and peer_id are raw bytes already in self
            self.sock.send(pack_handshake(self.torrent_info_hash, self.torrent_peer_id))
            
            # Unchoke the peer and send bitfield
            try:
                from peer_handling import pack_message, MessageID, pack_bitfield
                self.sock.send(pack_message(MessageID.UNCHOKE))
                self.am_choking = False
                
                if self.torrent_pieces and self.torrent_pieces.piece_amount > 0:
                    bitfield = bytearray((self.torrent_pieces.piece_amount + 7) // 8)
                    has_any = False
                    for i in range(self.torrent_pieces.piece_amount):
                        if all(self.torrent_pieces.received[i]):
                            bitfield[i // 8] |= (1 << (7 - (i % 8)))
                            has_any = True
                    if has_any:
                        self.sock.send(pack_bitfield(bytes(bitfield)))
            except Exception as e:
                logger.error(f"Error sending unchoke/bitfield: {e}")
                
            return True
            
        except Exception as e:
            logger.warning(f"Failed to handshake with incoming peer {self.peer}: {e}")
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
            import select
            from peer_handling import handle_response, request_piece_block
            
            messages_processed = 0
            while messages_processed < 50:
                readable, _, _ = select.select([self.sock], [], [], 0.002)  # 2ms tick
                if not readable:
                    break
                
                messages_processed += 1
                
                response_data = recv_by_length(self.sock)
                if response_data is False or response_data is None:
                    if self.torrent_pieces is not None:
                        return False
                    else:
                        return True
                
                self.is_not_listening = False
                handle_response(self.sock, response_data, self.torrent_pieces, self, self.torrent_download_files)
            
            # Only request pieces if we don't already have them all (not seeding)
            if self.torrent_pieces is not None and not self.torrent_pieces.is_done():
                request_piece_block(self.sock, self.torrent_pieces, self)
            return True

        except Exception as e:
            if self.torrent_pieces is not None:
                return False
            return True

    def empty(self):
        return len(self.queue) == 0

    def pop(self, index=0):
        return self.queue.pop(index)

    def remove(self, block):
        return self.queue.remove(block)

    def peek(self):
        return self.queue[0]
