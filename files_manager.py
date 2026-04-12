import os
from hashlib import sha1

class Files:
    def __init__(self, piece_size, pieces_hash, files, relative_directory):
        self.piece_size = piece_size
        self.pieces_hash = pieces_hash
        self.files = files
        self.relative_directory = relative_directory
        self.file_map = []
        self.setup_file_map()

    def setup_file_map(self):
        current_offset = 0
        for f in self.files:
            rel_path = ""
            if 'path' in f:
                if isinstance(f['path'], list):
                    rel_path = os.path.join(*[str(p) for p in f['path']])
                else:
                    rel_path = str(f['path'])
            elif 'name' in f:
                rel_path = str(f['name'])
            else:
                rel_path = "unknown_file"

            full_path = os.path.join(self.relative_directory, rel_path)
            length = f.get('length', 0)
            
            self.file_map.append({
                'start': current_offset,
                'end': current_offset + length,
                'path': full_path,
                'length': length
            })
            current_offset += length

    def create_directory(self):
        try:
            os.makedirs(self.relative_directory, exist_ok=True)
            return False
        except Exception as e:
            print("Failed to set up a directory:", e)
            return True

    def create_files(self):
        try:
            for f_info in self.file_map:
                os.makedirs(os.path.dirname(f_info['path']), exist_ok=True)
                if not os.path.exists(f_info['path']):
                    with open(f_info['path'], 'wb') as file:
                        file.truncate(f_info['length'])
            return False
        except Exception as e:
            print("Failed to create files:", e)
            return True

    def check_piece_hash(self, piece_index):
        if not self.pieces_hash:
            return False
        expected_hash = self.pieces_hash[piece_index * 20 : (piece_index + 1) * 20]
        piece_data = self.retrieve_piece_from_files(piece_index)
        return sha1(piece_data).digest() == expected_hash

    def retrieve_piece_from_files(self, piece_index):
        global_offset = piece_index * self.piece_size
        total_size = sum(f['length'] for f in self.file_map)
        piece_len = min(self.piece_size, total_size - global_offset)
        
        return self._read(global_offset, piece_len)

    def write(self, piece_index, byte_index, data):
        global_offset = piece_index * self.piece_size + byte_index
        bytes_to_write = len(data)
        current_data_offset = 0
        
        for f_info in self.file_map:
            if global_offset < f_info['end'] and global_offset + bytes_to_write > f_info['start']:
                file_pos = max(0, global_offset - f_info['start'])
                data_start = current_data_offset
                
                write_amount = min(
                    bytes_to_write - current_data_offset, 
                    f_info['length'] - file_pos
                )
                
                self._write_to_file(f_info['path'], file_pos, data[data_start:data_start + write_amount])

                current_data_offset += write_amount
                global_offset += write_amount
                
                if current_data_offset >= bytes_to_write:
                    break

    def _write_to_file(self, file_path, position, data):
        try:
            with open(file_path, 'r+b') as f:
                f.seek(position)
                f.write(data)
        except Exception:
            # Fallback in case file lacks some structure yet
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'ab') as f:
                pass
            with open(file_path, 'r+b') as f:
                f.seek(position)
                f.write(data)

    def _read(self, global_offset, length):
        bytes_read = 0
        data = bytearray()
        
        for f_info in self.file_map:
            if global_offset < f_info['end'] and global_offset + length > f_info['start']:
                file_pos = max(0, global_offset - f_info['start'])
                read_amount = min(
                    length - bytes_read,
                    f_info['length'] - file_pos
                )
                
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

    def get_total_size(self):
        return sum(f['length'] for f in self.file_map)
