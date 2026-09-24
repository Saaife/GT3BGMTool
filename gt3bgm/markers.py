"""Beat-marker tables: the data that makes GT3's replay camera cut in time with a song.

A table is up to 32 channels of sample positions against a sample rate. Each frame the engine compares the
song's play position with the next marker per channel and raises a hit; the replay director turns hits into cuts.

GT3's channels (measured across PD's 17 race songs):
    ch1  every beat            ch2  every bar (4 beats)
    ch3  the camera cuts       ch4  identical copy of ch3, used by a second director mode
    ch5-9  director style switches - the engine reads them, PD never used them
"""

from __future__ import annotations
import struct

NUM_CHANNELS = 32
HEADER = 0xC                 # u32 0, u32 rate, u32 length
TABLE_MIN = 0x10C            # header + 32 counts + 32 offsets


class MarkerSet:
    def __init__(self, rate: int = 44100, length: int = 0):
        self.rate = rate
        self.length = length
        self.ch: list[list[int]] = [[] for _ in range(NUM_CHANNELS)]

    @staticmethod
    def read(data: bytes, at: int) -> "MarkerSet":
        _, rate, length = struct.unpack_from("<III", data, at)
        m = MarkerSet(rate, length)
        counts = struct.unpack_from(f"<{NUM_CHANNELS}I", data, at + HEADER)
        offs = struct.unpack_from(f"<{NUM_CHANNELS}I", data, at + HEADER + NUM_CHANNELS * 4)
        for c in range(NUM_CHANNELS):
            if counts[c]:
                m.ch[c] = list(struct.unpack_from(f"<{counts[c]}I", data, at + offs[c]))
        return m

    def write(self) -> bytes:
        out = bytearray(struct.pack("<III", 0, self.rate, self.length))
        off = TABLE_MIN
        counts, offsets = [], []
        for c in range(NUM_CHANNELS):
            counts.append(len(self.ch[c]))
            offsets.append(off)
            off += len(self.ch[c]) * 4
        out += struct.pack(f"<{NUM_CHANNELS}I", *counts)
        out += struct.pack(f"<{NUM_CHANNELS}I", *offsets)
        for c in range(NUM_CHANNELS):
            if self.ch[c]:
                out += struct.pack(f"<{len(self.ch[c])}I", *self.ch[c])
        return bytes(out)

    @property
    def size(self) -> int:
        return TABLE_MIN + 4 * self.total

    @property
    def total(self) -> int:
        return sum(len(c) for c in self.ch)

    @property
    def seconds(self) -> float:
        return self.length / self.rate if self.rate else 0.0

    def summary(self) -> str:
        used = [f"ch{c} {len(self.ch[c])}" for c in range(NUM_CHANNELS) if self.ch[c]]
        return ", ".join(used) if used else "no markers"

    def set_times(self, channel: int, times_seconds) -> None:
        self.ch[channel] = sorted({int(round(t * self.rate)) for t in times_seconds
                                   if 0 <= t * self.rate < (self.length or 1 << 62)})

    def set_cuts(self, times_seconds) -> None:
        """Camera cuts go on ch3 and its ch4 copy, the way PD's songs do it."""
        self.set_times(3, times_seconds)
        self.ch[4] = list(self.ch[3])

    def grid(self, bpm: float, first_beat: float, beats_per_bar: int = 4, cut_every_bars: int = 2,
             with_cuts: bool = True) -> None:
        """Fill beats/bars/cuts from a tempo. Good enough for anything with a steady pulse.
        `with_cuts=False` leaves ch3/ch4 alone, for when the cuts come from somewhere better."""
        beat = 60.0 / bpm
        end = self.seconds
        beats, bars, cuts, k, t = [], [], [], 0, first_beat
        while t < end:
            beats.append(t)
            if k % beats_per_bar == 0:
                bars.append(t)
                if k % (beats_per_bar * cut_every_bars) == 0:
                    cuts.append(t)
            k += 1
            t = first_beat + k * beat
        self.set_times(1, beats)
        self.set_times(2, bars)
        if with_cuts:
            self.set_cuts(cuts)

    def load_labels(self, path: str) -> int:
        """Audacity label export: 'start<TAB>end<TAB>label'. Labels: cut / bar / beat / chN."""
        names = {"cut": (3, 4), "bar": (2,), "beat": (1,)}
        found = {c: [] for c in range(NUM_CHANNELS)}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                parts = raw.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                try:
                    t = float(parts[0])
                except ValueError:
                    continue
                label = parts[2].strip().lower()
                if label in names:
                    chans = names[label]
                elif label.startswith("ch") and label[2:].isdigit():
                    chans = (int(label[2:]),)
                else:
                    continue
                for c in chans:
                    found[c].append(t)
        total = 0
        for c, times in found.items():
            if times:
                self.set_times(c, times)
                total += len(self.ch[c])
        return total

    def problems(self) -> list[str]:
        out = []
        for c in range(NUM_CHANNELS):
            v = self.ch[c]
            if not v:
                continue
            if any(x >= self.length for x in v):
                out.append(f"ch{c} has markers past the end of the song")
            if list(v) != sorted(set(v)):
                out.append(f"ch{c} is not sorted / has duplicates")
        return out
