"""Reading cut points straight out of an Audacity project (.aup3).

Why this exists: typing marker times by hand is miserable, but splitting a track at the cuts (Ctrl+I) is
something you do by eye in a few seconds. Audacity saves those splits in the project, so the project IS the
marker list - no exporting a label file, no second document to keep in sync.

An .aup3 is a SQLite database (stdlib `sqlite3`, no dependency). Two blobs matter:

    project.dict   FT_CharSize, then 0x0F <u16 id> <u16 byte len> <name> - the tag/attribute name table
    project.doc    a token stream that references those ids

Tokens, as measured (the ids are the FieldTypes enum in Audacity's ProjectSerializer):
    0 char size   1 start tag   2 end tag    3 string(u32 len)  4 int    5 bool   6 long
    7 long long   8 size_t      9 float+digits  10 double+digits  11 data  12 raw  13 push  14 pop

What we take: clip start times, label tracks, the project rate and its time-signature tempo.

What we deliberately do NOT take: the audio. Project audio is 32-bit float spread over clips that carry
trims, gains, pans, stretch and live effect chains - what you hear is Audacity's mixer, not the samples in
the blocks. Export a WAV and let Audacity do that job.
"""

from __future__ import annotations
import os
import sqlite3
import struct

FLOAT_SAMPLE = 0x4000F
SAME_TIME = 0.002        # clips on the left and right track of a pair land on the same instant


class Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag: str):
        self.tag = tag
        self.attrs: dict[str, object] = {}
        self.kids: list["Node"] = []

    def every(self, tag: str):
        if self.tag == tag:
            yield self
        for k in self.kids:
            yield from k.every(tag)


def _dictionary(blob: bytes) -> tuple[dict[int, str], int]:
    if not blob or blob[0] != 0:
        raise ValueError("no name dictionary - not an Audacity 3 project")
    char_size, i, names = blob[1], 2, {}
    enc = "utf-16-le" if char_size == 2 else "utf-8"
    while i < len(blob):
        if blob[i] != 0x0F:
            raise ValueError(f"unexpected token {blob[i]:#x} in the name dictionary")
        idn, ln = struct.unpack_from("<HH", blob, i + 1)
        names[idn] = blob[i + 5:i + 5 + ln].decode(enc)
        i += 5 + ln
    return names, char_size


def _tree(doc: bytes, names: dict[int, str], char_size: int) -> Node:
    enc = "utf-16-le" if char_size == 2 else "utf-8"
    root, stack, cur, i = None, [], None, 0
    while i < len(doc):
        t = doc[i]
        i += 1
        if t in (1, 2):                                   # start / end tag
            idn = struct.unpack_from("<H", doc, i)[0]
            i += 2
            if t == 1:
                node = Node(names[idn])
                if cur is not None:
                    cur.kids.append(node)
                elif root is None:
                    root = node
                stack.append(cur)
                cur = node
            else:
                cur = stack.pop() if stack else None
            continue
        if t in (11, 12):                                 # data / raw text - not addressed by id
            i += 4 + struct.unpack_from("<I", doc, i)[0]
            continue
        if t in (13, 14):                                 # push / pop
            continue
        idn = struct.unpack_from("<H", doc, i)[0]
        i += 2
        if t == 3:
            ln = struct.unpack_from("<I", doc, i)[0]
            i += 4
            value = doc[i:i + ln].decode(enc)
            i += ln
        elif t in (4, 6, 8):
            value = struct.unpack_from("<i", doc, i)[0]
            i += 4
        elif t == 5:
            value = bool(doc[i])
            i += 1
        elif t == 7:
            value = struct.unpack_from("<q", doc, i)[0]
            i += 8
        elif t == 9:                                      # value + digit count
            value = struct.unpack_from("<f", doc, i)[0]
            i += 8
        elif t == 10:
            value = struct.unpack_from("<d", doc, i)[0]
            i += 12
        else:
            raise ValueError(f"unknown token {t:#x} at offset {i}")
        if cur is not None:
            cur.attrs[names[idn]] = value
    if root is None:
        raise ValueError("the project contains no tags")
    return root


class Project:
    """What an .aup3 can tell us about where the cuts go."""

    def __init__(self, path: str):
        self.path = path
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = db.execute("select dict, doc from project").fetchone()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"not an Audacity 3 project ({e})")
        finally:
            db.close()
        if not row:
            raise ValueError("the project table is empty")
        names, char_size = _dictionary(row[0])
        self.root = _tree(row[1], names, char_size)
        a = self.root.attrs
        self.rate = int(float(a.get("rate", 0)))
        self.tempo = float(a.get("time_signature_tempo", 0) or 0)
        self.beats_per_bar = int(a.get("time_signature_upper", 4) or 4)

    @property
    def name(self) -> str:
        for t in self.root.every("wavetrack"):
            n = t.attrs.get("name")
            if n:
                return str(n)
        return os.path.splitext(os.path.basename(self.path))[0]

    def _clips(self):
        """Clips that are actually part of what you hear: muted tracks do not count."""
        for track in self.root.every("wavetrack"):
            if track.attrs.get("mute"):
                continue
            for clip in track.every("waveclip"):
                yield clip

    @property
    def muted_tracks(self) -> int:
        return sum(1 for t in self.root.every("wavetrack") if t.attrs.get("mute"))

    def cut_times(self) -> list[float]:
        """Where the track is split. A clip's audible start is offset + trimLeft - the hidden head that
        trimming leaves in the sequence is not played."""
        times = []
        for c in self._clips():
            t = float(c.attrs.get("offset", 0.0)) + float(c.attrs.get("trimLeft", 0.0))
            if t < 0:
                t = 0.0
            times.append(t)
        out = []
        for t in sorted(times):
            if not out or t - out[-1] > SAME_TIME:        # the two tracks of a stereo pair agree
                out.append(t)
        return out

    def label_times(self) -> dict[str, list[float]]:
        """Label tracks, grouped by the label's text. Empty titles are ignored."""
        out: dict[str, list[float]] = {}
        for lab in self.root.every("label"):
            title = str(lab.attrs.get("title", "")).strip().lower()
            if title:
                out.setdefault(title, []).append(float(lab.attrs.get("t", 0.0)))
        for v in out.values():
            v.sort()
        return out

    def find_wav(self) -> str | None:
        """Audacity exports next to the project under the project's name by default, so look there first."""
        folder = os.path.dirname(os.path.abspath(self.path))
        stem = os.path.splitext(os.path.basename(self.path))[0]
        for candidate in (f"{stem}.wav", f"{self.name}.wav"):
            p = os.path.join(folder, candidate)
            if os.path.isfile(p):
                return p
        return None

    def summary(self) -> str:
        bits = [f"{self.rate} Hz"]
        cuts = self.cut_times()
        bits.append(f"{len(cuts)} clip split(s)" if cuts else "no splits")
        labels = self.label_times()
        if labels:
            bits.append(", ".join(f"{len(v)} {k}" for k, v in sorted(labels.items())))
        if self.tempo:
            bits.append(f"project tempo {self.tempo:g}")
        if self.muted_tracks:
            bits.append(f"{self.muted_tracks} muted track(s) ignored")
        return " | ".join(bits)
