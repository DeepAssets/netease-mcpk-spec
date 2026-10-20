"""Regression tests using generated containers; no game assets required."""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import PackError, inspect_pack, repack, unpack
from name_hash import path_key


def sample(items=None, contents_index=0, prefix=65):
    if items is None:
        items = {'manifest.json': b'{"version":1}', 'data/config.json': b'{}',
                 'textures/example.bin': bytes(range(256)) * 3}
    contents = json.dumps({'content': [{'path': name} for name in items]}).encode()
    names = list(items)
    names.insert(contents_index, 'contents.json')
    payloads = [contents if name == 'contents.json' else items[name] for name in names]
    header = bytearray(prefix)
    header[:4] = b'MCPK'
    data_offset = prefix + 16 * len(names)
    struct.pack_into('<I', header, 20, data_offset)
    table = bytearray()
    encoded = bytearray()
    for name, payload in zip(names, payloads):
        compressed = zlib.compress(payload)
        table.extend(struct.pack('<II', path_key(name), len(encoded)))
        table.extend(len(compressed).to_bytes(3, 'little'))
        table.append(0)
        table.extend(len(payload).to_bytes(3, 'little'))
        table.append(0)
        encoded.extend(compressed)
    return bytes(header + table + encoded + b'\x01' + bytes(128))


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.write_bytes(sample())
        self.project = self.root / 'project'

    def tearDown(self):
        self.temporary.cleanup()

    def extract(self):
        return unpack(self.source, self.project)

    def test_known_file_keys(self):
        self.assertEqual(path_key('manifest.json'), 0x4C71DE25)
        self.assertEqual(path_key('contents.json'), 0x33340C73)
        self.assertEqual(path_key('any/path/manifest.json'), 0x4C71DE25)

    def test_contents_positions_and_table_alignment(self):
        for contents_index in (0, 1, 3):
            for prefix in (65, 69, 77):
                with self.subTest(index=contents_index, prefix=prefix):
                    pack = inspect_pack(sample(contents_index=contents_index, prefix=prefix))
                    self.assertEqual(len(pack.streams), 4)
                    self.assertEqual(pack.contents_index, contents_index)

    def test_no_change_is_byte_identical(self):
        self.extract()
        output = self.root / 'rebuilt'
        repack(self.project, output)
        self.assertEqual(output.read_bytes(), self.source.read_bytes())

    def test_changed_file_and_later_offsets(self):
        self.extract()
        expected = b'changed payload\x00' * 3000
        (self.project / 'manifest.json').write_bytes(expected)
        output = self.root / 'rebuilt'
        repack(self.project, output)
        second = self.root / 'second'
        unpack(output, second)
        self.assertEqual((second / 'manifest.json').read_bytes(), expected)
        self.assertEqual((second / 'textures/example.bin').read_bytes(), bytes(range(256)) * 3)

    def test_added_file_rejected(self):
        self.extract()
        (self.project / 'extra.txt').write_bytes(b'new')
        with self.assertRaises(PackError):
            repack(self.project, self.root / 'rejected')
        self.assertFalse((self.root / 'rejected').exists())

    def test_removed_file_rejected(self):
        self.extract()
        (self.project / 'manifest.json').unlink()
        with self.assertRaises(PackError):
            repack(self.project, self.root / 'rejected')

    def test_renamed_file_rejected(self):
        self.extract()
        (self.project / 'manifest.json').rename(self.project / 'other.json')
        with self.assertRaises(PackError):
            repack(self.project, self.root / 'rejected')

    def test_contents_bytes_cannot_change(self):
        self.extract()
        with (self.project / 'contents.json').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaises(PackError):
            repack(self.project, self.root / 'rejected')

    def test_modified_template_rejected(self):
        self.extract()
        original = self.project / '.mcpk_tool/original.mcpk'
        body = bytearray(original.read_bytes())
        body[-1] ^= 1
        original.write_bytes(body)
        with self.assertRaises(PackError):
            repack(self.project, self.root / 'rejected')

    def test_unsafe_paths_rejected(self):
        for path in ('../escape', '/absolute', 'CON.txt', 'name:stream', 'A/../../x', '.', 'trailing.'):
            with self.subTest(path=path):
                with self.assertRaises(PackError):
                    inspect_pack(sample({path: b'x'}))

    def test_checksum_corruption_rejected(self):
        body = bytearray(self.source.read_bytes())
        first = inspect_pack(body).streams[0]
        body[first.offset + first.compressed - 1] ^= 1
        with self.assertRaises(PackError):
            inspect_pack(body)

    def test_existing_output_is_preserved(self):
        self.extract()
        output = self.root / 'existing'
        output.write_bytes(b'keep this')
        with self.assertRaises(PackError):
            repack(self.project, output)
        self.assertEqual(output.read_bytes(), b'keep this')


if __name__ == '__main__':
    unittest.main()
