import random
import socket
import struct
import logging


class TrackerClass:
    def __init__(self, tracker_ip: tuple, sock: socket.socket, torrent_instance,
                 peer_id: bytes, logger=None, listen_port=6881):
        self.sock = sock
        self.sock.settimeout(5)
        self.tracker_ip = tracker_ip
        self.torrent_instance = torrent_instance
        self.peer_id = peer_id
        self.listen_port = listen_port
        self.logger = logger or logging.getLogger('TrackerClass')

    def start_communicating(self, event=0, downloaded=0, uploaded=0):
        try:
            self.sock.connect(self.tracker_ip)
        except socket.gaierror as e:
            self.logger.error(f'Tracker connection failed: {e}')
            return None

        self.logger.info(f'{self.torrent_instance.name} {self.tracker_ip} Connected!')

        con_msg, tid = self._create_connection_msg()
        self.sock.send(con_msg)
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            self.logger.error('Tracker timed out on connection')
            return None

        con_resp = self._decode_connection_msg(data)
        if con_resp['action'] == 3 or con_resp['transaction_id'] != tid:
            self.logger.error(f'Connection response error: {con_resp}')
            return None

        total_size = getattr(self.torrent_instance, 'total_size',
                             getattr(self.torrent_instance, 'size', 0))
        left = max(0, int(total_size) - int(downloaded))

        ann_msg, tid2 = self._create_announce_msg(
            con_resp['connection_id'], self.torrent_instance.info_hash,
            self.peer_id, left, event, downloaded, uploaded)
        self.sock.send(ann_msg)
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            self.logger.error('Tracker timed out on announce')
            return None

        ann_resp = self._decode_announce_msg(data)
        if ann_resp['action'] == 3 or ann_resp['transaction_id'] != tid2:
            self.logger.error(f'Announce response error: {ann_resp}')
            return None

        self.logger.info(f'{self.torrent_instance.name} {self.tracker_ip} Received valid announce: {ann_resp}')
        return ann_resp['peer_list']

    def _create_connection_msg(self):
        transaction_id = random.randint(0, 2**32 - 1)
        return struct.pack('!QII', 0x41727101980, 0, transaction_id), transaction_id

    def _decode_connection_msg(self, msg):
        action, transaction_id, connection_id = struct.unpack('!IIQ', msg)
        return {'action': action, 'transaction_id': transaction_id, 'connection_id': connection_id}

    def _create_announce_msg(self, connection_id, info_hash, peer_id, left, event, downloaded, uploaded):
        transaction_id = random.randint(0, 2**32 - 1)
        msg = struct.pack('!QII20s20sQQQIIIiH',
                          connection_id, 1, transaction_id,
                          info_hash, peer_id,
                          int(downloaded), int(left), int(uploaded),
                          int(event), 0, random.randint(0, 2**32 - 1), 50, self.listen_port)
        return msg, transaction_id

    def _decode_announce_msg(self, msg):
        action, transaction_id, interval, leechers, seeders = struct.unpack('!IIIII', msg[:20])
        peer_data = msg[20:]
        peer_list = []
        for i in range(0, len(peer_data) - 5, 6):
            ip = '.'.join(str(b) for b in peer_data[i:i+4])
            port = struct.unpack('!H', peer_data[i+4:i+6])[0]
            peer_list.append({'ip': ip, 'port': port})
        return {'action': action, 'transaction_id': transaction_id,
                'interval': interval, 'leechers': leechers, 'seeders': seeders,
                'peer_list': peer_list}