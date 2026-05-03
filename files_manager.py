import os
from hashlib import sha1


class Files:
    def __init__(self, piece_size, pieces_hash, files, relative_directory):
        self.piece_size = piece_size
        self.pieces_hash = pieces_hash
        self.files = files
        self.relative_directory = relative_directory
        self.file_map = []
        self._total_size = 0
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
        if not self.pieces_hash:
            return False
        expected = self.pieces_hash[piece_index * 20:(piece_index + 1) * 20]
        global_offset = piece_index * self.piece_size
        piece_len = min(self.piece_size, self._total_size - global_offset)
        return sha1(self._read(global_offset, piece_len)).digest() == expected

    def write(self, piece_index: int, byte_index: int, data: bytes):
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

    def _write_to_file(self, file_path: str, position: int, data: bytes):
        try:
            with open(file_path, 'r+b') as f:
                f.seek(position)
                f.write(data)
        except Exception:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'ab'):
                pass
            with open(file_path, 'r+b') as f:
                f.seek(position)
                f.write(data)

    def _read(self, global_offset: int, length: int) -> bytes:
        data = bytearray()
        bytes_read = 0

        for f_info in self.file_map:
            if global_offset >= f_info['end'] or global_offset + length <= f_info['start']:
                continue
            file_pos = global_offset - f_info['start']
            read_amount = min(length - bytes_read, f_info['length'] - file_pos)
            try:
                with open(f_info['path'], 'rb') as f:
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
        import shutil
        try:
            for f_info in self.file_map:
                if os.path.exists(f_info['path']):
                    os.remove(f_info['path'])
            if os.path.exists(self.relative_directory):
                shutil.rmtree(self.relative_directory, ignore_errors=True)
            return True
        except Exception as e:
            print(f"Failed to delete files: {e}")
            return False
