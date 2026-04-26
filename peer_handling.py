import struct
import socket
import binascii
import enum
import logging

logger = logging.getLogger('PeerHandling')

'''The BitTorrent protocol uses a message format generally structured as:
<length prefix><message ID><payload>
Length prefix: 4 bytes (integer) indicating the length of the remaining message.
Message ID: 1 byte indicating the type of message (e.g., choke, unchoke, piece).
Payload: Variable length data depending on the message ID.
A connection typically starts with a Handshake, followed by exchanging Bitfields 
(which pieces each peer has), and then a continuous cycle of Choke/Unchoke/Interested 
to request and receive Pieces (actual file data).'''

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

# Message Handling
def handle_response(sock, msg, pieces, peer, download_files):
    """
    Main router for incoming messages from a peer. 
    It checks the message type and calls the appropriate specific handler.
    """
    if not msg:
        return None

    # Keep-alive messages have a length of 4 bytes (just the length prefix of 0)
    if len(msg) == ProtocolConstant.KEEP_ALIVE_LENGTH:
        logger.debug("Received Keep-alive")
        return None

    # Messages with no payload (length of 5 bytes: 4 for length prefix + 1 for message ID)
    if len(msg) == ProtocolConstant.KEEP_ALIVE_LENGTH + 1:
        msg_id = msg[ProtocolConstant.MESSAGE_ID_INDEX]
        if msg_id == MessageID.CHOKE:
            logger.debug("Received Choke - Peer stopped sharing")
            peer.choked = True
        elif msg_id == MessageID.UNCHOKE:
            logger.debug("Received Unchoke - Peer is ready to share")
            peer.choked = False
            request_piece_block(sock, pieces, peer)
        elif msg_id in (MessageID.INTERESTED, MessageID.NOT_INTERESTED):
            # Peer is updating its interest status
            logger.debug(f"Received {'Interested' if msg_id == MessageID.INTERESTED else 'Not Interested'}")
            request_piece_block(sock, pieces, peer)

    # Messages with a payload (length > 5 bytes)
    elif len(msg) > ProtocolConstant.KEEP_ALIVE_LENGTH + 1:
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
    else:
        # Invalid or unrecognized message
        sock.shutdown(socket.SHUT_RDWR)


def handle_have(sock, payload, pieces, peer):
    """
    Peer announces it has successfully downloaded and verified a piece.
    We add this piece to the peer's list of available pieces.
    """
    # Unpack the 4-byte piece index
    piece_index = struct.unpack('!I', payload)[0]
    
    queue_was_empty = peer.empty()
    peer.add_piece_blocks(piece_index)
    logger.debug(f"Peer has piece: {piece_index}")
    if queue_was_empty:
        request_piece_block(sock, pieces, peer)

def handle_request(sock, payload, pieces, peer, download_files):
    """
    Peer is requesting a block of data from us. (We are uploading/seeding).
    We should read the requested block from our file manager and send it back as a PIECE message.
    """
    if not download_files or not pieces:
        return
        
    index, begin, length = struct.unpack('!III', payload[:12])
    
    # Ensure we actually have the piece downloaded
    try:
        block_idx = begin // 16384
        if not pieces.received[index][block_idx]:
            return # Ignore request if we don't have the data yet
    except IndexError:
        pass
        
    global_offset = index * download_files.piece_size + begin
    data = download_files._read(global_offset, length)
    
    if data and len(data) == length:
        logger.debug(f"Uploading PIECE {index} block at offset {begin} (len: {length}) to {peer.peer}")
        msg = pack_piece(index, begin, data)
        try:
            sock.send(msg)
            peer.uploaded += length
        except Exception as e:
            logger.error(f"Failed to upload block: {e}")


def handle_bitfield(sock, payload, pieces, peer):
    """
    Bitfield is usually sent immediately after the handshake. 
    It tells us which pieces the peer currently has.
    Each individual bit represents a piece (1 = has it, 0 = doesn't).
    """
    logger.debug("Received Bitfield")
    queue_was_empty = peer.empty()

    # Iterate over each byte in the payload, checking its bits
    for i, byte in enumerate(payload):
        bits = '{0:08b}'.format(byte)  # Convert byte exactly to 8 bits
        for j, bit in enumerate(bits):
            if bit == '1':
                # The peer has the piece at this index
                peer.add_piece_blocks(i * 8 + j)

    if queue_was_empty:
        request_piece_block(sock, pieces, peer)


def handle_piece(sock, payload, pieces, peer, download_files):
    """
    Received a block of data (a subset of a piece).
    We need to save it to our file manager and request the next block.
    """
    # First 8 bytes of payload are piece_index (4 bytes) and begin offset (4 bytes)
    piece_index, begin = struct.unpack('!II', payload[:ProtocolConstant.PAYLOAD_OFFSET_SIZE])
    block_data = payload[ProtocolConstant.PAYLOAD_OFFSET_SIZE:]
    
    logger.debug(f"Received block for piece {piece_index} at offset {begin}")
    
    # Save the downloaded data block
    download_files.write(piece_index, begin, block_data)
    
    # Mark as received in our pieces manager
    pieces.add_received({'piece_index': piece_index, 'begin': begin})
    
    peer.pending_requests -= 1
    if peer.pending_requests < 0:
        peer.pending_requests = 0
        
    # Keep requesting blocks since we are unchoked and receiving data
    request_piece_block(sock, pieces, peer)


def request_piece_block(sock, pieces, peer):
    """
    Requests the next needed piece block from the peer.
    If we are choked, we just let the peer know we are interested.
    pieces may be None during magnet metadata fetching - in that case, do nothing.
    """
    if pieces is None:
        return  # Still fetching metadata via extension protocol, no pieces yet
    
    if peer.choked:
        sock.send(pack_message(MessageID.INTERESTED))
        return

    if peer.empty() and peer.pending_requests == 0:
        sock.send(pack_message(MessageID.INTERESTED))
        return

    requests_sent = 0
    while not peer.empty() and peer.pending_requests < 50 and requests_sent < 50:
        piece_block = peer.pop()
        if pieces and pieces.needed(piece_block):
            # Send a request for this specific pending block
            req_msg = pack_request(piece_block['piece_index'], piece_block['begin'], piece_block['length'])
            try:
                sock.send(req_msg)
                pieces.add_request(piece_block)
                peer.pending_requests += 1
                requests_sent += 1
            except Exception:
                peer.queue.insert(0, piece_block)
                break


# Network Utilities
def is_handshake(msg):
    """Checks if the received message is a valid BitTorrent handshake."""
    return len(msg) == ProtocolConstant.HANDSHAKE_LENGTH and msg[1:20] == b'BitTorrent protocol'


def recv_handshake(sock, size=ProtocolConstant.HANDSHAKE_LENGTH):
    """Receives exactly 'size' bytes from the socket. Used primarily for handshakes."""
    data = b''
    sock.settimeout(5)
    while len(data) < size:
        try:
            part = sock.recv(size - len(data))
            if not part:
                return False
            data += part
        except Exception as e:
            logger.error(f"Error receiving handshake: {e}")
            return False
    sock.settimeout(2)
    return data


def recv_by_length(sock):
    """
    Reads a standard BitTorrent message.
    First reads the 4-byte length prefix, then reads the rest of the message.
    """
    length_prefix = recv_handshake(sock, ProtocolConstant.LENGTH_PREFIX_SIZE)
    if not length_prefix or isinstance(length_prefix, bool):
        return False
        
    size = struct.unpack('!I', length_prefix)[0] # Extract integer from bytes
    data = b''
    
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            break
        data += part
        
    if len(data) < size:
        return False # Incomplete message
        
    return length_prefix + data

# Message Packing (Sending to Network)
def pack_handshake(info_hash_hex, peer_id):
    """
    Creates a handshake message.
    Format: <pstrlen><pstr><reserved><info_hash><peer_id>
    """
    pstrlen = 19
    pstr = b'BitTorrent protocol'
    reserved = 0
    info_hash = binascii.a2b_hex(info_hash_hex)
    
    # Pack format breakdown: 
    # ! = network byte order (big-endian)
    # B = unsigned char (1 byte)
    # 19s = string of 19 bytes
    # Q = unsigned long long (8 bytes)
    # 20s = 20-byte string
    pack_format = '!B 19s Q 20s 20s'.replace(' ', '')
    
    msg = struct.pack(pack_format, pstrlen, pstr, reserved, info_hash, peer_id)
    return msg


def pack_message(msg_id):
    """
    Packs a simple message with no payload (Choke, Unchoke, Interested, Not Interested).
    Format: <length=1><message ID>
    """
    pack_format = '!IB'
    return struct.pack(pack_format, 1, msg_id)


def pack_have(piece_index):
    """
    Packs a Have message to tell a peer we successfully got a piece.
    Format: <length=5><id=4><piece index>
    """
    pack_format = '!IBI'
    return struct.pack(pack_format, 5, MessageID.HAVE, piece_index)


def pack_bitfield(bit_field):
    """
    Packs a Bitfield message to share what pieces we currently have.
    Format: <length=1+len(bitfield)><id=5><bitfield data>
    """
    length = len(bit_field) + 1
    pack_format = '!IB'
    return struct.pack(pack_format, length, MessageID.BITFIELD, bit_field)


def pack_request(index, begin, length):
    """
    Packs a Request message asking for a specific block of data.
    Format: <length=13><id=6><index><begin offset><block length>
    """
    pack_format = '!IBIII'
    return struct.pack(pack_format, 13, MessageID.REQUEST, index, begin, length)


def pack_piece(index, begin, block):
    """
    Packs a Piece message containing the actual downloaded data.
    Format: <length=9+block_len><id=7><index><begin offset><block data>
    """
    length = len(block) + 9
    pack_format = '!IBII'
    return struct.pack(pack_format, length, MessageID.PIECE, index, begin, block)

def send_extended_handshake(sock):
    """
    Sends the local extended handshake indicating we want to exchange metadata.
    Format: <length><20><0><bencoded dictionary>
    """
    try:
        import bencodepy
        handshake_dict = {b'm': {b'ut_metadata': 1}}
        payload = bencodepy.encode(handshake_dict)
        msg_length = 2 + len(payload)
        pack_format = f'!IBB{len(payload)}s'
        msg = struct.pack(pack_format, msg_length, MessageID.EXTENDED, 0, payload)
        sock.send(msg)
        logger.debug("Sent Extended Handshake successfully.")
    except Exception as e:
        logger.error(f"Failed to send extended handshake: {e}")

def request_metadata_piece(sock, ut_metadata_id, piece_index):
    try:
        import bencodepy
        request_dict = {b'msg_type': 0, b'piece': piece_index}
        payload = bencodepy.encode(request_dict)
        msg_length = 2 + len(payload)
        pack_format = f'!IBB{len(payload)}s'
        msg = struct.pack(pack_format, msg_length, MessageID.EXTENDED, ut_metadata_id, payload)
        sock.send(msg)
        logger.debug(f"Requested metadata piece {piece_index}.")
    except Exception as e:
        logger.error(f"Failed to request metadata piece: {e}")
        
def extract_dict_and_trailing(data_bytes):
    stack = 0
    i = 0
    while i < len(data_bytes):
        c = data_bytes[i:i+1]
        if c == b'd' or c == b'l':
            stack += 1
            i += 1
        elif c == b'i':
            i += 1
            while i < len(data_bytes) and data_bytes[i:i+1] != b'e':
                i += 1
            i += 1
        elif b'0' <= c <= b'9':
            num = b''
            while i < len(data_bytes) and b'0' <= data_bytes[i:i+1] <= b'9':
                num += data_bytes[i:i+1]
                i += 1
            if i < len(data_bytes) and data_bytes[i:i+1] == b':':
                str_len = int(num)
                i += 1 + str_len
        elif c == b'e':
            stack -= 1
            i += 1
            if stack == 0:
                return data_bytes[:i], data_bytes[i:]
        else:
            i += 1
    return data_bytes, b''

def handle_extended(sock, payload, peer):
    """
    Handles incoming extended messages.
    """
    if len(payload) < 2:
        return
    
    import bencodepy
    extended_msg_id = payload[0]
    bencoded_data_bytes = payload[1:]
    
    if extended_msg_id == 0:  # Extended Handshake
        try:
            dict_bytes, _ = extract_dict_and_trailing(bencoded_data_bytes)
            decoded = bencodepy.decode(dict_bytes)
            if b'm' in decoded and b'ut_metadata' in decoded[b'm']:
                peer.ut_metadata_id = decoded[b'm'][b'ut_metadata']
                peer.metadata_size = decoded.get(b'metadata_size', 0)
                print(f"METADATA: Received info from {peer.peer}. Size: {peer.metadata_size}, ut_id: {peer.ut_metadata_id}")
                
                has_client = peer.client is not None
                if peer.client and not peer.client.pieces and peer.metadata_size > 0:
                    total_pieces = (peer.metadata_size + 16383) // 16384
                    peer.client.status = "Fetching Metadata"
                    if not hasattr(peer.client, 'metadata_buffer'):
                        peer.client.metadata_buffer = {}
                    if not hasattr(peer.client, 'metadata_requests'):
                        peer.client.metadata_requests = set()
                    
                    print(f"METADATA: Starting sync fetch. Total pieces: {total_pieces}, Buffer: {len(peer.client.metadata_buffer)}")
                    import time as _time
                    for piece_idx in range(total_pieces):
                        if piece_idx in peer.client.metadata_buffer:
                            continue
                        try:
                            peer.client.metadata_requests.add(piece_idx)
                            print(f"METADATA: Requesting piece {piece_idx} from {peer.peer}")
                            request_metadata_piece(sock, peer.ut_metadata_id, piece_idx)
                            
                            # Blocking read - wait up to 10s for the reply
                            deadline = _time.time() + 10
                            while _time.time() < deadline:
                                raw = recv_by_length(sock)
                                if raw is False or raw is None:
                                    break
                                    
                                msg_id_byte = raw[4] if len(raw) > 4 else -1
                                if msg_id_byte == 20:  # EXTENDED message
                                    inner_payload = raw[5:]
                                    if len(inner_payload) < 2:
                                        continue
                                    inner_id = inner_payload[0]
                                    # The peer sends response using OUR declared ut_metadata ID (1)
                                    if inner_id == 1:
                                        dict_bytes, metadata_piece = extract_dict_and_trailing(inner_payload[1:])
                                        if dict_bytes:
                                            inner_decoded = bencodepy.decode(dict_bytes)
                                            if inner_decoded.get(b'msg_type') == 1:
                                                p_idx = inner_decoded.get(b'piece', piece_idx)
                                                peer.client.metadata_buffer[p_idx] = metadata_piece
                                                print(f"METADATA: Received piece {p_idx} ({len(metadata_piece)} bytes)")
                                                break
                                            elif inner_decoded.get(b'msg_type') == 2:
                                                print(f"METADATA: Piece {piece_idx} REJECTED by peer")
                                                break
                                    elif inner_id == 0:
                                        # Another extended handshake, extract their metadata ID if updated
                                        try:
                                            hdict_b, _ = extract_dict_and_trailing(inner_payload[1:])
                                            hd = bencodepy.decode(hdict_b)
                                            if b'm' in hd and b'ut_metadata' in hd[b'm']:
                                                peer.ut_metadata_id = hd[b'm'][b'ut_metadata']
                                        except Exception:
                                            pass
                                # Ignore other message types while waiting
                        except Exception as e:
                            print(f"METADATA: Error fetching piece {piece_idx}: {e}")
                            break
                    
                    # Check if we now have all pieces
                    if len(peer.client.metadata_buffer) == total_pieces:
                        if hasattr(peer.client, 'verify_and_install_metadata'):
                            peer.client.verify_and_install_metadata(peer.metadata_size)
        except Exception as e:
            print(f"METADATA: Error parsing Extended Handshake: {e}")
            
    elif extended_msg_id == peer.ut_metadata_id:  # ut_metadata message
        try:
            dict_bytes, metadata_piece = extract_dict_and_trailing(bencoded_data_bytes)
            if not dict_bytes:
                return
            decoded_dict = bencodepy.decode(dict_bytes)
            
            msg_type = decoded_dict.get(b'msg_type')
            piece_index = decoded_dict.get(b'piece')
            
            if msg_type == 1 and piece_index is not None and peer.client:
                print(f"METADATA: Received piece {piece_index} from {peer.peer}")
                if not hasattr(peer.client, 'metadata_buffer'):
                    peer.client.metadata_buffer = {}
                peer.client.metadata_buffer[piece_index] = metadata_piece
                
                if hasattr(peer.client, 'metadata_requests') and piece_index in peer.client.metadata_requests:
                    peer.client.metadata_requests.remove(piece_index)
                    
                total_pieces = (peer.metadata_size + 16383) // 16384
                if len(peer.client.metadata_buffer) == total_pieces:
                    # We have all pieces
                    if hasattr(peer.client, 'verify_and_install_metadata'):
                        peer.client.verify_and_install_metadata(peer.metadata_size)
                        
                # Continue requesting if there are more
                if not peer.client.pieces:
                    for i in range(total_pieces):
                        if i not in peer.client.metadata_buffer and i not in getattr(peer.client, 'metadata_requests', set()):
                            peer.client.metadata_requests.add(i)
                            print(f"METADATA: Requesting NEXT piece {i} from {peer.peer}")
                            request_metadata_piece(sock, peer.ut_metadata_id, i)
                            break
        except Exception as e:
            print(f"METADATA: Error handling ut_metadata: {e}")