import logging

logger = logging.getLogger('PiecesManager')

BLOCK_SIZE = 16384


class Pieces:
    def __init__(self, piece_size, piece_amount, total_size):
        self.piece_size = piece_size
        self.piece_amount = piece_amount
        self.total_size = total_size

        self.blocks_amount = 0
        self.requested = []
        self.received = []
        self._received_count = 0  # Cached count for O(1) progress/done checks

        self._setup_lists_by_blocks()

    def _setup_lists_by_blocks(self):
        for piece_index in range(self.piece_amount):
            piece_sz = self.determine_piece_size(piece_index)
            number_of_blocks = (piece_sz + BLOCK_SIZE - 1) // BLOCK_SIZE

            self.requested.append([False] * number_of_blocks)
            self.received.append([False] * number_of_blocks)
            self.blocks_amount += number_of_blocks

    def determine_piece_size(self, piece_index):
        if piece_index == self.piece_amount - 1:
            last = self.total_size % self.piece_size
            return last if last != 0 else self.piece_size
        return self.piece_size

    def add_request(self, piece_block):
        idx = piece_block['piece_index']
        b_idx = piece_block['begin'] // BLOCK_SIZE
        if not self.requested[idx][b_idx]:
            self.requested[idx][b_idx] = True

    def add_received(self, piece_block):
        idx = piece_block['piece_index']
        b_idx = piece_block['begin'] // BLOCK_SIZE
        if not self.received[idx][b_idx]:
            self.received[idx][b_idx] = True
            self._received_count += 1

    def needed(self, piece_block):
        idx = piece_block['piece_index']
        b_idx = piece_block['begin'] // BLOCK_SIZE

        # Endgame: if all blocks have been requested, reset requested to match received
        if all(self.requested[i][j] for i in range(self.piece_amount) for j in range(len(self.requested[i]))):
            for i in range(self.piece_amount):
                self.requested[i] = self.received[i][:]

        return not self.requested[idx][b_idx]

    def get_progress(self):
        if self.blocks_amount == 0:
            return 0
        return (self._received_count / self.blocks_amount) * 100

    def is_done(self):
        return self._received_count >= self.blocks_amount

    def check_completed_piece(self, piece_block):
        return all(self.received[piece_block['piece_index']])


class PieceManager:
    """
    Tracks which seeders have which pieces.
    """
    def __init__(self, pieces: Pieces):
        self.pieces = pieces
        self.seeders_pieces = {}

    def add_seeder(self, seeder_addr):
        if seeder_addr not in self.seeders_pieces:
            self.seeders_pieces[seeder_addr] = [False] * self.pieces.piece_amount
            logger.info(f"Added seeder {seeder_addr} to PieceManager.")

    def update_seeder_bitfield(self, seeder_addr, bitfield):
        if len(bitfield) == self.pieces.piece_amount:
            self.seeders_pieces[seeder_addr] = bitfield
        else:
            logger.error(f"Bitfield length mismatch for {seeder_addr}.")

    def update_seeder_piece(self, seeder_addr, piece_index):
        if seeder_addr in self.seeders_pieces and piece_index < self.pieces.piece_amount:
            self.seeders_pieces[seeder_addr][piece_index] = True
