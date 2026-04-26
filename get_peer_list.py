from torrent_client import TorrentClass
import binascii
import random
import socket
import struct


class TrackerClass:
    def __init__(self, tracker_ip: tuple, sock: socket, torrent_instance: TorrentClass, peer_id: bytes, logger=None, listen_port=6881):
        self.sock = sock  # Socket for communication
        self.sock.settimeout(5) # Timeout for the socket
        self.tracker_ip = tracker_ip  # The tracker IP
        self.torrent_instance = torrent_instance  # An instance of the Torrent class object
        self.connection_id = 0  # Connection id, will not be a 0 after a successful connection.
        self.peer_id = peer_id  # Random 20 bytes for the id
        self.peer_list = []
        self.listen_port = listen_port
        
        if logger is None:
            import logging
            self.logger = logging.getLogger('TrackerClass')
        else:
            self.logger = logger

    def start_communicating(self, event=0, downloaded=0, uploaded=0):
        try:
            self.logger.debug(f'Connecting to tracker: {self.tracker_ip}')
            self.sock.connect(self.tracker_ip)
        except socket.gaierror as e:
            self.logger.error(f'Tracker connection failed: {e}')
            return
        self.logger.info(f'{self.torrent_instance.name} {self.tracker_ip} Connected!')
        con_msg, transaction_id = self.create_connection_msg()
        self.sock.send(con_msg)
        self.logger.debug('Sent connection message, waiting for response...')
        
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            self.logger.error('Tracker timed out on connection')
            return
            
        tracker_con_msg = self.decode_connection_msg(data)  # Trackers own connection message
        if tracker_con_msg['action'] == 3:
            self.logger.error(f"Tracker returned error: {tracker_con_msg}")
            return
        if tracker_con_msg['transaction_id'] != transaction_id:
            self.logger.error(f"Transaction ID mismatch: sent {transaction_id}, got {tracker_con_msg['transaction_id']}")
            return
        self.logger.info(f'{self.torrent_instance.name}{self.tracker_ip} Received valid connection message: {tracker_con_msg} sending announce message...')
        ann_msg, transaction_id = self.create_announce_msg(tracker_con_msg['connection_id'],
                                                           self.torrent_instance.info_hash, self.peer_id,
                                                           self.torrent_instance.size, event, downloaded, uploaded)
        self.sock.send(ann_msg)
        self.logger.debug('Sent announce message, waiting for response...')
        
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            self.logger.error('Tracker timed out on announce')
            return
            
        tracker_ann_msg = self.decode_announce_msg(data)
        if tracker_ann_msg['action'] == 3:
            self.logger.error(f"Tracker returned error on announce: {tracker_ann_msg}")
            return
        if tracker_ann_msg['transaction_id'] != transaction_id:
            self.logger.error("Transaction ID mismatch on announce")
            return
        self.logger.info(f'{self.torrent_instance.name} {self.tracker_ip} Received valid announce message: {tracker_ann_msg}')
        self.peer_list = tracker_ann_msg['peer_list']
        return self.peer_list

    def create_connection_msg(self):
        """Create UDP tracker connection request message."""
        protocol_id = 0x41727101980  # Magic constant
        action = 0  # Connect action
        transaction_id = random.randint(0, 2**32 - 1)
        
        msg = struct.pack('!QII', protocol_id, action, transaction_id)
        return msg, transaction_id

    def decode_connection_msg(self, msg):
        """Decode UDP tracker connection response."""
        action, transaction_id, connection_id = struct.unpack('!IIQ', msg)
        return {
            'action': action,
            'transaction_id': transaction_id,
            'connection_id': connection_id
        }

    def create_announce_msg(self, connection_id, info_hash, peer_id, torrent_size, event=0, downloaded=0, uploaded=0):
        """Create UDP tracker announce request message."""
        action = 1  # Announce action
        transaction_id = random.randint(0, 2**32 - 1)
        # Ensure values are strictly integers for struct.pack
        safe_downloaded = int(downloaded) if downloaded else 0
        safe_uploaded = int(uploaded) if uploaded else 0
        safe_event = int(event) if event else 0
        safe_torrent_size = int(torrent_size) if torrent_size else 0
        
        # Use total_size if available (magnets), otherwise fallback to size (static torrents)
        total_torrent_size = getattr(self.torrent_instance, 'total_size', getattr(self.torrent_instance, 'size', 0))
        left = max(0, int(total_torrent_size) - int(downloaded))
        
        ip_address = 0  # Default
        key = random.randint(0, 2**32 - 1)
        num_want = 50
        port = self.listen_port
        
        msg = struct.pack('!QII20s20sQQQIIIiH',
                         connection_id, action, transaction_id,
                         info_hash, peer_id,
                         int(downloaded), left, int(uploaded),
                         int(event), ip_address, key, num_want, port)
        return msg, transaction_id

    def decode_announce_msg(self, msg):
        """Decode UDP tracker announce response and extract peer list."""
        action, transaction_id, interval, leechers, seeders = struct.unpack('!IIIII', msg[:20])
        
        # Parse peer list (each peer is 6 bytes: 4 for IP, 2 for port)
        peer_data = msg[20:]
        peer_list = []
        
        for i in range(0, len(peer_data), 6):
            if i + 6 <= len(peer_data):
                ip_bytes = peer_data[i:i+4]
                port_bytes = peer_data[i+4:i+6]
                
                ip = '.'.join(str(b) for b in ip_bytes)
                port = struct.unpack('!H', port_bytes)[0]
                
                peer_list.append({'ip': ip, 'port': port})
        
        return {
            'action': action,
            'transaction_id': transaction_id,
            'interval': interval,
            'leechers': leechers,
            'seeders': seeders,
            'peer_list': peer_list
        }