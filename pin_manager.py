import os
import json
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# We store the lock data in a JSON file in the same directory as this script.
LOCKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "torrent_locks.json")

# Verification string to encrypt/decrypt
VERIFICATION_STRING = b"LOCKED_TORRENT"

def load_locks():
    if not os.path.exists(LOCKS_FILE):
        return {}
    try:
        with open(LOCKS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_locks(locks):
    with open(LOCKS_FILE, 'w', encoding='utf-8') as f:
        json.dump(locks, f, indent=4)

def _derive_key(pin: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a PIN and salt using PBKDF2."""
    # PBKDF2 applies a pseudorandom function (HMAC-SHA256) multiple times to the PIN. 
    # The high iteration count (100,000) makes brute-forcing the PIN computationally expensive.
    # The salt ensures that identical PINs produce different keys, preventing rainbow table attacks.
    return hashlib.pbkdf2_hmac(
        hash_name='sha256',
        password=pin.encode('utf-8'),
        salt=salt,
        iterations=100000,
        dklen=32
    )
    # Dictionary is used to store the salt, nonce, ciphertext, and tag, than saved to locks.json

def lock_torrent(info_hash_hex: str, pin: str):
    """Encrypt the verification string with a key derived from the PIN and save the components."""
    # Salt is used for key derivation, nonce (Number used ONCE) is for AES-GCM encryption.
    # Both must be uniquely generated for each lock operation to ensure cryptographic security.
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(pin, salt)

    # AES in GCM (Galois/Counter Mode) provides Authenticated Encryption.
    # It not only encrypts the data but also generates an authentication tag 
    # to ensure the data hasn't been tampered with and the key is correct.
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(VERIFICATION_STRING) + encryptor.finalize()
    tag = encryptor.tag

    # Store the non-secret components needed for decryption alongside the ciphertext and tag
    locks = load_locks()
    locks[info_hash_hex] = {
        'salt': salt.hex(),
        'nonce': nonce.hex(),
        'ciphertext': ciphertext.hex(),
        'tag': tag.hex()
    }
    save_locks(locks)

def unlock_torrent(info_hash_hex: str, pin: str) -> bool:
    """Verify the PIN by attempting to decrypt the verification string."""
    locks = load_locks()
    lock_data = locks.get(info_hash_hex)
    if not lock_data:
        # If it's not locked, consider unlock successful (or shouldn't have been called)
        return True

    try:
        salt = bytes.fromhex(lock_data['salt'])
        nonce = bytes.fromhex(lock_data['nonce'])
        ciphertext = bytes.fromhex(lock_data['ciphertext'])
        tag = bytes.fromhex(lock_data['tag'])

        key = _derive_key(pin, salt)

        # Reconstruct the cipher with the provided tag for authentication
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        
        # This will raise an exception (specifically InvalidTag) if the derived key is wrong,
        # because the authentication tag won't match the decrypted ciphertext.
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        if plaintext == VERIFICATION_STRING:
            # Correct PIN! Remove the lock
            del locks[info_hash_hex]
            save_locks(locks)
            return True
        return False
    except Exception:
        # Invalid tag or other decryption error means wrong PIN
        return False

def is_torrent_locked(info_hash_hex: str) -> bool:
    """Check if a torrent is locked."""
    locks = load_locks()
    return info_hash_hex in locks
