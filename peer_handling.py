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
    """
    if peer.choked:
        sock.send(pack_message(MessageID.INTERESTED))
        return

    if peer.empty() and peer.pending_requests == 0:
        sock.send(pack_message(MessageID.INTERESTED))
        return

    requests_sent = 0
    while not peer.empty() and peer.pending_requests < 50 and requests_sent < 50:
        piece_block = peer.pop()
        if pieces.needed(piece_block):
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