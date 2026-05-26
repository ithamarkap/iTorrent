import socket
import threading
import logging
from peer_manager import Peer, recv_handshake, is_handshake

logger = logging.getLogger('NetworkEngine')

class NetworkEngine:
    """
    A shared network listener that multiplexes incoming BitTorrent connections 
    to multiple TorrentClient instances based on their info_hash.
    """
    def __init__(self):
        self.port = 6881
        self.server_sock = None
        self.is_running = False
        self._registry = {}  # info_hash (bytes) -> TorrentClient
        self._lock = threading.Lock()
        self._thread = None

    def register(self, info_hash, client):
        with self._lock:
            self._registry[info_hash] = client
            logger.info(f"Registered torrent {client.name} ({info_hash.hex()[:8]}...) with NetworkEngine")

    def unregister(self, info_hash):
        with self._lock:
            if info_hash in self._registry:
                del self._registry[info_hash]
                logger.debug(f"Unregistered info_hash {info_hash.hex()[:8]}...")

    def start(self, start_port=6881):
        if self.is_running:
            return self.port

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        bound_port = None
        for p in range(start_port, start_port + 10):
            try:
                self.server_sock.bind(('0.0.0.0', p))
                bound_port = p
                break
            except Exception:
                continue
        
        if bound_port is None:
            logger.error("NetworkEngine failed to bind to any port in range.")
            return None

        self.port = bound_port
        self.server_sock.listen(10)
        self.server_sock.settimeout(1.0)
        self.is_running = True
        
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="NetworkEngine")
        self._thread.start()
        
        logger.info(f"NetworkEngine listening on port {self.port}")
        return self.port

    def stop(self):
        self.is_running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def _listen_loop(self):
        while self.is_running:
            try:
                client_sock, addr = self.server_sock.accept()
                threading.Thread(target=self._handle_incoming, args=(client_sock, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.is_running:
                    logger.error(f"Listen loop error: {e}")
                break

    def _handle_incoming(self, sock, addr):
        try:
            sock.settimeout(5.0)
            # Read the initial handshake and check if it's valid + save it
            handshake = recv_handshake(sock)
            if not handshake or not is_handshake(handshake):
                sock.close()
                return

            # Extract info_hash from the handshake (first 20 bytes)
            info_hash = handshake[28:48]
            
            with self._lock:
                client = self._registry.get(info_hash)

            if not client:
                logger.debug(f"Incoming connection for unknown info_hash {info_hash.hex()[:8]} from {addr}")
                sock.close()
                return

            logger.info(f"Routing incoming connection from {addr} to torrent: {client.name}")
            
            # Create the Peer object and fill it with the torrent info from the client object
            # This way the peer doesn't need to access the client object directly
            peer_obj = Peer(addr, client.info_hash, client.peer_id,
                            pieces=client.pieces, piece_size=client.piece_size,
                            piece_amount=client.piece_amount, total_size=client.total_size,
                            download_files=client.download_files, client=client)
            
            # Pass the socket and the already-read handshake to the peer
            if peer_obj.accept_connection(sock, handshake_data=handshake):
                client.connected_peers.append(peer_obj)
            
        except Exception as e:
            logger.debug(f"Error handling incoming connection from {addr}: {e}")
            try:
                sock.close()
            except:
                pass

# Singleton instance
network_engine = NetworkEngine()
