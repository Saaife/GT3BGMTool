"""Gran Turismo 3 music.inf (MSEQ) — sequenced song index.

Layout (version 1)
------------------
  0x00  'MSEQ'
  0x04  version (1)
  0x08  unknown (0)
  0x0C  song count (N)
  0x10  song count again (N)   — mirrors ads.inf style dual count
  0x14  N × { offset_to_entry u32, count u32 }   (usually count=1)
  ...   N × SongEntry (0x14 bytes each):
          name_off, seqfile_off, title_off, artist_off, seq_index
  ...   string table (latin-1, null-terminated)

seq_index selects which sequence inside the paired music.seq file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


MAGIC = b"MSEQ"
ENTRY_SIZE = 0x14


@dataclass
class MseqSong:
    name: str = ""
    seq_file: str = "music.seq"
    title: str = ""
    artist: str = ""
    seq_index: int = 0
    # original string offsets (for in-place rewrite when possible)
    _offs: list[int] = field(default_factory=lambda: [0, 0, 0, 0])


@dataclass
class Mseq:
    version: int = 1
    songs: list[MseqSong] = field(default_factory=list)
    raw: bytes = b""

    @classmethod
    def read(cls, data: bytes) -> "Mseq":
        if data[:4] != MAGIC:
            raise ValueError(f"Not an MSEQ file (got {data[:4]!r})")
        version, unk, count, count2 = struct.unpack_from("<IIII", data, 4)
        # Index table at 0x14
        index = []
        for i in range(count):
            off, cnt = struct.unpack_from("<II", data, 0x14 + i * 8)
            index.append((off, cnt))

        # String helper
        def getstr(off: int) -> str:
            if off <= 0 or off >= len(data):
                return ""
            end = data.find(b"\0", off)
            if end < 0:
                end = len(data)
            return data[off:end].decode("latin-1", errors="replace")

        songs: list[MseqSong] = []
        for off, _cnt in index:
            if off + ENTRY_SIZE > len(data):
                continue
            o0, o1, o2, o3, seq_idx = struct.unpack_from("<5I", data, off)
            s = MseqSong(
                name=getstr(o0),
                seq_file=getstr(o1),
                title=getstr(o2),
                artist=getstr(o3),
                seq_index=seq_idx,
                _offs=[o0, o1, o2, o3],
            )
            songs.append(s)

        return cls(version=version, songs=songs, raw=data)

    @classmethod
    def open(cls, path: str) -> "Mseq":
        with open(path, "rb") as f:
            return cls.read(f.read())

    def write(self) -> bytes:
        """Rebuild MSEQ. Strings are packed after the entry table."""
        n = len(self.songs)
        # Build string table first to know offsets
        # Layout: header(0x14) + index(n*8) + entries(n*0x14) + strings
        header_size = 0x14
        index_size = n * 8
        entries_size = n * ENTRY_SIZE
        strings_at = header_size + index_size + entries_size

        strings = bytearray()
        seen: dict[str, int] = {}

        def add_str(s: str) -> int:
            nonlocal strings
            if s in seen:
                return seen[s]
            off = strings_at + len(strings)
            seen[s] = off
            strings += s.encode("latin-1", errors="replace") + b"\0"
            return off

        entry_blobs = []
        for song in self.songs:
            offs = [
                add_str(song.name),
                add_str(song.seq_file or "music.seq"),
                add_str(song.title),
                add_str(song.artist),
            ]
            entry_blobs.append(
                struct.pack("<5I", offs[0], offs[1], offs[2], offs[3], song.seq_index)
            )

        out = bytearray()
        out += MAGIC
        out += struct.pack("<IIII", self.version, 0, n, n)
        # index table — each entry points at its slot in the entry table
        entries_base = header_size + index_size
        for i in range(n):
            out += struct.pack("<II", entries_base + i * ENTRY_SIZE, 1)
        for blob in entry_blobs:
            out += blob
        out += strings
        return bytes(out)

    def find(self, name: str) -> MseqSong | None:
        name_l = name.lower()
        for s in self.songs:
            if s.name.lower() == name_l:
                return s
        return None
