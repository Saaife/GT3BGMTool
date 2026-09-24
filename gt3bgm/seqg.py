"""Gran Turismo SEQG (music.seq) format — multi-sequence container.

Based on research by xan1242 (GTSeq2Midi) and analysis of GT3 music.seq.

Layout
------
  0x00  'SEQG'
  0x04  unknown (usually 0)
  0x08  sequence count (N)
  0x0C  Sequence[N] headers, each 72 bytes:
          MasterVolume  u32
          TempoMS       u32   (microseconds-ish; BPM = 240_000_000 / TempoMS)
          TrackPtr[16]  u32   (file offsets into the event streams)

Event stream (per track)
------------------------
  VLV delta-time, then command byte:

  0x00          nop / padding (1 byte)
  0x01          loop marker   (cmd + 1 pad = 2 bytes)
  0x02          end of track  (cmd + 2 bytes = 3 bytes)
  0x03          program/instrument change (cmd + program = 2 bytes)
  0x04          volume        (cmd + vol = 2 bytes)
  0x05          pan           (cmd + pan = 2 bytes)
  0x06          tempo         (rarely used)
  0x07-0x7F     other control event: cmd + one parameter byte (2 bytes). Seen in the original
                music.seq as 0x11, 0x19, 0x50, 0x5B-0x63 and others. Meaning not yet known, so
                they are kept as ("ctrl", cmd, param) and written back unchanged.
  0x80-0xFF     note on: note, velocity, then VLV duration
"""

from __future__ import annotations

import struct
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable

MAGIC = b"SEQG"
TRACK_COUNT = 16
PPQN = 120  # ticks per quarter note used by GT sequences
DEFAULT_BEND_RANGE = 3

# Event command IDs
CMD_NOP = 0x00
CMD_LOOP = 0x01
CMD_END = 0x02
CMD_PROGRAM = 0x03
CMD_VOLUME = 0x04
CMD_PAN = 0x05
CMD_TEMPO = 0x06


def encode_vlv(value: int) -> bytes:
    """Encode an unsigned integer as a MIDI-style variable-length quantity (little-endian byte order as used by GT)."""
    if value < 0:
        raise ValueError("VLV cannot be negative")
    if value == 0:
        return b"\x00"
    parts = []
    while value > 0:
        parts.append(value & 0x7F)
        value >>= 7
    # GT stores VLV with continuation bits on all but the last byte, in the order written
    out = bytearray()
    for i, p in enumerate(reversed(parts)):
        if i < len(parts) - 1:
            out.append(p | 0x80)
        else:
            out.append(p)
    return bytes(out)


def decode_vlv(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a GT VLV starting at offset. Returns (value, bytes_consumed)."""
    value = 0
    length = 0
    for i in range(4):
        if offset + i >= len(data):
            break
        b = data[offset + i]
        value = (value << 7) | (b & 0x7F)
        length += 1
        if b < 0x80:
            break
    return value, length


def tempo_ms_to_bpm(tempo_ms: int) -> float:
    if tempo_ms <= 0:
        return 0.0
    return 240_000_000.0 / tempo_ms


def bpm_to_tempo_ms(bpm: float) -> int:
    if bpm <= 0:
        return 500_000
    return int(round(240_000_000.0 / bpm))


@dataclass
class SeqTrack:
    """Decoded event list for one track."""
    events: list[tuple] = field(default_factory=list)  # (abs_tick, cmd, *args)
    raw: bytes = b""

    def append(self, abs_tick: int, cmd: int, *args):
        self.events.append((abs_tick, cmd, *args))


@dataclass
class Sequence:
    master_volume: int = 0x4000
    tempo_ms: int = 500_000
    track_pointers: list[int] = field(default_factory=lambda: [0] * TRACK_COUNT)
    tracks: list[SeqTrack] = field(default_factory=lambda: [SeqTrack() for _ in range(TRACK_COUNT)])
    # True for anything built in memory (e.g. imported from MIDI). Sequences read from a file are marked
    # False, and write() leaves those byte-for-byte as they were on disk.
    modified: bool = True

    @property
    def bpm(self) -> float:
        return tempo_ms_to_bpm(self.tempo_ms)


def _valid_ptr(ptr: int, size: int) -> bool:
    return ptr not in (0, 0xFFFFFFFF) and ptr < size


@dataclass
class SeqG:
    """Full music.seq container."""
    sequences: list[Sequence] = field(default_factory=list)
    raw: bytes = b""
    header_unk: int = 0

    @classmethod
    def read(cls, data: bytes) -> "SeqG":
        if data[:4] != MAGIC:
            raise ValueError(f"Not a SEQG file (got {data[:4]!r})")
        unk, count = struct.unpack_from("<II", data, 4)

        # Pass 1: the sequence headers. Every track pointer marks where one stream starts, so the
        # sorted set of them also tells us where each stream stops (at the next one, or end of file).
        headers = []
        for i in range(count):
            base = 12 + i * 72
            if base + 72 > len(data):
                break
            vol, tempo = struct.unpack_from("<II", data, base)
            ptrs = list(struct.unpack_from(f"<{TRACK_COUNT}I", data, base + 8))
            headers.append((vol, tempo, ptrs))
        starts = sorted({p for _, _, ptrs in headers for p in ptrs if _valid_ptr(p, len(data))})

        def bound_of(ptr: int) -> int:
            i = bisect_right(starts, ptr)
            return starts[i] if i < len(starts) else len(data)

        # Pass 2: the event streams
        sequences: list[Sequence] = []
        for vol, tempo, ptrs in headers:
            seq = Sequence(master_volume=vol, tempo_ms=tempo, track_pointers=ptrs, modified=False)
            for t, ptr in enumerate(ptrs):
                if _valid_ptr(ptr, len(data)):
                    seq.tracks[t] = _parse_track(data, ptr, bound_of(ptr))
            sequences.append(seq)
        return cls(sequences=sequences, raw=data, header_unk=unk)

    @classmethod
    def open(cls, path: str) -> "SeqG":
        with open(path, "rb") as f:
            return cls.read(f.read())

    def write(self) -> bytes:
        """Serialise the container.

        Sequences that were read from the file and not replaced keep their original bytes exactly.
        With nothing replaced the original file comes back unchanged. Only replaced sequences are
        rebuilt, so 'open, save, compare' on an untouched file is a true no-op.
        """
        n = len(self.sequences)
        loaded_n = struct.unpack_from("<I", self.raw, 8)[0] if len(self.raw) >= 12 else -1
        if self.raw and n == loaded_n:
            if not any(s.modified for s in self.sequences):
                return self.raw
            if any(not s.modified for s in self.sequences):
                return self._patch()
        return self._build_fresh()

    def _patch(self) -> bytes:
        """Keep the loaded file as it is; append each replaced sequence's tracks and repoint its header.

        Untouched sequences stay at their original offsets. The old bytes of a replaced sequence are
        zeroed (not removed, so nothing else moves), which also keeps a reader from running on into
        stale data. The file grows by the size of the new data.
        """
        out = bytearray(self.raw)
        orig = SeqG.read(self.raw)
        keep = {p for i, s_ in enumerate(orig.sequences) if not self.sequences[i].modified
                for p in s_.track_pointers if _valid_ptr(p, len(self.raw))}
        starts = sorted({p for s_ in orig.sequences for p in s_.track_pointers if _valid_ptr(p, len(self.raw))})
        for i, s_ in enumerate(orig.sequences):                 # the replaced sequences' old streams
            if not self.sequences[i].modified:
                continue
            for p in s_.track_pointers:
                if _valid_ptr(p, len(self.raw)) and p not in keep:
                    nxt = bisect_right(starts, p)
                    stop = starts[nxt] if nxt < len(starts) else len(self.raw)
                    out[p:stop] = bytes(stop - p)
        for i, seq in enumerate(self.sequences):
            if not seq.modified:
                continue
            ptrs = []
            for track in seq.tracks:
                while len(out) % 4:
                    out.append(0)
                ptrs.append(len(out))
                out += _track_bytes(track)
            struct.pack_into(f"<II{TRACK_COUNT}I", out, 12 + i * 72, seq.master_volume, seq.tempo_ms, *ptrs)
        return bytes(out)

    def _build_fresh(self) -> bytes:
        """Lay the whole container out from scratch (no usable original, or every sequence replaced)."""
        count = len(self.sequences)
        offset = 12 + count * 72
        headers = bytearray()
        body = bytearray()
        for seq in self.sequences:
            ptrs = []
            for track in seq.tracks:
                raw = _track_bytes(track)
                ptrs.append(offset + len(body))
                body += raw
            headers += struct.pack("<II", seq.master_volume, seq.tempo_ms)
            headers += struct.pack(f"<{TRACK_COUNT}I", *ptrs[:TRACK_COUNT])
        return MAGIC + struct.pack("<II", self.header_unk, count) + bytes(headers) + bytes(body)

    def replaced_indices(self) -> list[int]:
        return [i for i, s in enumerate(self.sequences) if s.modified]

    def sequence_count(self) -> int:
        return len(self.sequences)


def _track_bytes(track: SeqTrack) -> bytes:
    raw = track.raw if track.raw else _encode_track(track)
    if not raw:
        raw = encode_vlv(0) + bytes([CMD_END, 0, 0])      # an empty track still needs an end marker
    return raw


def check_rebuild(original: bytes, rebuilt: bytes, replaced: Iterable[int] = ()) -> list[str]:
    """Compare a rebuilt music.seq with the original. Returns a list of problems (empty means fine).

    Every sequence not in `replaced` must have the same header values and the same track bytes.
    With nothing replaced the two files must be identical.
    """
    replaced = set(replaced)
    problems: list[str] = []
    if not replaced and rebuilt != original:
        problems.append(f"nothing was replaced but the file changed ({len(original)} -> {len(rebuilt)} bytes)")
    try:
        a, b = SeqG.read(original), SeqG.read(rebuilt)
    except ValueError as e:
        return problems + [f"rebuilt file does not read back: {e}"]
    if len(a.sequences) != len(b.sequences):
        return problems + [f"sequence count changed ({len(a.sequences)} -> {len(b.sequences)})"]
    for i, (x, y) in enumerate(zip(a.sequences, b.sequences)):
        if i in replaced:
            continue
        if (x.master_volume, x.tempo_ms) != (y.master_volume, y.tempo_ms):
            problems.append(f"sequence {i}: header values changed")
        for t, (tx, ty) in enumerate(zip(x.tracks, y.tracks)):
            if not ty.raw.startswith(tx.raw):               # may run on into zeroed data, never differ
                problems.append(f"sequence {i} track {t}: bytes differ ({len(tx.raw)} -> {len(ty.raw)})")
    return problems


def _parse_track(data: bytes, start: int, bound: int | None = None) -> SeqTrack:
    """Parse one SEQG track.

    Per leo-the-leon/vgm-specs (gran-turismo/SEQG.md) and xan1242/gtseq2midi:

    Stream is:  VLV-delta, then either
      - event:   type (01-06) + value   [often preceded by a 00 delta]
      - note:    note (80-FF) + velocity (00-7F) + VLV duration
      - end:     02 + padding

    Bytes 00-7F are deltas / velocities / durations (high bit clear).
    Bytes 80-FF are note numbers (high bit set). Pitch-bend is NOT
    documented in vgm-specs; we no longer invent bend events from low bytes.

    `bound` is where this stream's bytes stop: the next track pointer in the file, or the end of
    the file. Parsing ends at the 02 marker, but the stream's raw bytes always run to `bound`, so
    anything after the marker (padding) is kept and a save can never cut a track short.

    Command bytes 07-7F are two-byte events (cmd + parameter). Reading them as one byte throws the
    parse off by one, after which a data byte can look like a 02 and end the track early.
    """
    track = SeqTrack()
    if start == 0 or start == 0xFFFFFFFF or start >= len(data):
        return track

    if bound is None or bound <= start:
        bound = len(data)
    cursor = start
    abs_time = 0
    max_iters = 500_000
    for _ in range(max_iters):
        if cursor >= len(data):
            break
        delta, vlen = decode_vlv(data, cursor)
        abs_time += delta
        cursor += vlen
        if cursor >= len(data):
            break
        cmd = data[cursor]

        # --- control events (type byte 01-06; value follows) ---
        if cmd == CMD_LOOP:  # 0x01
            # value byte is 0xFF for an infinite loop; keep it so a re-encode does not lose it
            track.append(abs_time, CMD_LOOP, data[cursor + 1] if cursor + 1 < len(data) else 0)
            cursor += 2
        elif cmd == CMD_END:  # 0x02
            param = data[cursor + 1] if cursor + 1 < len(data) else 0
            pad = data[cursor + 2] if cursor + 2 < len(data) else 0
            track.append(abs_time, CMD_END, param, pad)
            # GTSeq2Midi advances 3; value + pad
            cursor += 3
            break
        elif cmd == CMD_PROGRAM:  # 0x03  (programs are 1-based per vgm-specs)
            prog = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_PROGRAM, prog)
            cursor += 2
        elif cmd == CMD_VOLUME:  # 0x04
            vol = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_VOLUME, vol)
            cursor += 2
        elif cmd == CMD_PAN:  # 0x05
            pan = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_PAN, pan)
            cursor += 2
        elif cmd == CMD_TEMPO:  # 0x06 (rare)
            val = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, CMD_TEMPO, val)
            cursor += 2
        elif cmd == 0x00:
            # bare zero — treat as padding / zero-delta already consumed; skip
            cursor += 1
        elif cmd >= 0x80:
            # Note: high bit set. Pitch = cmd & 0x7F (MIDI 0-127).
            # Then velocity (7-bit), then VLV duration.
            note = cmd & 0x7F
            if cursor + 1 >= len(data):
                break
            vel = data[cursor + 1] & 0x7F
            dur, dlen = decode_vlv(data, cursor + 2)
            # duration 0 still produces a tick of sound for exporters
            track.append(abs_time, "note", note, vel, max(dur, 0))
            cursor += 2 + dlen
        else:
            # 07-7F: cmd + one parameter byte. Meaning unknown, so keep it verbatim.
            param = data[cursor + 1] if cursor + 1 < len(data) else 0
            track.append(abs_time, "ctrl", cmd, param)
            cursor += 2

    end = min(max(cursor, bound), len(data))
    track.raw = data[start:end]
    return track


def _encode_track(track: SeqTrack) -> bytes:
    """Encode a SeqTrack event list back to GT event bytes."""
    out = bytearray()
    last_tick = 0
    for ev in track.events:
        abs_tick = ev[0]
        cmd = ev[1]
        delta = max(0, abs_tick - last_tick)
        last_tick = abs_tick
        out += encode_vlv(delta)

        if cmd == CMD_NOP:
            out.append(CMD_NOP)
        elif cmd == CMD_LOOP:
            out += bytes([CMD_LOOP, (ev[2] if len(ev) > 2 else 0xFF) & 0xFF])
        elif cmd == CMD_END:
            param = ev[2] if len(ev) > 2 else 0
            pad = ev[3] if len(ev) > 3 else 0
            out += bytes([CMD_END, param & 0xFF, pad & 0xFF])
        elif cmd == CMD_PROGRAM:
            out += bytes([CMD_PROGRAM, ev[2] & 0xFF])
        elif cmd == CMD_VOLUME:
            out += bytes([CMD_VOLUME, ev[2] & 0xFF])
        elif cmd == CMD_PAN:
            out += bytes([CMD_PAN, ev[2] & 0xFF])
        elif cmd == CMD_TEMPO:
            out += bytes([CMD_TEMPO, ev[2] & 0xFF])
        elif cmd == "ctrl":
            out += bytes([ev[2] & 0x7F, ev[3] & 0xFF])
        elif cmd == "bend":
            bend = ev[2] & 0xFFFF
            low = bend & 0xFF
            high = (bend >> 8) & 0xFF
            out += bytes([low, high])
        elif cmd == "note":
            note, vel, dur = ev[2], ev[3], ev[4]
            # high bit marks "this is a note"; pitch in low 7 bits
            n = 0x80 | (int(note) & 0x7F)
            out += bytes([n, int(vel) & 0x7F])
            out += encode_vlv(max(0, int(dur)))
        else:
            # skip unknowns
            pass

    # ensure track ends
    if not track.events or track.events[-1][1] != CMD_END:
        out += encode_vlv(0)
        out += bytes([CMD_END, 0, 0])
    return bytes(out)


# ---------------------------------------------------------------------------
# MIDI export / import
# ---------------------------------------------------------------------------

def sequence_to_midi(seq: Sequence, path: str) -> None:
    """Write one Sequence to a standard Type-1 MIDI file (stdlib only)."""
    import io

    def write_vlv(buf: bytearray, value: int):
        # standard MIDI VLV (big-endian continuation)
        if value == 0:
            buf.append(0)
            return
        stack = []
        while value > 0:
            stack.append(value & 0x7F)
            value >>= 7
        while len(stack) > 1:
            buf.append(stack.pop() | 0x80)
        buf.append(stack.pop())

    tracks_data: list[bytes] = []

    # Tempo track
    tempo_track = bytearray()
    # meta tempo: FF 51 03 tt tt tt  (microseconds per quarter)
    # GT TempoMS is not exactly MIDI tempo; convert via BPM
    bpm = seq.bpm or 120.0
    us_per_quarter = int(round(60_000_000 / bpm))
    write_vlv(tempo_track, 0)
    tempo_track += bytes([0xFF, 0x51, 0x03])
    tempo_track += struct.pack(">I", us_per_quarter)[1:]  # 3 bytes
    write_vlv(tempo_track, 0)
    tempo_track += bytes([0xFF, 0x2F, 0x00])  # end of track
    tracks_data.append(bytes(tempo_track))

    for ch, track in enumerate(seq.tracks):
        if not track.events:
            continue
        c = ch & 0x0F
        # Build absolute-time events first, then sort, then write deltas. Note-on and note-off are
        # separate events: a note's off lands at start + duration, which is usually later than the next
        # note's start (chords, legato), so deltas cannot be chained note by note.
        # Sort order at one tick: note-offs, then program/controller/meta, then note-ons.
        timed: list[tuple[int, int, int, bytes]] = []      # (tick, order, index, bytes)
        for n, ev in enumerate(track.events):
            tick, cmd, args = ev[0], ev[1], ev[2:]
            if cmd == CMD_PROGRAM and args:
                # SEQG programs are 1-based; General MIDI is 0-based
                timed.append((tick, 1, n, bytes([0xC0 | c, max(0, (int(args[0]) & 0x7F) - 1)])))
            elif cmd == CMD_VOLUME and args:
                timed.append((tick, 1, n, bytes([0xB0 | c, 7, int(args[0]) & 0x7F])))
            elif cmd == CMD_PAN and args:
                timed.append((tick, 1, n, bytes([0xB0 | c, 10, int(args[0]) & 0x7F])))
            elif cmd == "note" and len(args) >= 3:
                note, vel, dur = int(args[0]) & 0x7F, int(args[1]), int(args[2])
                v = max(1, min(127, vel & 0x7F))
                d = max(1, dur)                            # duration 0 -> one tick so it is audible
                timed.append((tick, 2, n, bytes([0x90 | c, note, v])))
                timed.append((tick + d, 0, n, bytes([0x80 | c, note, 0])))
            elif cmd == CMD_LOOP:
                name = b"loopStart"
                timed.append((tick, 1, n, bytes([0xFF, 0x06, len(name)]) + name))
            elif cmd == CMD_END:
                name = b"loopEnd"
                timed.append((tick, 1, n, bytes([0xFF, 0x06, len(name)]) + name))
            elif cmd == "bend" and args:
                bend = int(args[0])
                timed.append((tick, 1, n, bytes([0xE0 | c, bend & 0x7F, (bend >> 7) & 0x7F])))
            # anything else has no MIDI form: it adds nothing here, and no delta is written for it

        timed.sort(key=lambda t: t[:3])
        buf = bytearray()
        label = f"GT track {ch}".encode()
        write_vlv(buf, 0)
        buf += bytes([0xFF, 0x03, len(label)]) + label     # lets an import map the track back exactly
        last = 0
        for tick, _, _, data in timed:
            write_vlv(buf, tick - last)
            buf += data
            last = tick
        write_vlv(buf, 0)
        buf += bytes([0xFF, 0x2F, 0x00])
        tracks_data.append(bytes(buf))

    # MIDI header
    ntrks = len(tracks_data)
    hdr = struct.pack(">4sIHHH", b"MThd", 6, 1, ntrks, PPQN)
    out = bytearray(hdr)
    for td in tracks_data:
        out += struct.pack(">4sI", b"MTrk", len(td))
        out += td
    with open(path, "wb") as f:
        f.write(out)


def midi_to_sequence(path: str, master_volume: int = 0x4000) -> Sequence:
    """Parse a Type-0/1 MIDI file into a GT Sequence (best-effort).

    Limitations (Phase 3):
    - Only note on/off, program, volume (CC7), pan (CC10), tempo are mapped.
    - Pitch bend is approximated.
    - Complex MIDI features (sysex, RPN, etc.) are ignored.
    - Note numbers are written in the 0x80-0xEC style expected by the GT player.
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"MThd":
        raise ValueError("Not a MIDI file")
    header_len, fmt, ntrks, division = struct.unpack_from(">IHHH", data, 4)
    if division & 0x8000:
        raise ValueError("SMPTE time division not supported")
    ppqn = division
    # scale factor to GT PPQN
    scale = PPQN / ppqn if ppqn else 1.0

    pos = 8 + header_len
    midi_tracks: list[list[tuple]] = []  # list of (abs_tick, event_bytes)

    def read_midi_vlv(buf, i):
        value = 0
        while i < len(buf):
            b = buf[i]
            i += 1
            value = (value << 7) | (b & 0x7F)
            if b < 0x80:
                break
        return value, i

    for _ in range(ntrks):
        if pos + 8 > len(data) or data[pos:pos + 4] != b"MTrk":
            break
        track_len = struct.unpack_from(">I", data, pos + 4)[0]
        track_data = data[pos + 8: pos + 8 + track_len]
        pos += 8 + track_len

        events = []
        i = 0
        abs_tick = 0
        running = 0
        while i < len(track_data):
            delta, i = read_midi_vlv(track_data, i)
            abs_tick += delta
            if i >= len(track_data):
                break
            status = track_data[i]
            if status & 0x80:
                running = status
                i += 1
            else:
                status = running
            etype = status & 0xF0
            channel = status & 0x0F

            if etype in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(track_data):
                    break
                a, b = track_data[i], track_data[i + 1]
                i += 2
                events.append((int(abs_tick * scale), etype, channel, a, b))
            elif etype in (0xC0, 0xD0):
                if i >= len(track_data):
                    break
                a = track_data[i]
                i += 1
                events.append((int(abs_tick * scale), etype, channel, a, 0))
            elif status == 0xFF:
                # meta
                if i >= len(track_data):
                    break
                meta = track_data[i]
                i += 1
                length, i = read_midi_vlv(track_data, i)
                meta_data = track_data[i:i + length]
                i += length
                events.append((int(abs_tick * scale), 0xFF, meta, meta_data))
            elif status in (0xF0, 0xF7):
                length, i = read_midi_vlv(track_data, i)
                i += length
            else:
                break
        midi_tracks.append(events)

    # Build GT sequence: map MIDI channels 0-15 → GT tracks 0-15
    seq = Sequence(master_volume=master_volume, tempo_ms=bpm_to_tempo_ms(120))
    # find tempo
    for evs in midi_tracks:
        for e in evs:
            if e[1] == 0xFF and e[2] == 0x51 and len(e[3]) >= 3:
                us = (e[3][0] << 16) | (e[3][1] << 8) | e[3][2]
                if us > 0:
                    bpm = 60_000_000 / us
                    seq.tempo_ms = bpm_to_tempo_ms(bpm)
                break

    # loopStart / loopEnd text markers (written by sequence_to_midi and by GTSeq2Midi).
    # Original SEQG files put the same LOOP tick and the same END tick on every track of a
    # sequence. When tracks end at different times the game restarts the short ones while the
    # long ones are still playing, which is heard as the sequence overlapping itself.
    # So we collect markers globally and force one shared END across all tracks.
    loops: dict[int, list[int]] = {}
    loop_ends: list[int] = []
    for evs in midi_tracks:
        target = None
        for e in evs:                                   # our own export names each track "GT track N"
            if e[1] == 0xFF and e[2] == 0x03 and e[3].startswith(b"GT track "):
                try:
                    target = int(e[3][9:])
                except ValueError:
                    pass
        if target is None:
            chans = sorted({e[2] for e in evs if e[1] != 0xFF})
            target = chans[0] if chans else None
        for e in evs:
            if e[1] != 0xFF or e[2] not in (0x01, 0x05, 0x06):
                continue
            name = e[3].strip()
            if name == b"loopStart" and target is not None and 0 <= target < TRACK_COUNT:
                loops.setdefault(target, []).append(e[0])
            elif name == b"loopEnd":
                loop_ends.append(e[0])

    # First pass: build events for every channel (no END yet) so we can pick one global end tick.
    built: list[SeqTrack] = [SeqTrack() for _ in range(TRACK_COUNT)]
    max_note_end = 0
    for ch in range(TRACK_COUNT):
        ch_events = []
        for evs in midi_tracks:
            for e in evs:
                if e[1] == 0xFF:
                    continue
                if len(e) >= 3 and e[2] == ch:
                    ch_events.append(e)
        ch_events.sort(key=lambda x: x[0])

        gt = built[ch]
        for tick in loops.get(ch, []):
            gt.append(tick, CMD_LOOP, 0xFF)
        active: dict[int, tuple[int, int]] = {}  # note -> (start_tick, vel)

        for e in ch_events:
            tick, etype = e[0], e[1]
            if etype == 0x90:  # note on
                note, vel = e[3], e[4]
                if vel == 0:
                    if note in active:
                        start, v = active.pop(note)
                        dur = max(1, tick - start)
                        gt.append(start, "note", note & 0x7F, v, dur)
                        max_note_end = max(max_note_end, start + dur)
                else:
                    active[note] = (tick, vel)
            elif etype == 0x80:  # note off
                note = e[3]
                if note in active:
                    start, v = active.pop(note)
                    dur = max(1, tick - start)
                    gt.append(start, "note", note & 0x7F, v, dur)
                    max_note_end = max(max_note_end, start + dur)
            elif etype == 0xC0:
                gt.append(tick, CMD_PROGRAM, (e[3] & 0x7F) + 1)     # MIDI 0-based -> SEQG 1-based
            elif etype == 0xB0:
                cc, val = e[3], e[4]
                if cc == 7:
                    gt.append(tick, CMD_VOLUME, val & 0x7F)
                elif cc == 10:
                    gt.append(tick, CMD_PAN, val & 0x7F)

        for note, (start, v) in active.items():
            dur = PPQN // 4
            gt.append(start, "note", note & 0x7F, v, dur)
            max_note_end = max(max_note_end, start + dur)

        gt.events.sort(key=lambda x: x[0])

    # One END tick for the whole sequence: prefer an explicit loopEnd marker, else just past the
    # last note. Original files always share this value across tracks.
    if loop_ends:
        end_tick = max(loop_ends)
    else:
        end_tick = max_note_end + 1 if max_note_end else 0

    # Tracks that only carry a loop marker still need the shared END so they stay in sync.
    any_content = any(t.events for t in built)
    for ch in range(TRACK_COUNT):
        gt = built[ch]
        if not gt.events and not any_content:
            continue
        # Drop any LOOP that sits at or after the chosen END (would never be reached).
        gt.events = [ev for ev in gt.events if not (ev[1] == CMD_LOOP and ev[0] >= end_tick)]
        if end_tick > 0 or gt.events:
            gt.append(end_tick, CMD_END, 0)
        gt.events.sort(key=lambda x: x[0])
        gt.raw = _encode_track(gt)
        seq.tracks[ch] = gt

    return seq