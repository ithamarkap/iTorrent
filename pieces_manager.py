import logging
from torrent_client import parse_torrent_file
from peer_manager import Peer

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger('PiecesManager')

BLOCK_SIZE = 16384


class Piece:
    """Represents a single downloaded Piece, holding its expected hash and data blocks."""
    def __init__(self, piece_index, piece_size, piece_hash):
        self.piece_index = piece_index
        self.piece_size = piece_size
        self.piece_hash = piece_hash
        self.is_full = False
        self.data = bytearray(piece_size)
        
    def verify(self) -> bool:
        import hashlib
        return hashlib.sha1(self.data).digest() == self.piece_hash


class Pieces:
    def __init__(self, piece_size, piece_amount, total_size):
        self.piece_size = piece_size
        self.piece_amount = piece_amount
        self.total_size = total_size

        self.blocks_amount = 0 # Dividing the pieces into blocks of 16KB.
        self.requested = list()
        self.received = list()

        self.setup_lists_by_blocks()

    def setup_lists_by_blocks(self):
        for piece_index in range(self.piece_amount):
            number_of_blocks = self.determine_piece_size(piece_index) // BLOCK_SIZE  # Taking the integer value.
            if self.determine_piece_size(piece_index) % BLOCK_SIZE:
                number_of_blocks += 1

            self.requested.append(list())
            self.received.append(list())

            for block in range(number_of_blocks):
                self.requested[piece_index].append(False)
                self.received[piece_index].append(False)

            self.blocks_amount += number_of_blocks

    def determine_piece_size(self, piece_index):
        if piece_index == self.piece_amount - 1:
            return self.total_size % self.piece_size
        return self.piece_size

    def add_request(self, piece_block):
        idx, b_idx = piece_block['piece_index'], piece_block['begin'] // BLOCK_SIZE
        if getattr(self, 'unrequested_blocks_count', 0) == 0:
            self.unrequested_blocks_count = self.blocks_amount
        if not self.requested[idx][b_idx]:
             self.requested[idx][b_idx] = True
             self.unrequested_blocks_count -= 1

    def add_received(self, piece_block):
        self.received[piece_block['piece_index']][piece_block['begin'] // BLOCK_SIZE] = True

    def needed(self, piece_block):
        if getattr(self, 'unrequested_blocks_count', self.blocks_amount) <= 0:
            import copy
            self.requested = copy.deepcopy(self.received)
            self.unrequested_blocks_count = self.blocks_amount - sum(sum(1 for block in piece if block) for piece in self.received)
            
        return not self.requested[piece_block['piece_index']][piece_block['begin'] // BLOCK_SIZE]

    def get_progress(self):
        if self.blocks_amount == 0:
            return 0
        received_blocks = sum(sum(1 for block in piece if block) for piece in self.received)
        return (received_blocks / self.blocks_amount) * 100

    def is_done(self):
        for piece_index in self.received:
            for block in piece_index:
                if not block:
                    return False
        return True

    def check_completed_piece(self, piece_block):
        for block in self.received[piece_block['piece_index']]:
            if not block:
                return False
        logger.info(f"Received complete piece {piece_block['piece_index']}")
        return True


class PieceManager:
    """
    Manages the logic of what to download from where (which seeder has which piece),
    and keeps track of pieces for each seeder.
    """
    def __init__(self, pieces: Pieces):
        self.pieces = pieces
        # Map of seeder (IP, Port) to a list of booleans representing their pieces
        self.seeders_pieces = {}

    def add_seeder(self, seeder_addr):
        """Initializes an empty list of pieces for a new seeder."""
        if seeder_addr not in self.seeders_pieces:
            # Initially, we assume the seeder has no pieces until we receive a bitfield or HAVE messages
            self.seeders_pieces[seeder_addr] = [False] * self.pieces.piece_amount
            logger.info(f"Added seeder {seeder_addr} to PieceManager.")

    def update_seeder_bitfield(self, seeder_addr, bitfield):
        """Updates the full bitfield for a seeder (e.g., received bitfield message)."""
        if len(bitfield) == self.pieces.piece_amount:
            self.seeders_pieces[seeder_addr] = bitfield
            logger.info(f"Updated full bitfield for seeder {seeder_addr}.")
        else:
            logger.error(f"Bitfield length mismatch for {seeder_addr}. Expected {self.pieces.piece_amount}, got {len(bitfield)}")

    def update_seeder_piece(self, seeder_addr, piece_index):
        """Updates a single piece for a seeder (e.g., received HAVE message)."""
        if seeder_addr in self.seeders_pieces and piece_index < self.pieces.piece_amount:
            self.seeders_pieces[seeder_addr][piece_index] = True
            logger.debug(f"Seeder {seeder_addr} has piece {piece_index}.")

    def get_seeders_for_piece(self, piece_index):
        """Returns a list of seeders that have the requested piece."""
        available_seeders = []
        for seeder_addr, bitfield in self.seeders_pieces.items():
            if bitfield[piece_index]:
                available_seeders.append(seeder_addr)
        return available_seeders

    def get_piece_to_download(self, seeder_addr):
        """
        Determines the next piece block to download from a specific seeder.
        Usually, you would implement rare-first logic here.
        For now, returns the first needed block for a piece the seeder has.
        """
        if seeder_addr not in self.seeders_pieces:
            return None

        # Iterate over all pieces this seeder has
        for piece_index, has_piece in enumerate(self.seeders_pieces[seeder_addr]):
            if has_piece:
                # Check if we still need to request blocks for this piece
                number_of_blocks = len(self.pieces.requested[piece_index])
                for block_idx in range(number_of_blocks):
                    # Calculate actual block length (last block might be smaller)
                    begin = block_idx * BLOCK_SIZE
                    piece_size = self.pieces.determine_piece_size(piece_index)
                    length = BLOCK_SIZE if begin + BLOCK_SIZE <= piece_size else piece_size % BLOCK_SIZE
                    
                    piece_block = {
                        'piece_index': piece_index,
                        'begin': begin,
                        'length': length
                    }
                    if self.pieces.needed(piece_block):
                        return piece_block
        return None

def main():
    # 1. Parse a torrent file (like the connection example)
    logger.info("Parsing Torrent File...")
    try:
        torrent = parse_torrent_file("ubuntu-24.04.4-desktop-amd64.iso.torrent")
        piece_size = torrent.piece_length
        piece_amount = torrent.piece_amount
        total_size = torrent.size
        logger.info(f"Parsed torrent: piece_size={piece_size}, piece_amount={piece_amount}, total_size={total_size}")
    except Exception as e:
        logger.warning("ubuntu-24.04.4-desktop-amd64.iso.torrent not found. Using default mock values.")
        piece_size = 524288  # 512KB
        total_size = 1500000 # 1.43 MB
        piece_amount = 3     # 3 pieces

    # 2. Initialize a single PIECE object from the actual torrent
    logger.info("Initializing a single Piece object from the actual torrent as an example...")
    # Extract the hash for piece 0 (first 20 bytes of the pieces string)
    if hasattr(torrent, 'pieces') and len(torrent.pieces) >= 20:
        piece_0_hash = torrent.pieces[0:20]
    else:
        piece_0_hash = b'\x00' * 20
    
    first_piece = Piece(0, piece_size, piece_0_hash)
    logger.info(f"Initialized Piece 0 with hash: {piece_0_hash.hex()} and size {piece_size}")

    # Initialize the Pieces manager object
    logger.info("Initializing Pieces (All pieces manager)...")
    pieces = Pieces(piece_size, piece_amount, total_size)
    
    # 3. Managing the seeders and initializing objects for that
    logger.info("Initializing PieceManager object (Seeders Manager)...")
    manager = PieceManager(pieces)

    # Initialize seeder objects (using REAL IPs from the Tracker for the example)
    import os
    import socket
    from get_peer_list import TrackerClass
    import urllib.parse
    
    logger.info("Fetching real seeders from Tracker...")
    peer_id = os.urandom(20)
    # Using a known reliable public tracker
    tracker_url = "udp://tracker.opentrackr.org:1337"
    parsed = urllib.parse.urlparse(tracker_url)
    ip = socket.gethostbyname(parsed.hostname)
    port = parsed.port or 1337
    tracker_addr = (ip, port)
    
    tracker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tracker_sock.settimeout(3)
    
    try:
        tracker = TrackerClass(tracker_addr, tracker_sock, torrent, peer_id, logger)
        real_peers = tracker.start_communicating()
        if real_peers and len(real_peers) >= 3:
            seeder_1 = (real_peers[0]['ip'], real_peers[0]['port'])
            seeder_2 = (real_peers[1]['ip'], real_peers[1]['port'])
            seeder_3 = (real_peers[2]['ip'], real_peers[2]['port'])
        else:
            raise Exception("Tracker didn't return enough peers")
    except Exception as e:
        logger.warning(f"Failed to fetch real peers ({e}), using mock IPs.")
        seeder_1 = ("192.168.1.10", 6881)
        seeder_2 = ("192.168.1.11", 6881)
        seeder_3 = ("192.168.1.12", 6881)

    manager.add_seeder(seeder_1)
    manager.add_seeder(seeder_2)
    manager.add_seeder(seeder_3)

    # Simulate seeder 1 having piece 0 and 1 (from HAVE messages)
    manager.update_seeder_piece(seeder_1, 0)
    manager.update_seeder_piece(seeder_1, 1)

    # Simulate seeder 2 having some bitfield
    bitfield_2 = [False] * piece_amount
    if piece_amount > 2:
        bitfield_2[1] = True
        bitfield_2[2] = True
    manager.update_seeder_bitfield(seeder_2, bitfield_2)

    # Simulate seeder 3 having all pieces
    bitfield_3 = [True] * piece_amount
    manager.update_seeder_bitfield(seeder_3, bitfield_3)

    # Choose a chunk to download
    logger.info("Finding what to download and from where...")
    if piece_amount > 1:
        logger.info(f"Seeders for piece 1: {manager.get_seeders_for_piece(1)}")
    
    # Try getting a block to download from seeder 1
    next_block = manager.get_piece_to_download(seeder_1)
    if next_block:
        logger.info(f"Decided to download block from seeder {seeder_1}: {next_block}")
        # Simulate requesting it
        pieces.add_request(next_block)
        
        # Simulate receiving it
        logger.info(f"Simulating receiving block...")
        pieces.add_received(next_block)
        pieces.check_completed_piece(next_block)

    logger.info(f"Is download completely done? {pieces.is_done()}")


if __name__ == "__main__":
    main()
