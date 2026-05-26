import os
from hashlib import sha1
import threading

class Files:
    # The files parameter is a list of dictionaries that contain the file information.
    # Each dictionary has the following keys:
    #   - 'path': The path to the file.
    #   - 'length': The length of the file in bytes.
    # The relative_directory parameter is the path to the directory where the files should be stored.
    # The relative_directory comes from torrent_client's _setup_download_files() method.
    
    def __init__(self, piece_size, pieces_hash, files, relative_directory):
        self.piece_size = piece_size
        self.pieces_hash = pieces_hash
        self.files = files
        self.relative_directory = relative_directory
        self.file_map = []
        self._total_size = 0
        self._lock = threading.Lock()
        self._file_handles = {}
        self._setup_file_map()

    def _setup_file_map(self):
        current_offset = 0
        for f in self.files:
            if 'path' in f:
                rel_path = os.path.join(*[str(p) for p in f['path']]) if isinstance(f['path'], list) else str(f['path'])
            elif 'name' in f:
                rel_path = str(f['name'])
            else:
                rel_path = 'unknown_file'

            full_path = os.path.join(self.relative_directory, rel_path)
            length = f.get('length', 0)
            self.file_map.append({'start': current_offset, 'end': current_offset + length,
                                   'path': full_path, 'length': length})
            current_offset += length

        self._total_size = current_offset

    def create_directory(self):
        try:
            os.makedirs(self.relative_directory, exist_ok=True)
            return False
        except Exception as e:
            print("Failed to set up directory:", e)
            return True

    def create_files(self):
        try:
            for f_info in self.file_map:
                os.makedirs(os.path.dirname(f_info['path']), exist_ok=True)
                if not os.path.exists(f_info['path']):
                    with open(f_info['path'], 'wb') as fh:
                        fh.truncate(f_info['length'])
            return False
        except Exception as e:
            print("Failed to create files:", e)
            return True

    def check_piece_hash(self, piece_index: int) -> bool:
        '''
        Extracts the expected SHA-1 hash of the piece (20 bytes long) from the 
        pieces_hash list, calculates the global offset and the actual length of the piece (to handle cases where the last piece is shorter),
        reads the piece data from the disk using the private _read function, and returns True if the SHA-1 hash of the read data matches the expected hash,
        or False if they do not match.
        '''
        if not self.pieces_hash:
            return False
        
        # 20 bytes per piece hash
        expected = self.pieces_hash[piece_index * 20:(piece_index + 1) * 20]
        # get the global offset and piece length
        global_offset = piece_index * self.piece_size
        piece_len = min(self.piece_size, self._total_size - global_offset)
        # read the piece data and compare its hash with the expected hash
        return sha1(self._read(global_offset, piece_len)).digest() == expected

    def write(self, piece_index: int, byte_index: int, data: bytes):
        """
        Receives access to the Files class and the parameters: piece_index - integer, byte_index - integer, and data in bytes.
        Writes data to the appropriate disk locations based on the piece index and its offset.
        It calculates the global offset in the torrent address space and searches in file_map
        for files whose range overlaps with the new data. For each matching file, it calculates
        the internal position and the amount of data to be written, and performs the actual write
        using _write_to_file.
        """
        # global_offset is how far into the entire torrent file the current data starts
        global_offset = piece_index * self.piece_size + byte_index
        bytes_to_write = len(data)
        data_offset = 0

        for f_info in self.file_map:
            if global_offset >= f_info['end'] or global_offset + bytes_to_write <= f_info['start']:
                continue
            file_pos = global_offset - f_info['start']
            write_amount = min(bytes_to_write - data_offset, f_info['length'] - file_pos)
            self._write_to_file(f_info['path'], file_pos, data[data_offset:data_offset + write_amount])
            data_offset += write_amount
            global_offset += write_amount
            if data_offset >= bytes_to_write:
                break

    def _get_file_handle(self, file_path):
        """
        Receives access to the Files class and the parameters: file_path - string representing the file path.
        Manages and returns an open file handle for the requested file. The function saves the open
        handles in the self._file_handles dictionary to avoid repeatedly opening and closing files.
        If the file does not exist or opening it fails, it creates the file and opens it in binary
        read-write ('r+b') mode.
        """
        if file_path not in self._file_handles:
            try:
                self._file_handles[file_path] = open(file_path, 'r+b')
            except Exception:
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, 'ab'):
                    pass
                self._file_handles[file_path] = open(file_path, 'r+b')
        return self._file_handles[file_path]

    def _write_to_file(self, file_path: str, position: int, data: bytes):
        """
        Receives access to the Files class and the parameters: file_path - string representing the file path, position - integer representing the position in the file, and data in bytes.
        Writes data to a specific file at a defined position on the disk using a locking mechanism
        (self._lock) to prevent conflicts between threads. It gets the file handle using
        _get_file_handle, moves the write cursor to the requested position (seek), and writes the data.
        """
        with self._lock:
            f = self._get_file_handle(file_path)
            f.seek(position)
            f.write(data)

    def _read(self, global_offset: int, length: int) -> bytes:
        """
        Receives access to the Files class and the parameters: global_offset - integer representing the global offset, and length - integer representing the length of the data to read in bytes.
        Reads a specific amount of bytes from the global offset of the torrent, crossing file boundaries
        if necessary. It iterates over file_map and locates the files belonging to the requested read range.
        For each file, it safely accesses the file handle under locking, reads the relevant data, and appends
        it to a single bytearray. In case of a read failure, it fills the missing bytes with null bytes (\x00).
        """
        data = bytearray()
        bytes_read = 0

        for f_info in self.file_map:
            if global_offset >= f_info['end'] or global_offset + length <= f_info['start']:
                continue
            file_pos = global_offset - f_info['start']
            read_amount = min(length - bytes_read, f_info['length'] - file_pos)
            
            with self._lock:
                try:
                    f = self._get_file_handle(f_info['path'])
                    f.seek(file_pos)
                    data.extend(f.read(read_amount))
                except Exception:
                    data.extend(b'\x00' * read_amount)
            
            bytes_read += read_amount
            global_offset += read_amount
            if bytes_read >= length:
                break

        return bytes(data)

    def delete_files(self) -> bool:
        """
        Receives access to the Files class (no additional parameters).
        Deletes all torrent files and the download directory from the disk. First, it safely closes all
        open file handles and clears self._file_handles. Then, it physically deletes each file listed in
        file_map, and finally deletes the main torrent download directory using shutil.rmtree.
        Returns True on success and False on failure.
        """
        import shutil
        try:
            with self._lock:
                for file_path, f in self._file_handles.items():
                    try:
                        f.close()
                    except Exception:
                        pass
                self._file_handles.clear()

            for f_info in self.file_map:
                if os.path.exists(f_info['path']):
                    os.remove(f_info['path'])
            if os.path.exists(self.relative_directory):
                shutil.rmtree(self.relative_directory, ignore_errors=True)
            return True
        except Exception as e:
            print(f"Failed to delete files: {e}")
            return False