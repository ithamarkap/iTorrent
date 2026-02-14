"""
Torrent Parser Module
Parses .torrent files and magnet links to extract metadata needed for tracker communication.
"""
import hashlib
import urllib.parse
import bencodepy


class TorrentClass:
    """Represents a parsed torrent with all necessary metadata."""
    
    def __init__(self, name: str, info_hash: bytes, size: int, announce: str, trackers: list = None):
        self.name = name
        self.info_hash = info_hash
        self.size = size
        self.announce = announce  # Primary tracker URL
        self.trackers = trackers if trackers else [announce] if announce else []


def parse_torrent_file(file_path: str) -> TorrentClass:
    """
    Parse a .torrent file and extract metadata.
    
    Args:
        file_path: Path to the .torrent file
        
    Returns:
        TorrentClass instance with parsed metadata
        
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a valid torrent
    """
    with open(file_path, 'rb') as f:
        data = bencodepy.decode(f.read())
    
    # Extract info dict and calculate info_hash
    info = data[b'info']
    info_hash = hashlib.sha1(bencodepy.encode(info)).digest()
    
    # Extract metadata
    name = info[b'name'].decode('utf-8')
    
    # Calculate total size
    if b'length' in info:
        # Single file torrent
        size = info[b'length']
    else:
        # Multi-file torrent
        size = sum(f[b'length'] for f in info[b'files'])
    
    # Get primary tracker URL
    announce = data.get(b'announce', b'').decode('utf-8')
    
    # Get all trackers from announce-list if available
    trackers = []
    if b'announce-list' in data:
        # announce-list is a list of lists of strings
        for tier in data[b'announce-list']:
            for tracker in tier:
                trackers.append(tracker.decode('utf-8'))
    
    # Ensure primary announce is in the list
    if announce and announce not in trackers:
        trackers.insert(0, announce)
    
    return TorrentClass(name, info_hash, size, announce, trackers)


def parse_magnet_link(magnet_url: str) -> TorrentClass:
    """
    Parse a magnet link and extract metadata.
    
    Args:
        magnet_url: Magnet link string
        
    Returns:
        TorrentClass instance with parsed metadata
        
    Raises:
        ValueError: If magnet link is invalid
    """
    if not magnet_url.startswith('magnet:?'):
        raise ValueError("Invalid magnet link")
    
    # Parse query parameters
    parsed = urllib.parse.urlparse(magnet_url)
    params = urllib.parse.parse_qs(parsed.query)
    
    # Extract info hash (xt parameter)
    if 'xt' not in params:
        raise ValueError("Magnet link missing xt parameter")
    
    xt = params['xt'][0]
    if not xt.startswith('urn:btih:'):
        raise ValueError("Invalid xt parameter")
    
    # Info hash can be base32 or hex
    hash_str = xt[9:]  # Remove 'urn:btih:' prefix
    
    if len(hash_str) == 40:
        # Hex encoded
        info_hash = bytes.fromhex(hash_str)
    elif len(hash_str) == 32:
        # Base32 encoded
        import base64
        info_hash = base64.b32decode(hash_str.upper())
    else:
        raise ValueError("Invalid info hash length")
    
    # Extract name (dn parameter)
    name = params.get('dn', ['Unknown'])[0]
    
    # Extract trackers (tr parameter)
    trackers = params.get('tr', [])
    announce = trackers[0] if trackers else ''
    
    # Size is unknown for magnet links
    size = 0
    
    return TorrentClass(name, info_hash, size, announce, trackers)
