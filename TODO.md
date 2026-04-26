# Fix Seeding/Upload = 0 Issue

## Root Causes Identified:
1. Missing `peer_interested` and `am_choking` state tracking in Peer class
2. Wrong INTERESTED handling in peer_handling.py (calls request_piece_block instead of managing upload)
3. No periodic choke/unchoke management for seeding peers
4. Incomplete block validation in handle_request()
5. Unnecessary download requests while seeding

## Steps:
- [x] 1. Update `peer_manager.py` - Add peer_interested and am_choking flags
- [x] 2. Update `peer_handling.py` - Fix INTERESTED/NOT_INTERESTED, improve handle_request, add send_unchoke helper
- [x] 3. Update `torrent_client.py` - Add periodic choke management in _download_loop, unchoke interested peers while seeding
- [x] 4. Update `peer_manager.py` - Skip download requests when seeding
- [x] 5. Test the changes (syntax check)

