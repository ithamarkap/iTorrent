import struct
import enum
import logging

logger = logging.getLogger('PeerHandling')

BLOCK_SIZE = 16384

class MessageID(enum.IntEnum):
    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE = 4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7
    CANCEL = 8
    PORT = 9
    EXTENDED = 20

class ProtocolConstant(enum.IntEnum):
    HANDSHAKE_LENGTH = 68
    KEEP_ALIVE_LENGTH = 4
    LENGTH_PREFIX_SIZE = 4
    MESSAGE_ID_INDEX = 4
    PAYLOAD_START_INDEX = 5
    PAYLOAD_OFFSET_SIZE = 8


# ── Message Router ────────────────────────────────────────────────────────────

def handle_response(sock, msg, pieces, peer, download_files):
    """Routes incoming peer messages to the appropriate handler."""
    if not msg:
        return

    msg_len = len(msg)

    # Keep-alive: just a 4-byte zero length prefix
    if msg_len == ProtocolConstant.KEEP_ALIVE_LENGTH:
        return

    if msg_len == ProtocolConstant.KEEP_ALIVE_LENGTH + 1:
        msg_id = msg[ProtocolConstant.MESSAGE_ID_INDEX]
        if msg_id == MessageID.CHOKE:
            peer.choked = True
        elif msg_id == MessageID.UNCHOKE:
            peer.choked = False
            request_piece_block(sock, pieces, peer)
        elif msg_id in (MessageID.INTERESTED, MessageID.NOT_INTERESTED):
            peer.peer_interested = (msg_id == MessageID.INTERESTED)
            if peer.peer_interested and peer.am_choking:
                try:
                    sock.send(pack_message(MessageID.UNCHOKE))
                    peer.am_choking = False
                except Exception as e:
                    logger.error(f"Failed to send unchoke: {e}")

    elif msg_len > ProtocolConstant.KEEP_ALIVE_LENGTH + 1:
        msg_id = msg[ProtocolConstant.MESSAGE_ID_INDEX]
        payload = msg[ProtocolConstant.PAYLOAD_START_INDEX:]

        if msg_id == MessageID.HAVE:
            handle_have(sock, payload, pieces, peer)
        elif msg_id == MessageID.BITFIELD:
            handle_bitfield(sock, payload, pieces, peer)
        elif msg_id == MessageID.PIECE:
            handle_piece(sock, payload, pieces, peer, download_files)
        elif msg_id == MessageID.REQUEST:
            handle_request(sock, payload, pieces, peer, download_files)
        elif msg_id == MessageID.EXTENDED:
            handle_extended(sock, payload, peer)


# ── Message Handlers ──────────────────────────────────────────────────────────

def handle_have(sock, payload, pieces, peer):
    piece_index = struct.unpack('!I', payload)[0]
    queue_was_empty = peer.empty()
    peer.add_piece_blocks(piece_index)
    if queue_was_empty and not peer.choked:
        request_piece_block(sock, pieces, peer)


def handle_request(sock, payload, pieces, peer, download_files):
    """Peer is requesting a block from us (upload/seeding)."""
    if not download_files or not pieces:
        return
    try:
        index, begin, length = struct.unpack('!III', payload[:12])
    except Exception:
        return

    try:
        if index < 0 or index >= len(pieces.received):
            return
        piece_size = pieces.determine_piece_size(index)
        if begin < 0 or begin >= piece_size or length <= 0 or begin + length > piece_size:
            return
        start_block = begin // BLOCK_SIZE
        end_block = (begin + length - 1) // BLOCK_SIZE
        for block_idx in range(start_block, end_block + 1):
            if block_idx >= len(pieces.received[index]) or not pieces.received[index][block_idx]:
                return
    except (IndexError, TypeError):
        return

    global_offset = index * download_files.piece_size + begin
    data = download_files._read(global_offset, length)
    if data and len(data) == length:
        try:
            sock.send(pack_piece(index, begin, data))
            peer.uploaded += length
        except Exception as e:
            logger.error(f"Failed to upload block: {e}")


def handle_bitfield(sock, payload, pieces, peer):
    """Parse the bitfield using fast bitwise operations."""
    queue_was_empty = peer.empty()
    for i, byte in enumerate(payload):
        for j in range(8):
            if (byte >> (7 - j)) & 1:
                piece_index = i * 8 + j
                if pieces and piece_index < pieces.piece_amount:
                    peer.add_piece_blocks(piece_index)
    if queue_was_empty and not peer.choked:
        request_piece_block(sock, pieces, peer)


def handle_piece(sock, payload, pieces, peer, download_files):
    """Received a data block; write it and request more."""
    piece_index, begin = struct.unpack('!II', payload[:ProtocolConstant.PAYLOAD_OFFSET_SIZE])
    block_data = payload[ProtocolConstant.PAYLOAD_OFFSET_SIZE:]

    download_files.write(piece_index, begin, block_data)
    pieces.add_received({'piece_index': piece_index, 'begin': begin})

    peer.downloaded += len(block_data)
    peer.pending_requests = max(0, peer.pending_requests - 1)

    request_piece_block(sock, pieces, peer)


def request_piece_block(sock, pieces, peer):
    """
    Fills the request pipeline up to MAX_PIPELINE in-flight requests.
    In endgame mode (queue empty, download not done) re-queues all missing blocks.
    """
    if pieces is None:
        return

    if peer.choked:
        try:
            sock.send(pack_message(MessageID.INTERESTED))
        except Exception:
            pass
        return

    # Endgame: refill queue with unreceived blocks ONLY from pieces this peer has
    if peer.empty() and not pieces.is_done() and pieces._unrequested_count <= 0:
        for piece_idx in peer.available_pieces:
            piece_size = pieces.determine_piece_size(piece_idx)
            for block_idx, received in enumerate(pieces.received[piece_idx]):
                if not received:
                    begin = block_idx * BLOCK_SIZE
                    length = min(BLOCK_SIZE, piece_size - begin)
                    if length > 0:
                        peer.queue.append({'piece_index': piece_idx, 'begin': begin, 'length': length})

    MAX_PIPELINE = 200
    requests_made = 0
    while not peer.empty() and peer.pending_requests < MAX_PIPELINE and requests_made < 50:
        piece_block = peer.pop()
        
        if pieces.needed(piece_block):
            try:
                sock.send(pack_request(piece_block['piece_index'], piece_block['begin'], piece_block['length']))
                pieces.add_request(piece_block)
                peer.pending_requests += 1
                requests_made += 1
            except Exception:
                peer.queue.insert(0, piece_block)
                break


# ── Network I/O ───────────────────────────────────────────────────────────────

def recv_by_length(sock):
    """Reads a full BitTorrent message: 4-byte length prefix + body."""
    try:
        # Read exactly 4 bytes for the length prefix
        prefix = bytearray(4)
        view = memoryview(prefix)
        received = 0
        while received < 4:
            n = sock.recv_into(view[received:], 4 - received)
            if not n:
                return False
            received += n

        size = struct.unpack('!I', prefix)[0]
        if size == 0:
            return bytes(prefix)  # keep-alive

        body = bytearray(size)
        view = memoryview(body)
        received = 0
        while received < size:
            n = sock.recv_into(view[received:], size - received)
            if not n:
                return False
            received += n

        return bytes(prefix) + bytes(body)
    except Exception:
        return False


# ── Message Packers ───────────────────────────────────────────────────────────

def pack_message(msg_id):
    return struct.pack('!IB', 1, msg_id)

def pack_have(piece_index):
    return struct.pack('!IBI', 5, MessageID.HAVE, piece_index)

def pack_bitfield(bit_field):
    length = len(bit_field) + 1
    return struct.pack(f'!IB{len(bit_field)}s', length, MessageID.BITFIELD, bit_field)

def pack_request(index, begin, length):
    return struct.pack('!IBIII', 13, MessageID.REQUEST, index, begin, length)

def pack_piece(index, begin, block):
    length = len(block) + 9
    return struct.pack(f'!IBII{len(block)}s', length, MessageID.PIECE, index, begin, block)


# ── Extension Protocol (BEP 9 / ut_metadata) ─────────────────────────────────

def send_extended_handshake(sock):
    try:
        import bencodepy
        payload = bencodepy.encode({b'm': {b'ut_metadata': 1}})
        msg_length = 2 + len(payload)
        msg = struct.pack(f'!IBB{len(payload)}s', msg_length, MessageID.EXTENDED, 0, payload)
        sock.send(msg)
    except Exception as e:
        logger.error(f"Failed to send extended handshake: {e}")


def request_metadata_piece(sock, ut_metadata_id, piece_index):
    try:
        import bencodepy
        payload = bencodepy.encode({b'msg_type': 0, b'piece': piece_index})
        msg_length = 2 + len(payload)
        msg = struct.pack(f'!IBB{len(payload)}s', msg_length, MessageID.EXTENDED, ut_metadata_id, payload)
        sock.send(msg)
    except Exception as e:
        logger.error(f"Failed to request metadata piece {piece_index}: {e}")


def _extract_dict_and_trailing(data_bytes):
    """Parse the first bencoded value from data_bytes, return (dict_bytes, rest)."""
    stack = 0
    i = 0
    while i < len(data_bytes):
        c = data_bytes[i:i+1]
        if c in (b'd', b'l'):
            stack += 1; i += 1
        elif c == b'i':
            i += 1
            while i < len(data_bytes) and data_bytes[i:i+1] != b'e':
                i += 1
            i += 1
        elif b'0' <= c <= b'9':
            num = b''
            while i < len(data_bytes) and b'0' <= data_bytes[i:i+1] <= b'9':
                num += data_bytes[i:i+1]; i += 1
            if i < len(data_bytes) and data_bytes[i:i+1] == b':':
                i += 1 + int(num)
        elif c == b'e':
            stack -= 1; i += 1
            if stack == 0:
                return data_bytes[:i], data_bytes[i:]
        else:
            i += 1
    return data_bytes, b''


def handle_extended(sock, payload, peer):
    if len(payload) < 2:
        return

    import bencodepy
    extended_msg_id = payload[0]
    bencoded_data = payload[1:]

    if extended_msg_id == 0:  # Extended handshake
        try:
            dict_bytes, _ = _extract_dict_and_trailing(bencoded_data)
            decoded = bencodepy.decode(dict_bytes)
            if b'm' in decoded and b'ut_metadata' in decoded[b'm']:
                peer.ut_metadata_id = decoded[b'm'][b'ut_metadata']
                peer.metadata_size = decoded.get(b'metadata_size', 0)

                if peer.client and not peer.client.pieces and peer.metadata_size > 0:
                    total_pieces = (peer.metadata_size + BLOCK_SIZE - 1) // BLOCK_SIZE
                    if not hasattr(peer.client, 'metadata_buffer'):
                        peer.client.metadata_buffer = {}
                    if not hasattr(peer.client, 'metadata_requests'):
                        peer.client.metadata_requests = set()

                    import time as _time
                    for piece_idx in range(total_pieces):
                        if piece_idx in peer.client.metadata_buffer:
                            continue
                        try:
                            peer.client.metadata_requests.add(piece_idx)
                            request_metadata_piece(sock, peer.ut_metadata_id, piece_idx)

                            deadline = _time.time() + 10
                            while _time.time() < deadline:
                                raw = recv_by_length(sock)
                                if raw is False or raw is None:
                                    break
                                if len(raw) > 4 and raw[4] == 20:
                                    inner = raw[5:]
                                    if len(inner) >= 2 and inner[0] == 1:
                                        db, metadata_piece = _extract_dict_and_trailing(inner[1:])
                                        if db:
                                            inner_d = bencodepy.decode(db)
                                            if inner_d.get(b'msg_type') == 1:
                                                p_idx = inner_d.get(b'piece', piece_idx)
                                                peer.client.metadata_buffer[p_idx] = metadata_piece
                                                break
                                            elif inner_d.get(b'msg_type') == 2:
                                                break
                        except Exception as e:
                            logger.error(f"Metadata fetch error piece {piece_idx}: {e}")
                            break

                    if len(peer.client.metadata_buffer) == total_pieces:
                        if hasattr(peer.client, 'verify_and_install_metadata'):
                            peer.client.verify_and_install_metadata(peer.metadata_size)
        except Exception as e:
            logger.error(f"Extended handshake parse error: {e}")

    elif extended_msg_id == peer.ut_metadata_id:
        try:
            dict_bytes, metadata_piece = _extract_dict_and_trailing(bencoded_data)
            if not dict_bytes:
                return
            decoded_dict = bencodepy.decode(dict_bytes)
            msg_type = decoded_dict.get(b'msg_type')
            piece_index = decoded_dict.get(b'piece')

            if msg_type == 1 and piece_index is not None and peer.client:
                if not hasattr(peer.client, 'metadata_buffer'):
                    peer.client.metadata_buffer = {}
                peer.client.metadata_buffer[piece_index] = metadata_piece
                if hasattr(peer.client, 'metadata_requests'):
                    peer.client.metadata_requests.discard(piece_index)

                total_pieces = (peer.metadata_size + BLOCK_SIZE - 1) // BLOCK_SIZE
                if len(peer.client.metadata_buffer) == total_pieces:
                    if hasattr(peer.client, 'verify_and_install_metadata'):
                        peer.client.verify_and_install_metadata(peer.metadata_size)
                elif not peer.client.pieces:
                    for i in range(total_pieces):
                        if i not in peer.client.metadata_buffer and i not in getattr(peer.client, 'metadata_requests', set()):
                            peer.client.metadata_requests.add(i)
                            request_metadata_piece(sock, peer.ut_metadata_id, i)
                            break
        except Exception as e:
            logger.error(f"ut_metadata handling error: {e}")