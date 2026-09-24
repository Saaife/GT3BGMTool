"""GT3's song index: data/bgm/ads.inf ('MADS' version 1).

    0x00  'MADS', version 1, 0, song count
    0x10  17 groups of {first entry offset, count}
    0x98  the entries themselves, 20 bytes each: name, file, title, artist (string offsets) + marker table offset
          stored grouped, in group order
          then the marker tables, back to back, in the same order as the entries
          then one shared string blob, to the end of the file

Two rules the engine imposes, both read out of the parser at 0x22bb38:
  * the GROUP COUNT is fixed at 17 (all of them are used) - you cannot add a group
  * entries inside a group are walked with `while (i < count)`, so a group CAN grow, and the file is loaded
    into a heap buffer sized to the file, so it may get bigger

And one PD quirk that silently corrupts the file if you miss it: a group with NO entries still stores the
running entry offset, not the start of the entry array.

The race songs live in group 2. The Options "Favorite Music List" is built from that group (capacity 64) and
keyed by a hash of each song's FILE name, so file names must be unique.
"""

from __future__ import annotations
import struct
from .markers import MarkerSet

RACE_GROUP = 2
MUSIC_LIST_CAP = 64
ENTRY_SIZE = 20


class Song:
    def __init__(self, group: int):
        self.group = group
        self.off = [0, 0, 0, 0]      # name, file, title, artist - offsets into the ORIGINAL blob
        self.text: list[str] | None = None   # set instead of `off` for songs we add, rename or compact
        self.table = MarkerSet()
        self.added = False           # added in this session, as opposed to merely carrying explicit text

    @property
    def is_new(self) -> bool:
        return self.added


class AdsInf:
    def __init__(self):
        self.header = b""
        self.groups = 0
        self.songs: list[Song] = []
        self.strings = b""
        self.strings_at = 0

    @staticmethod
    def read(data: bytes) -> "AdsInf":
        if data[:4] != b"MADS" or struct.unpack_from("<I", data, 4)[0] != 1:
            raise ValueError("not a GT3 ads.inf (expected MADS version 1)")
        a = AdsInf()
        a.header = data[:0x10]
        a.groups = (struct.unpack_from("<I", data, 0x10)[0] - 0x10) // 8
        for g in range(a.groups):
            off, cnt = struct.unpack_from("<II", data, 0x10 + g * 8)
            for k in range(cnt):
                p = off + k * ENTRY_SIZE
                s = Song(g)
                s.off = list(struct.unpack_from("<4I", data, p))
                s.table = MarkerSet.read(data, struct.unpack_from("<I", data, p + 16)[0])
                a.songs.append(s)
        a.strings_at = min(min(s.off) for s in a.songs)
        a.strings = data[a.strings_at:]
        return a

    def text_of(self, song: Song, i: int) -> str:
        if song.text is not None:
            return song.text[i]
        o = song.off[i] - self.strings_at
        end = self.strings.index(b"\0", o)
        return self.strings[o:end].decode("latin-1")

    def name(self, song: Song) -> str:
        return self.text_of(song, 0)

    def file_name(self, song: Song) -> str:
        """The .ads the game loads for this song, as named in the entry."""
        return self.text_of(song, 1)

    def write(self) -> bytes:
        ordered = sorted(self.songs, key=lambda s: s.group)
        entries_at = 0x10 + self.groups * 8
        pos = entries_at + len(ordered) * ENTRY_SIZE
        table_pos = []
        for s in ordered:
            table_pos.append(pos)
            pos += s.table.size
        strings_at = pos
        delta = strings_at - self.strings_at

        extra = bytearray()
        new_off: dict[int, list[int]] = {}
        seen: dict[str, int] = {}     # PD shares strings between entries (17 songs, one 'daiki kasho')
        for i, s in enumerate(ordered):
            if s.text is None:
                continue
            offs = []
            for t in s.text:
                at = seen.get(t)
                if at is None:
                    at = seen[t] = strings_at + len(self.strings) + len(extra)
                    extra += t.encode("latin-1") + b"\0"
                offs.append(at)
            new_off[i] = offs

        out = bytearray(self.header)
        struct.pack_into("<I", out, 0xC, len(ordered))
        run = 0
        for g in range(self.groups):
            cnt = sum(1 for s in ordered if s.group == g)
            out += struct.pack("<II", entries_at + run * ENTRY_SIZE, cnt)
            run += cnt
        for i, s in enumerate(ordered):
            offs = new_off[i] if s.text is not None else [o + delta for o in s.off]
            out += struct.pack("<4I", *offs) + struct.pack("<I", table_pos[i])
        for s in ordered:
            out += s.table.write()
        out += self.strings + bytes(extra)
        return bytes(out)

    def add_song(self, group: int, name: str, title: str, artist: str, table: MarkerSet) -> Song:
        if not 0 <= group < self.groups:
            raise ValueError(f"group must be 0..{self.groups - 1}")
        if any(self.name(s).lower() == name.lower() for s in self.songs):
            raise ValueError(f"a song called '{name}' is already in this ads.inf")
        s = Song(group)
        s.text = [name, name + ".ads", title, artist]
        s.added = True
        s.table = table
        at = max((i for i, x in enumerate(self.songs) if x.group == group), default=len(self.songs) - 1)
        self.songs.insert(at + 1, s)
        return s

    def materialize(self) -> None:
        """Give every song its own strings and drop the original blob, so nothing dead is carried along.
        Only worth doing when an entry goes away - otherwise the blob is reused and the rewrite stays
        byte-identical."""
        for s in self.songs:
            if s.text is None:
                s.text = [self.text_of(s, i) for i in range(4)]
        self.strings = b""

    def remove_song(self, song: Song) -> None:
        """Take an entry out of the index. The .ads on disk is left alone - the game simply stops asking
        for it, and deleting someone's audio is not this tool's call."""
        if song not in self.songs:
            raise ValueError("that song is not in this index")
        self.materialize()
        self.songs.remove(song)

    def rename(self, song: Song, title: str, artist: str) -> None:
        """Retitle any song, including PD's - the whole file is rebuilt, so the strings can change."""
        song.text = [self.text_of(song, 0), self.text_of(song, 1), title, artist]

    def group_songs(self, group: int) -> list[Song]:
        return [s for s in self.songs if s.group == group]
