"""MCPK extraction and original-structure repacking. Author: nmbb."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import hashlib
import json
import mmap
import os
import shutil
import struct
import tempfile
import time
import zlib

from name_hash import path_key

VERSION = '1.1.1'
AUTHOR = 'nmbb'
META = '.mcpk_tool'
CHUNK = 1024 * 1024
MAX_STREAM = 256 * 1024 * 1024
MAX_CONTENTS = 64 * 1024 * 1024
MAX_FILES = 250000
MAX_TABLE = 64 * 1024 * 1024
NO_LOG = lambda message: None
NO_PROGRESS = lambda fraction: None


class PackError(Exception):
    """An actionable error safe to show in the GUI."""


@dataclass
class Stream:
    offset: int
    compressed: int
    decoded: int
    sha256: str
    prefix: bytes
    name: str = ''
    key_offset: int = -1


@dataclass
class Pack:
    data_offset: int
    streams: list[Stream]
    footer_offset: int
    contents_index: int
    source_sha256: str
    size: int


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        while block := file.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _hash_buffer(data) -> str:
    digest = hashlib.sha256()
    for start in range(0, len(data), CHUNK):
        digest.update(data[start:start + CHUNK])
    return digest.hexdigest()


@contextmanager
def _open_pack(path: Path):
    if not path.is_file():
        raise PackError(f'找不到封包文件：{path}')
    with path.open('rb') as file:
        if os.fstat(file.fileno()).st_size < 64:
            raise PackError('文件太小，不是支持的 MCPK 封包。')
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 4096:
        raise PackError('封包包含无效文件名。')
    path = PurePosixPath(name)
    forbidden = '<>:"\\|?*'
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)}
    if not path.parts or path.is_absolute() or name != path.as_posix():
        raise PackError(f'封包包含不安全路径：{name!r}')
    for part in path.parts:
        if (part in ('.', '..') or part.rstrip(' .') != part
                or any(character in forbidden or ord(character) < 32 for character in part)
                or part.split('.')[0].upper() in reserved):
            raise PackError(f'封包包含 Windows 无法安全提取的路径：{name!r}')
    if path.parts[0].casefold() == META:
        raise PackError(f'封包路径与工具元数据目录冲突：{name}')
    return name


def _inflate(data, offset: int, sink=None):
    """Stream a zlib member, checking its checksum and bounding decoded size."""
    decoder = zlib.decompressobj()
    cursor = offset
    decoded = 0
    digest = hashlib.sha256()
    prefix = bytearray()
    try:
        while not decoder.eof:
            if cursor >= len(data):
                raise PackError(f'压缩流不完整，位置 0x{offset:X}。')
            block = data[cursor:min(cursor + CHUNK, len(data))]
            cursor += len(block)
            pending = block
            while pending and not decoder.eof:
                raw = decoder.decompress(pending, CHUNK)
                decoded += len(raw)
                if decoded > MAX_STREAM:
                    raise PackError('单个文件解压超过 256 MiB 安全上限，已停止。')
                digest.update(raw)
                if len(prefix) < 256:
                    prefix.extend(raw[:256 - len(prefix)])
                if sink is not None:
                    sink(raw)
                pending = decoder.unconsumed_tail
        end = cursor - len(decoder.unused_data)
    except zlib.error as error:
        raise PackError(f'zlib 解压或校验失败，位置 0x{offset:X}：{error}') from error
    if end <= offset:
        raise PackError('无效压缩流长度。')
    return Stream(offset, end - offset, decoded, digest.hexdigest(), bytes(prefix))


def _stream_bytes(data, stream: Stream) -> bytes:
    if stream.decoded > MAX_CONTENTS:
        raise PackError('contents.json 过大，无法安全解析。')
    chunks = []
    actual = _inflate(data, stream.offset, chunks.append)
    if (actual.compressed, actual.sha256) != (stream.compressed, stream.sha256):
        raise PackError('读取过程中源包发生变化。')
    return b''.join(chunks)


def inspect_pack(data, log=NO_LOG, progress=NO_PROGRESS) -> Pack:
    if data[:4] != b'MCPK':
        raise PackError('文件头不是 MCPK。本工具不支持 ZIP、BRARCHIVE 或加密资源包。')
    data_offset = struct.unpack_from('<I', data, 0x14)[0]
    if not 64 <= data_offset <= min(MAX_TABLE, len(data) - 2):
        raise PackError('MCPK 数据区偏移无效，或表区超出支持范围。')
    streams = []
    cursor = data_offset
    last_update = time.monotonic()
    while cursor + 2 <= len(data):
        if data[cursor] & 15 != 8 or (data[cursor] * 256 + data[cursor + 1]) % 31:
            break
        stream = _inflate(data, cursor)
        streams.append(stream)
        if len(streams) > MAX_FILES:
            raise PackError('文件数量超过工具上限。')
        cursor += stream.compressed
        if time.monotonic() - last_update > 0.6:
            progress(0.75 * cursor / len(data))
            log(f'扫描压缩数据：{len(streams):,} 个文件')
            last_update = time.monotonic()
    if not streams or len(data) - cursor != 129:
        raise PackError(f'不支持的 MCPK 布局：识别 {len(streams)} 个流，尾部 {len(data)-cursor} 字节；需要 129 字节尾部。')

    candidates = []
    for index, stream in enumerate(streams):
        if stream.decoded > MAX_CONTENTS or not stream.prefix.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'{'):
            continue
        try:
            document = json.loads(_stream_bytes(data, stream))
        except (ValueError, UnicodeError):
            continue
        if isinstance(document, dict) and isinstance(document.get('content'), list):
            items = document['content']
            if all(isinstance(item, dict) and isinstance(item.get('path'), str) for item in items):
                candidates.append((index, [item['path'] for item in items]))
    candidates = [(index, paths) for index, paths in candidates if len(paths) + 1 == len(streams)]
    if len(candidates) != 1:
        raise PackError('未找到唯一、完整的 contents.json 清单，无法可靠恢复文件名。')
    contents_index, names = candidates[0]
    names.insert(contents_index, 'contents.json')
    seen = set()
    for stream, name in zip(streams, names):
        stream.name = _safe_name(name)
        if name.casefold() in seen:
            raise PackError(f'封包包含重复路径：{name}')
        seen.add(name.casefold())
    # Reject file/directory collisions before any extraction is attempted.
    for name in names:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix().casefold() in seen:
                raise PackError(f'封包存在文件与目录重名：{name}')

    by_relative = {stream.offset - data_offset: stream for stream in streams}
    expected_keys = {stream.offset - data_offset: path_key(stream.name) for stream in streams}
    # The documented direct and chained rows share this exact 15-byte window:
    # key:u32, relative:u32, compressed:u24, pad:u8, decoded:u24.
    for key_position in range(64, data_offset - 14):
        relative = struct.unpack_from('<I', data, key_position + 4)[0]
        stream = by_relative.get(relative)
        if stream is None:
            continue
        compressed = int.from_bytes(data[key_position + 8:key_position + 11], 'little')
        decoded = int.from_bytes(data[key_position + 12:key_position + 15], 'little')
        if (compressed, decoded) != (stream.compressed, stream.decoded):
            continue
        if struct.unpack_from('<I', data, key_position)[0] != expected_keys[relative]:
            continue
        if stream.key_offset != -1:
            raise PackError(f'文件索引重复匹配，已停止：{stream.name}')
        stream.key_offset = key_position
    missing = [stream.name for stream in streams if stream.key_offset < 0]
    if missing:
        raise PackError(f'{len(missing)} 个文件无法通过索引及文件名哈希核对，例如：{missing[0]}')
    progress(0.95)
    digest = _hash_buffer(data)
    progress(1.0)
    log(f'已核对 {len(streams):,} 个文件的 zlib 校验、索引和文件名哈希。')
    return Pack(data_offset, streams, cursor, contents_index, digest, len(data))


def _json_write(path: Path, value):
    with path.open('x', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)


def _remove_stage(stage: Path, parent: Path):
    resolved = stage.resolve()
    if resolved.parent != parent.resolve() or not stage.name.startswith('.mcpk-stage-'):
        raise PackError('临时目录清理路径校验失败。')
    if stage.exists():
        shutil.rmtree(stage)


def unpack(source_path, destination_path, log=NO_LOG, progress=NO_PROGRESS) -> dict:
    source = Path(source_path).expanduser().absolute()
    destination = Path(destination_path).expanduser().absolute()
    if destination.exists():
        raise PackError('输出目录已存在。请选择一个全新目录，避免覆盖文件。')
    if not destination.parent.is_dir():
        raise PackError('输出目录的父目录不存在。')
    if source.resolve().is_relative_to(destination.resolve()):
        raise PackError('输出目录不能包含原封包。')
    stage = Path(tempfile.mkdtemp(prefix='.mcpk-stage-', dir=destination.parent))
    try:
        with _open_pack(source) as data:
            pack = inspect_pack(data, log, lambda p: progress(p * 0.45))
            metadata = stage / META
            metadata.mkdir()
            for index, stream in enumerate(pack.streams):
                target = stage.joinpath(*PurePosixPath(stream.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as file:
                    actual = _inflate(data, stream.offset, file.write)
                if (actual.compressed, actual.sha256) != (stream.compressed, stream.sha256):
                    raise PackError('读取过程中源包发生变化。')
                progress(0.45 + 0.40 * (index + 1) / len(pack.streams))
            log('保存原包副本，供原结构回封使用……')
            shutil.copyfile(source, metadata / 'original.mcpk')
            if _hash_file(metadata / 'original.mcpk') != pack.source_sha256:
                raise PackError('源包在解包期间发生变化，已停止。')
            if _hash_buffer(data) != pack.source_sha256:
                raise PackError('源包在解包期间发生变化，已停止。')
        project = {
            'schema': 1, 'tool': 'MCPK Tool', 'version': VERSION, 'author': AUTHOR,
            'source_name': source.name, 'source_sha256': pack.source_sha256,
            'source_bytes': pack.size, 'file_count': len(pack.streams),
        }
        _json_write(metadata / 'project.json', project)
        report = {
            **project, 'operation': 'unpack', 'output_path': str(destination),
            'container_verified': True, 'game_runtime_verified': False,
            'trailer_bytes_preserved': 129,
            'files': [{'path': item.name, 'bytes': item.decoded, 'sha256': item.sha256} for item in pack.streams],
        }
        _json_write(metadata / 'unpack_report.json', report)
        # On Windows rename fails if the destination was created concurrently.
        stage.rename(destination)
        progress(1.0)
        log(f'解包完成：{len(pack.streams):,} 个文件。请保留 {META} 目录。')
        return {'file_count': len(pack.streams), 'output_path': str(destination),
                'report_path': str(destination / META / 'unpack_report.json')}
    finally:
        if stage.exists():
            _remove_stage(stage, destination.parent)


def _no_link(path: Path):
    if path.is_symlink() or path.is_junction():
        raise PackError(f'不支持符号链接或目录联接：{path}')


def _project_files(root: Path) -> set[str]:
    found = set()
    for folder, directories, files in os.walk(root, followlinks=False):
        current = Path(folder)
        if current == root and META in directories:
            directories.remove(META)
        for name in directories + files:
            _no_link(current / name)
        for name in files:
            path = current / name
            found.add(path.relative_to(root).as_posix())
    return found


def _copy_range(data, start: int, end: int, file):
    for offset in range(start, end, CHUNK):
        file.write(data[offset:min(offset + CHUNK, end)])


def repack(folder_path, output_path, log=NO_LOG, progress=NO_PROGRESS) -> dict:
    root = Path(folder_path).expanduser().absolute()
    output = Path(output_path).expanduser().absolute()
    if not root.is_dir():
        raise PackError('找不到待封包目录。')
    _no_link(root)
    _no_link(root / META)
    if output.exists():
        raise PackError('输出文件已存在。请选择新文件名，工具不会覆盖原包。')
    if not output.parent.is_dir():
        raise PackError('输出文件的父目录不存在。')
    if output.resolve().is_relative_to(root.resolve()):
        raise PackError('请把封包结果保存到解包目录外，避免混入资源文件。')
    template = root / META / 'original.mcpk'
    project_path = root / META / 'project.json'
    _no_link(template)
    _no_link(project_path)
    try:
        if project_path.stat().st_size > CHUNK:
            raise PackError('工具元数据异常过大。')
        project = json.loads(project_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise PackError(f'缺少有效的 {META}/project.json。请先用本工具重新解包原文件。') from error
    if not isinstance(project, dict) or project.get('schema') != 1:
        raise PackError('不支持的工具元数据版本。')
    report_path = Path(str(output) + '.report.json')
    if report_path.exists():
        raise PackError('同名封包报告已存在，请使用新文件名。')
    temp_path = None
    try:
        with _open_pack(template) as data:
            pack = inspect_pack(data, log, lambda p: progress(p * 0.30))
            if pack.source_sha256 != project.get('source_sha256'):
                raise PackError('原包副本校验失败。请重新解包，不要修改工具元数据目录。')
            expected = {stream.name for stream in pack.streams}
            actual = _project_files(root)
            missing = sorted(expected - actual)
            added = sorted(actual - expected)
            if missing or added:
                details = []
                if missing:
                    details.append('缺少/改名：' + '、'.join(missing[:4]))
                if added:
                    details.append('新增/改名：' + '、'.join(added[:4]))
                raise PackError('原结构回封不支持新增、删除或改名。\n' + '\n'.join(details))
            hashes = {}
            changed = []
            for index, stream in enumerate(pack.streams):
                path = root.joinpath(*PurePosixPath(stream.name).parts)
                if not path.is_file():
                    raise PackError(f'不是普通文件：{stream.name}')
                if path.stat().st_size >= 1 << 24:
                    raise PackError(f'文件超过 MCPK 24 位大小字段上限：{stream.name}')
                hashes[stream.name] = _hash_file(path)
                if hashes[stream.name] != stream.sha256:
                    changed.append(stream.name)
                progress(0.30 + 0.12 * (index + 1) / len(pack.streams))
            if 'contents.json' in changed:
                raise PackError('contents.json 是原始文件索引，本版本要求保留原字节。请恢复该文件后重试。')
            log(f'发现 {len(changed):,} 个修改文件，开始原结构回封。')
            changed_set = set(changed)
            handle, temporary = tempfile.mkstemp(prefix='.mcpk-stage-', dir=output.parent)
            os.close(handle)
            temp_path = Path(temporary)
            if not changed:
                with temp_path.open('wb') as result:
                    _copy_range(data, 0, len(data), result)
            else:
                table = bytearray(data[:pack.data_offset])
                with temp_path.open('wb') as result:
                    result.write(table)
                    for index, stream in enumerate(pack.streams):
                        start = result.tell()
                        if stream.name not in changed_set:
                            _copy_range(data, stream.offset, stream.offset + stream.compressed, result)
                            decoded = stream.decoded
                        else:
                            encoder = zlib.compressobj(9)
                            digest = hashlib.sha256()
                            decoded = 0
                            with root.joinpath(*PurePosixPath(stream.name).parts).open('rb') as resource:
                                while block := resource.read(CHUNK):
                                    decoded += len(block)
                                    if decoded >= 1 << 24:
                                        raise PackError(f'文件超过 MCPK 大小字段上限：{stream.name}')
                                    digest.update(block)
                                    result.write(encoder.compress(block))
                                result.write(encoder.flush())
                            if digest.hexdigest() != hashes[stream.name]:
                                raise PackError(f'回封过程中资源被修改：{stream.name}')
                        compressed = result.tell() - start
                        relative = start - pack.data_offset
                        if compressed >= 1 << 24 or relative >= 1 << 32:
                            raise PackError('回封结果超过 MCPK 索引字段上限。')
                        struct.pack_into('<I', table, stream.key_offset + 4, relative)
                        table[stream.key_offset + 8:stream.key_offset + 11] = compressed.to_bytes(3, 'little')
                        table[stream.key_offset + 12:stream.key_offset + 15] = decoded.to_bytes(3, 'little')
                        progress(0.42 + 0.32 * (index + 1) / len(pack.streams))
                    _copy_range(data, pack.footer_offset, len(data), result)
                    result.seek(0)
                    result.write(table)
            # Validate the exact rebuilt container, not just our in-memory plan.
            with _open_pack(temp_path) as rebuilt:
                verified = inspect_pack(rebuilt, log, lambda p: progress(0.74 + p * 0.24))
                if [item.name for item in verified.streams] != [item.name for item in pack.streams]:
                    raise PackError('回封校验失败：文件列表不一致。')
                for original, current in zip(pack.streams, verified.streams):
                    if current.sha256 != hashes[current.name]:
                        raise PackError(f'回封内容校验失败：{current.name}')
                    if current.name not in changed_set:
                        if (current.compressed != original.compressed
                                or rebuilt[current.offset:current.offset + current.compressed]
                                != data[original.offset:original.offset + original.compressed]):
                            raise PackError(f'未修改文件的压缩字节发生变化：{current.name}')
                if rebuilt[verified.footer_offset:] != data[pack.footer_offset:]:
                    raise PackError('尾部保留校验失败。')
                if not changed and verified.source_sha256 != pack.source_sha256:
                    raise PackError('未修改项目的逐字节回封校验失败。')
            if _hash_buffer(data) != pack.source_sha256:
                raise PackError('回封期间原包副本发生变化。')
        temp_path.rename(output)
        temp_path = None
        report = {
            'tool_version': VERSION, 'author': AUTHOR, 'operation': 'repack',
            'output_path': str(output), 'file_count': len(pack.streams),
            'changed_count': len(changed), 'changed_files': changed,
            'source_sha256': pack.source_sha256,
            'output_sha256': verified.source_sha256, 'output_bytes': verified.size,
            'container_verified': True, 'game_runtime_verified': False,
            'unchanged_stream_bytes_preserved': True, 'trailer_bytes_preserved': 129,
            'note': '仅验证封包结构、文件名和解压内容；原样保留用途未确认的尾部，不代表游戏加载已验证。',
        }
        _json_write(report_path, report)
        progress(1.0)
        log(f'封包完成：{len(pack.streams):,} 个文件，修改 {len(changed):,} 个。')
        return {'file_count': len(pack.streams), 'changed_count': len(changed),
                'output_path': str(output), 'report_path': str(report_path)}
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
