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

# sock is the socket from Peer class (peer_manager.py). created when peer is added and connected
# msg is the message from the peer (from communicate() in peer_manager.py)
# pieces is an instance of Pieces class in pieces_manager.py
# peer is the peer object initialized in torrent_client.py and updated in peer_manager.py
# download_files is the file handler object. is an instance of the Files class (defined in files_manager.py).

def handle_response(sock, msg, pieces, peer, download_files):
    """Routes incoming peer messages to the appropriate handler."""
    if not msg:
        return

    msg_len = len(msg)

    # Keep-alive: just a 4-byte zero length prefix
    if msg_len == ProtocolConstant.KEEP_ALIVE_LENGTH:
        return

    # If msg_len is 5, it's a single-byte message (choke, unchoke, interested, not interested, have, cancel, port)
    if msg_len == ProtocolConstant.KEEP_ALIVE_LENGTH + 1:
        # get the message ID (at index 4)
        msg_id = msg[ProtocolConstant.MESSAGE_ID_INDEX]
        if msg_id == MessageID.CHOKE:
            peer.choked = True
        elif msg_id == MessageID.UNCHOKE:
            peer.choked = False
            # if peer is unchoked, request a piece block
            request_piece_block(sock, pieces, peer)
        # if message ID is interested or not interested
        elif msg_id in (MessageID.INTERESTED, MessageID.NOT_INTERESTED):
            # update the peer's interested status
            peer.peer_interested = (msg_id == MessageID.INTERESTED)
            # if peer is interested and we are choking, send unchoke
            if peer.peer_interested and peer.am_choking:
                try:
                    sock.send(pack_message(MessageID.UNCHOKE))
                    peer.am_choking = False
                except Exception as e:
                    logger.error(f"Failed to send unchoke: {e}")

    # If msg_len is > 5, it's a multi-byte message
    elif msg_len > ProtocolConstant.KEEP_ALIVE_LENGTH + 1:
        # get the message ID (at index 4)
        msg_id = msg[ProtocolConstant.MESSAGE_ID_INDEX]
        # get the payload (from index 5 onwards)
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

# handle_have message (received when a peer has a piece)
def handle_have(sock, payload, pieces, peer):
    # extract piece index (4 bytes)
    piece_index = struct.unpack('!I', payload)[0]
    # check if the block queue was empty before adding new piece blocks
    queue_was_empty = peer.empty()
    # add piece blocks to queue
    peer.add_piece_blocks(piece_index)
    # if the block queue was empty and peer is not choked, request a piece block
    if queue_was_empty and not peer.choked:
        request_piece_block(sock, pieces, peer)


# handle_request message (received when a peer requests a block from us)
def handle_request(sock, payload, pieces, peer, download_files):
    """Peer is requesting a block from us (upload/seeding)."""
    if not download_files or not pieces:
        return
    try:
        # extract index, begin (offset), and length from payload (12 bytes)
        index, begin, length = struct.unpack('!III', payload[:12])
    except Exception:
        return

    try:
        # validate the piece index is within range
        if index < 0 or index >= len(pieces.received):
            return
        # determine the piece size
        piece_size = pieces.determine_piece_size(index)
        # validate offset (begin) and block length are within the piece boundaries
        if begin < 0 or begin >= piece_size or length <= 0 or begin + length > piece_size:
            return
        start_block = begin // BLOCK_SIZE
        end_block = (begin + length - 1) // BLOCK_SIZE
        # verify that we have successfully received/downloaded all blocks in this requested range
        for block_idx in range(start_block, end_block + 1):
            if block_idx >= len(pieces.received[index]) or not pieces.received[index][block_idx]:
                return
    except (IndexError, TypeError):
        return

    # calculate the global offset across the entire torrent
    global_offset = index * download_files.piece_size + begin
    # read the requested block data from disk
    data = download_files._read(global_offset, length)
    if data and len(data) == length:
        try:
            # pack and send the piece block back to the peer
            sock.send(pack_piece(index, begin, data))
            # update the amount of uploaded data for this peer
            peer.uploaded += length
        except Exception as e:
            logger.error(f"Failed to upload block: {e}")


# handle_bitfield message (received when a peer shares their entire piece availability bitfield)
def handle_bitfield(sock, payload, pieces, peer):
    """Parse the bitfield using fast bitwise operations."""
    # check if the block queue was empty before adding new piece blocks
    queue_was_empty = peer.empty()
    # iterate over each byte of the bitfield payload
    for i, byte in enumerate(payload):
        # check each of the 8 bits in the byte
        for j in range(8):
            # if the bit is set to 1, the peer has this piece
            if (byte >> (7 - j)) & 1:
                piece_index = i * 8 + j
                # validate the piece index is within the torrent's piece range
                if pieces and piece_index < pieces.piece_amount:
                    # add all blocks of this piece to the peer's request queue
                    peer.add_piece_blocks(piece_index)
    # if the block queue was empty and peer is not choked, request a piece block
    if queue_was_empty and not peer.choked:
        request_piece_block(sock, pieces, peer)


# handle_piece message (received when a peer sends a block of file data)
def handle_piece(sock, payload, pieces, peer, download_files):
    """Received a data block; write it and request more."""
    # unpack the piece index and begin offset from the first 8 bytes of payload
    piece_index, begin = struct.unpack('!II', payload[:ProtocolConstant.PAYLOAD_OFFSET_SIZE])
    # the rest of the payload is the raw block data
    block_data = payload[ProtocolConstant.PAYLOAD_OFFSET_SIZE:]

    # write the block data to disk
    download_files.write(piece_index, begin, block_data)
    # mark this block as received in the global piece tracker
    pieces.add_received({'piece_index': piece_index, 'begin': begin})

    # Track source peer for this piece (best-effort per-block; aggregated in GUI)
    try:
        if getattr(peer, 'client', None) and getattr(peer.client, 'record_piece_source', None):
            ip, port = peer.peer if peer.peer else (None, None)
            peer.client.record_piece_source(piece_index, ip, port)
    except Exception:
        pass

    # update bytes downloaded from this peer
    peer.downloaded += len(block_data)
    # decrement the count of pending request messages sent to this peer
    peer.pending_requests = max(0, peer.pending_requests - 1)
    
    # Remove from in-flight tracking
    import time as _time
    peer.last_action_time = _time.time()
    peer.in_flight_requests.pop((piece_index, begin), None)

    # request another block to keep the pipeline filled
    request_piece_block(sock, pieces, peer)



# request_piece_block (sends request messages to the peer to fill the request pipeline)
def request_piece_block(sock, pieces, peer):
    """
    Fills the request pipeline up to MAX_PIPELINE in-flight requests.
    In endgame mode (queue empty, download not done) re-queues all missing blocks.
    """
    if pieces is None:
        return

    # if the peer has choked us, we cannot request blocks; instead, send an INTERESTED message
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

    MAX_PIPELINE = 500
    # pop blocks from the queue and send request messages until we hit the pipeline limit
    while not peer.empty() and peer.pending_requests < MAX_PIPELINE:
        piece_block = peer.pop()
        
        # only request if the block is still needed (not received/requested elsewhere)
        if pieces.needed(piece_block):
            try:
                # send the REQUEST message to the peer
                sock.send(pack_request(piece_block['piece_index'], piece_block['begin'], piece_block['length']))
                # mark as requested in the global piece tracker
                pieces.add_request(piece_block)
                # track the request as in-flight on the peer object
                peer.in_flight_requests[(piece_block['piece_index'], piece_block['begin'])] = piece_block
                peer.pending_requests += 1
            except Exception:
                # on socket error, put the block back at the front of the queue
                peer.queue.insert(0, piece_block)
                break


# ── Network I/O ───────────────────────────────────────────────────────────────

# recv_by_length (reads a complete BitTorrent message from the socket)
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

        # unpack prefix to get the message size
        size = struct.unpack('!I', prefix)[0]
        if size == 0:
            return bytes(prefix)  # keep-alive message has length 0

        # read the remaining body of the message based on the size prefix
        body = bytearray(size)
        view = memoryview(body)
        received = 0
        while received < size:
            n = sock.recv_into(view[received:], size - received)
            if not n:
                return False
            received += n

        # return prefix and body concatenated
        return bytes(prefix) + bytes(body)
    except Exception:
        return False


# ── Message Packers ───────────────────────────────────────────────────────────

# pack_message (packs basic 1-byte message IDs like choke, unchoke, interested, etc.)
def pack_message(msg_id):
    return struct.pack('!IB', 1, msg_id)

# pack_have (packs a HAVE message for a verified piece index)
def pack_have(piece_index):
    return struct.pack('!IBI', 5, MessageID.HAVE, piece_index)

# pack_bitfield (packs a BITFIELD message representing piece availability)
def pack_bitfield(bit_field):
    length = len(bit_field) + 1
    return struct.pack(f'!IB{len(bit_field)}s', length, MessageID.BITFIELD, bit_field)

# pack_request (packs a REQUEST message for a specific block offset and size)
def pack_request(index, begin, length):
    return struct.pack('!IBIII', 13, MessageID.REQUEST, index, begin, length)

# pack_piece (packs a PIECE message containing actual file data block to upload)
def pack_piece(index, begin, block):
    length = len(block) + 9
    return struct.pack(f'!IBII{len(block)}s', length, MessageID.PIECE, index, begin, block)


# send_extended_handshake (announces extension protocol support and metadata capabilities)
def send_extended_handshake(sock):
    try:
        import bencodepy
        # encode the dictionary showing support for ut_metadata
        payload = bencodepy.encode({b'm': {b'ut_metadata': 1}})
        msg_length = 2 + len(payload)
        # pack extended message (ID 20), subcommand ID 0 for handshake
        msg = struct.pack(f'!IBB{len(payload)}s', msg_length, MessageID.EXTENDED, 0, payload)
        sock.send(msg)
    except Exception as e:
        err = str(e)
        # Quietly handle common network noise (Connection Refused, Timeout, Reset)
        if any(code in err for code in ['10061', '10054', '10060', 'timed out']):
            logger.debug(f"Connection noise from {sock.getpeername() if sock else 'unknown'}: {e}")
        else:
            logger.warning(f"Failed to send extended handshake: {e}")


# request_metadata_piece (requests a metadata piece via the extension protocol)
def request_metadata_piece(sock, ut_metadata_id, piece_index):
    try:
        import bencodepy
        # msg_type 0 represents a request
        payload = bencodepy.encode({b'msg_type': 0, b'piece': piece_index})
        msg_length = 2 + len(payload)
        # pack extended message with the negotiated ut_metadata_id
        msg = struct.pack(f'!IBB{len(payload)}s', msg_length, MessageID.EXTENDED, ut_metadata_id, payload)
        sock.send(msg)
    except Exception as e:
        logger.error(f"Failed to request metadata piece {piece_index}: {e}")


# _extract_dict_and_trailing (helper to extract the first bencoded dictionary and trailing raw bytes)
def _extract_dict_and_trailing(data_bytes):
    """Parse the first bencoded value from data_bytes, return (dict_bytes, rest)."""
    stack = 0
    i = 0
    # manually parse bencoded structures to find the boundary of the dictionary
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


# handle_extended (routes extension protocol messages)
def handle_extended(sock, payload, peer):
    # validate payload has at least extended msg id and bencoded header
    if len(payload) < 2:
        return

    import bencodepy
    extended_msg_id = payload[0]
    bencoded_data = payload[1:]

    # msg id 0 is the handshake
    if extended_msg_id == 0:  # Extended handshake
        try:
            # extract handshake dictionary
            dict_bytes, _ = _extract_dict_and_trailing(bencoded_data)
            decoded = bencodepy.decode(dict_bytes)
            # check for metadata extension support
            if b'm' in decoded and b'ut_metadata' in decoded[b'm']:
                peer.ut_metadata_id = decoded[b'm'][b'ut_metadata']
                peer.metadata_size = decoded.get(b'metadata_size', 0)

                # if we don't have the torrent metadata yet, start requesting pieces
                if peer.client and not peer.client.pieces and peer.metadata_size > 0:
                    total_pieces = (peer.metadata_size + BLOCK_SIZE - 1) // BLOCK_SIZE
                    if not hasattr(peer.client, 'metadata_buffer'):
                        peer.client.metadata_buffer = {}
                    if not hasattr(peer.client, 'metadata_requests'):
                        peer.client.metadata_requests = set()

                    import time as _time
                    # iterate and request each metadata piece
                    for piece_idx in range(total_pieces):
                        if piece_idx in peer.client.metadata_buffer:
                            continue
                        try:
                            peer.client.metadata_requests.add(piece_idx)
                            request_metadata_piece(sock, peer.ut_metadata_id, piece_idx)

                            # wait up to 10 seconds for the response
                            deadline = _time.time() + 10
                            while _time.time() < deadline:
                                raw = recv_by_length(sock)
                                if raw is False or raw is None:
                                    break
                                # if extended message is received
                                if len(raw) > 4 and raw[4] == 20:
                                    inner = raw[5:]
                                    if len(inner) >= 2 and inner[0] == 1:
                                        db, metadata_piece = _extract_dict_and_trailing(inner[1:])
                                        if db:
                                            inner_d = bencodepy.decode(db)
                                            # msg_type 1 represents data response
                                            if inner_d.get(b'msg_type') == 1:
                                                p_idx = inner_d.get(b'piece', piece_idx)
                                                peer.client.metadata_buffer[p_idx] = metadata_piece
                                                break
                                            elif inner_d.get(b'msg_type') == 2:
                                                break
                        except Exception as e:
                            logger.error(f"Metadata fetch error piece {piece_idx}: {e}")
                            break

                    # if we have all pieces of the metadata, verify and install them
                    if len(peer.client.metadata_buffer) == total_pieces:
                        if hasattr(peer.client, 'verify_and_install_metadata'):
                            peer.client.verify_and_install_metadata(peer.metadata_size)
        except Exception as e:
            logger.error(f"Extended handshake parse error: {e}")

    # handle incoming metadata piece responses from peers
    elif extended_msg_id == peer.ut_metadata_id:
        try:
            dict_bytes, metadata_piece = _extract_dict_and_trailing(bencoded_data)
            if not dict_bytes:
                return
            decoded_dict = bencodepy.decode(dict_bytes)
            msg_type = decoded_dict.get(b'msg_type')
            piece_index = decoded_dict.get(b'piece')

            # if peer returned metadata data piece (msg_type 1)
            if msg_type == 1 and piece_index is not None and peer.client:
                if not hasattr(peer.client, 'metadata_buffer'):
                    peer.client.metadata_buffer = {}
                peer.client.metadata_buffer[piece_index] = metadata_piece
                if hasattr(peer.client, 'metadata_requests'):
                    peer.client.metadata_requests.discard(piece_index)

                total_pieces = (peer.metadata_size + BLOCK_SIZE - 1) // BLOCK_SIZE
                # check if metadata is fully downloaded
                if len(peer.client.metadata_buffer) == total_pieces:
                    if hasattr(peer.client, 'verify_and_install_metadata'):
                        peer.client.verify_and_install_metadata(peer.metadata_size)
                # request the next missing metadata piece if any
                elif not peer.client.pieces:
                    for i in range(total_pieces):
                        if i not in peer.client.metadata_buffer and i not in getattr(peer.client, 'metadata_requests', set()):
                            peer.client.metadata_requests.add(i)
                            request_metadata_piece(sock, peer.ut_metadata_id, i)
                            break
        except Exception as e:
            logger.error(f"ut_metadata handling error: {e}")