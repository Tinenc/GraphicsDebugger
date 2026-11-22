"""Stream replay-relevant RDC bytes, excluding only named editor annotations.

Matches serialise/rdcfile.cpp's 0x100..0x102 binary/ASCII containers. Unknown
sections and EditedShaders/embedded SDKs stay in the digest. No decompression,
whole-frame allocation, capture replay or project write is performed.
"""
import hashlib
import struct
import time

SCHEMA = 'bread-rdc-replay-content/v1'
_EDITOR_SECTIONS = frozenset((3, 4, 5))  # Bookmarks, Notes, ResourceRenames.
_EDITOR_NAMES = frozenset((b'replaylab/cache/project-reuse-index',))
_BINARY = struct.Struct('<4sIQQQII')


def _read(stream, length):
    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError('Truncated RDC section')
    return raw


def _line(stream, limit):
    raw = stream.readline(limit + 1)
    if len(raw) > limit or not raw.endswith(b'\n'):
        raise ValueError('Invalid RDC ASCII section header')
    return raw[:-1]


def replay_digest(stream, hasher=None, checkpoint=None):
    """Return an explicitly scoped digest; unsupported containers are rejected."""
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    prefix = _read(stream, 32)
    magic, version, header_size = struct.unpack_from('<QII', prefix)
    if magic != 0x434f4452 or version not in (0x100, 0x101, 0x102):
        raise ValueError('Unsupported RDC container for replay identity')
    if not 32 <= header_size <= min(size, 16 * 1024 * 1024):
        raise ValueError('Invalid RDC header length')
    digest = hasher if hasher is not None else hashlib.sha256()
    digest.update(SCHEMA.encode('ascii') + b'\0')
    digest.update(prefix + _read(stream, header_size - 32))
    frame_count = 0
    section_count = 0
    next_check = time.monotonic() + 1
    while stream.tell() < size:
        section_count += 1
        if section_count > 65536:
            raise ValueError('Too many RDC sections')
        marker = _read(stream, 1)
        if marker == b'\0':
            head = marker + _read(stream, _BINARY.size - 1)
            marker4, kind, compressed, uncompressed, section_version, flags, length = _BINARY.unpack(head)
            if marker4 != b'\0\0\0\0' or not 1 <= length <= 2048:
                raise ValueError('Invalid RDC binary section header')
            raw_name = _read(stream, length)
            if not raw_name.endswith(b'\0'):
                raise ValueError('Invalid RDC section name')
            name = raw_name[:-1]
        elif marker == b'A':
            if _read(stream, 1) != b'\n':
                raise ValueError('Invalid RDC ASCII section marker')
            numbers = [_line(stream, 32) for _ in range(3)]
            if any(not item.isdigit() for item in numbers):
                raise ValueError('Invalid RDC ASCII section number')
            compressed, kind, section_version = [int(item) for item in numbers]
            name = _line(stream, 2048)
            uncompressed, flags = compressed, 1
        else:
            raise ValueError('Invalid RDC section marker')
        if compressed > size - stream.tell():
            raise ValueError('RDC section exceeds its container')
        if kind == 1:
            frame_count += 1
        excluded = kind in _EDITOR_SECTIONS or (kind == 0 and name in _EDITOR_NAMES)
        if excluded:
            stream.seek(compressed, 1)
            continue
        digest.update(struct.pack('<IQQQI', kind, compressed, uncompressed, section_version, flags))
        digest.update(struct.pack('<I', len(name)) + name)
        remaining = compressed
        while remaining:
            block = _read(stream, min(remaining, 4 * 1024 * 1024))
            digest.update(block)
            remaining -= len(block)
            if checkpoint is not None and time.monotonic() >= next_check:
                checkpoint('hashing RDC replay contents')
                next_check = time.monotonic() + 1
    if frame_count != 1:
        raise ValueError('RDC must contain exactly one FrameCapture section')
    return digest.hexdigest()
