import socket
import logging
import urllib.parse
import os
import random

from torrent_client import parse_torrent_file
from get_peer_list import TrackerClass
from peer_manager import Peer

# Set up simple logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger('PeerDemo')

def parse_tracker_url(tracker_url):
    """Convert string udp://tracker.domain:port to a (ip, port) tuple."""
    parsed = urllib.parse.urlparse(tracker_url)
    if parsed.scheme != 'udp':
        raise ValueError(f"Only UDP trackers are supported in this demo. Got: {tracker_url}")
        
    hostname = parsed.hostname
    port = parsed.port or 1337
    
    ip = socket.gethostbyname(hostname)
    return (ip, port)

def main():
    # 1. Parse a known active torrent file
    logger.info("Parsing Torrent File...")
    torrent = parse_torrent_file("ubuntu-24.04.4-desktop-amd64.iso.torrent")
    
    # 1.5. Add extra public trackers
    extra_trackers = [
        "udp://tracker.opentrackr.org:1337",
        "udp://tracker.openbittorrent.com:6969",
        "udp://exodus.desync.com:6969",
        "udp://tracker.torrent.eu.org:451",
        "udp://explodie.org:6969"
    ]
    for url in extra_trackers:
        if url not in torrent.trackers:
            torrent.trackers.append(url)
    
    # Generate our own random peer ID
    my_peer_id = os.urandom(20)
    
    peer_list = []
    
    # 2. Iterate through trackers to get peers
    for tracker_url in torrent.trackers:
        if not tracker_url.startswith('udp:'):
            continue
            
        logger.info(f"Trying tracker: {tracker_url}")
        try:
            tracker_ip_port = parse_tracker_url(tracker_url)
            tracker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tracker_sock.settimeout(3)
            
            tracker = TrackerClass(tracker_ip_port, tracker_sock, torrent, my_peer_id, logger)
            peers = tracker.start_communicating()
            
            if peers:
                peer_list.extend(peers)
                logger.info(f"Got {len(peers)} peers from {tracker_url}")
        except Exception as e:
            logger.warning(f"Failed to get peers from {tracker_url}: {e}")
            
    if not peer_list:
        logger.error("Failed to get any peers from any trackers.")
        return
        
    logger.info(f"Obtained {len(peer_list)} peers total. Will try to connect to one.")
    
    # 5. Take peers and try to connect and handshake
    # We shuffle just to ensure we don't always try the same dead node first
    random.shuffle(peer_list)
    
    for peer_dict in peer_list:
        peer_addr = (peer_dict['ip'], peer_dict['port'])
        peer_obj = Peer(peer_addr, torrent.info_hash, my_peer_id)
        
        success = peer_obj.connect()
        if success:
            logger.info(f"Successfully demonstrated connection and handshake with peer {peer_addr}!")
            peer_obj.close()
            return # We only want to demonstrate ONE connection
        else:
            logger.info("Trying the next peer...")
            peer_obj.close()
            
    logger.error("Failed to connect to any peers.")

if __name__ == '__main__':
    main()
