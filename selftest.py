"""Checks the tool against data it builds itself - no game files needed.

Run it after cloning, or before trusting a change:

    python selftest.py                 # everything that needs no outside data
    python selftest.py song.aup3       # also parse a real Audacity project
    python selftest.py music.seq       # also check a real music.seq survives open > save unchanged

This proves the code paths are sound. It does not replace testing against PD's own files, which is what
actually settles whether a format reading is right.
"""

from __future__ import annotations
import math
import os
import struct
import sys
import tempfile

from gt3bgm import __version__, psadpcm as ps, verify as vfy
from gt3bgm.adsinf import AdsInf, ENTRY_SIZE, RACE_GROUP
from gt3bgm.markers import MarkerSet, NUM_CHANNELS
from gt3bgm import seqg as sq

GROUPS = 17
passed = failed = 0


def check(ok: bool, what: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {what}" + (f" ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {what}" + (f" ({detail})" if detail else ""))


def tone(seconds: float, rate: int = 44100) -> list[list[int]]:
    """Something with real transients in it - a pure sine flatters an ADPCM encoder."""
    n = int(seconds * rate)
    left, right = [], []
    for i in range(n):
        t = i / rate
        env = 1.0 if (i // (rate // 4)) % 2 == 0 else 0.25          # a step every quarter second
        v = math.sin(2 * math.pi * 440 * t) * 0.6 + math.sin(2 * math.pi * 1970 * t) * 0.3
        left.append(max(-32768, min(32767, int(v * env * 30000))))
        right.append(max(-32768, min(32767, int(v * env * 22000))))
    return [left, right]


def build_inf(songs) -> bytes:
    """A minimal MADS index, written by hand so the reader is tested against something it did not produce.
    songs: [(group, name, title, artist, MarkerSet)]"""
    ordered = sorted(songs, key=lambda s: s[0])
    entries_at = 0x10 + GROUPS * 8
    pos = entries_at + len(ordered) * ENTRY_SIZE
    table_at = []
    for s in ordered:
        table_at.append(pos)
        pos += s[4].size
    strings_at, blob, offs = pos, bytearray(), []
    for group, name, title, artist, _ in ordered:
        row = []
        for text in (name, name + ".ads", title, artist):
            row.append(strings_at + len(blob))
            blob += text.encode("latin-1") + b"\0"
        offs.append(row)

    out = bytearray(b"MADS" + struct.pack("<III", 1, 0, len(ordered)))
    run = 0
    for g in range(GROUPS):
        count = sum(1 for s in ordered if s[0] == g)
        out += struct.pack("<II", entries_at + run * ENTRY_SIZE, count)   # empty groups keep the running offset
        run += count
    for i, row in enumerate(offs):
        out += struct.pack("<4I", *row) + struct.pack("<I", table_at[i])
    for s in ordered:
        out += s[4].write()
    return bytes(out + blob)


print(f"GT3BGMTool {__version__} self-test - Python {sys.version.split()[0]}\n")

print("PS-ADPCM and .ads")
pcm = tone(1.5)
ads = ps.write_ads(44100, pcm)
check(ads[:4] == b"SShd" and ads[0x28 - 8:0x28 - 4] == b"SSbd", "container has both chunk headers")
rate, back = ps.read_ads(ads)
check(rate == 44100 and len(back) == 2, "decodes back as 44100 Hz stereo")
db = vfy.snr(pcm, ads)
check(db > 30, "round-trip quality", f"{db:.1f} dB")
check(ps.samples_per_channel(ads) >= len(pcm[0]), "length rule covers the whole song",
      f"{ps.samples_per_channel(ads)} >= {len(pcm[0])}")
wav = os.path.join(tempfile.mkdtemp(), "t.wav")
ps.write_wav(wav, 44100, pcm)
r2, p2 = ps.read_wav(wav)
check(r2 == 44100 and p2 == pcm, "WAV survives a write and read unchanged")
try:
    ps.read_wav(__file__)
    check(False, "a non-WAV is rejected")
except Exception:
    check(True, "a non-WAV is rejected")

print("\nMarker tables")
m = MarkerSet(44100, 44100 * 10)
m.grid(120.0, 0.25, 4, 2)
check(len(m.ch[1]) == 20, "a 120 BPM grid over 10 s gives 20 beats", f"{len(m.ch[1])}")
check(m.ch[4] == m.ch[3] and len(m.ch[3]) == 3, "cuts land on ch3 with its ch4 copy", m.summary())
check(not m.problems(), "no markers out of range")
round_trip = MarkerSet.read(m.write(), 0)
check([list(c) for c in round_trip.ch] == [list(c) for c in m.ch] and round_trip.length == m.length,
      "table survives write and read")
check(m.size == 0x10C + 4 * m.total, "size matches the header plus the markers")
m.ch[9] = [m.length + 1]
check(bool(m.problems()), "a marker past the end is reported")

print("\nads.inf")
shared = "One Artist"                                   # PD shares strings; exercise the dedupe
m_a = MarkerSet(44100, 44100 * 60)
m_a.grid(100.0, 0.0)
base = build_inf([(0, "menu01", "Menu", shared, MarkerSet(44100, 44100)),
                  (RACE_GROUP, "race_a", "First", shared, m_a),
                  (RACE_GROUP, "race_b", "Second", "Other", MarkerSet(44100, 44100 * 90))])
inf = AdsInf.read(base)
check(len(inf.songs) == 3 and inf.groups == GROUPS, "reads three songs across 17 groups")
check(inf.write() == base, "rewrite is byte-identical")
check(inf.name(inf.songs[0]) == "menu01" and inf.file_name(inf.songs[1]) == "race_a.ads",
      "names and file fields read back")

inf = AdsInf.read(base)
table = MarkerSet(44100, ps.samples_per_channel(ads))
table.set_cuts([0.5, 1.0])
inf.add_song(RACE_GROUP, "mine", "My Song", "Me", table)
out = inf.write()
again = AdsInf.read(out)
check(len(again.songs) == 4, "added song is there after a read back")
added = next(s for s in again.songs if again.name(s) == "mine")
check(again.text_of(added, 2) == "My Song" and added.group == RACE_GROUP, "its text and group survive")
check(added.table.ch[3] == table.ch[3], "its markers survive")
res = vfy.verify(base, out, {"mine": ads})
check(res.ok, "verification passes on the result", res.report().splitlines()[0])
try:
    inf.add_song(RACE_GROUP, "mine", "Dupe", "", MarkerSet(44100, 1000))
    check(False, "a duplicate name is refused")
except ValueError:
    check(True, "a duplicate name is refused")

inf = AdsInf.read(base)
inf.rename(inf.songs[0], "Renamed", "Someone")
res = vfy.verify(base, inf.write(), {})
check(res.ok and any("retitled" in t for _, t in res.checks), "a retitle is reported, not called damage")

inf = AdsInf.read(base)
victim = next(s for s in inf.songs if inf.name(s) == "race_b")
inf.remove_song(victim)
out = inf.write()
after = AdsInf.read(out)
check(len(after.songs) == 2 and "race_b" not in [after.name(s) for s in after.songs], "removal takes the entry out")
check(after.write() == out, "the compacted file still rewrites identically")
res = vfy.verify(base, out, {}, removed={"race_b"})
check(res.ok, "verification passes on a removal", res.report().splitlines()[0])
res = vfy.verify(base, out, {})
check(not res.ok, "an UNEXPECTED removal is caught")

print("\nSequenced music (SEQG)")


def build_seqg() -> bytes:
    """Three sequences, written by hand. Sequence 0 track 0 has a two-byte control event (cmd 60, param 02:
    the param byte looks like an end marker but is not), a chord (overlapping notes) and padding after
    the real end marker."""
    n1 = bytes([0x00, 0xBC, 0x64, 0x60, 0x00, 0xC0, 0x64, 0x30])          # two notes at once, overlapping
    mid = bytes([0x10, 0x60, 0x02])                                        # control event, param 02
    n2 = bytes([0x10, 0xBE, 0x50, 0x20])
    end = bytes([0x00, 0x02, 0xFF, 0x00])
    t0 = bytes([0x00, 0x03, 0x05]) + n1 + mid + n2 + end + bytes(4)        # trailing padding
    t1 = bytes([0x00, 0xBC, 0x64, 0x10]) + end
    t2 = bytes([0x00, 0xB0, 0x64, 0x08]) + end
    body, ptrs, pos = b"", [], 12 + 3 * 72
    for t in (t0, t1, t2):
        ptrs.append(pos + len(body))
        body += t
    out = b"SEQG" + struct.pack("<II", 0, 3)
    rows = [(0x4000, 500000, [ptrs[0], ptrs[1]]), (0x4000, 400000, [ptrs[2]]), (0x3000, 300000, [ptrs[1]])]
    for vol, tempo, tp in rows:
        out += struct.pack("<II16I", vol, tempo, *(tp + [0] * (16 - len(tp))))
    return out + body


raw = build_seqg()
g = sq.SeqG.read(raw)
notes0 = [e for e in g.sequences[0].tracks[0].events if e[1] == "note"]
check(len(notes0) == 3, "notes after a two-byte control event are still read", f"{len(notes0)} notes")
check(not any(e[1] == "unknown" for e in g.sequences[0].tracks[0].events), "no unknown events")
check(g.sequences[0].tracks[0].raw.endswith(bytes(4)), "padding after the end marker is kept in the raw bytes")
check(g.write() == raw, "open > save on an untouched file is byte-identical", f"{len(raw)} bytes")
check(not sq.check_rebuild(raw, g.write()), "check_rebuild agrees")

# replace one sequence from MIDI; the others must be left exactly alone
notes = [(0, 60, 240), (0, 64, 120), (120, 67, 120), (240, 72, 60)]       # (start, note, dur) - chord + legato
tmp = tempfile.mkdtemp()
mid = os.path.join(tmp, "in.mid")
e = sq.Sequence()
tr = sq.SeqTrack()
for st, nt, du in notes:
    tr.append(st, "note", nt, 100, du)
tr.append(300, sq.CMD_END, 0)
e.tracks[0] = tr
sq.sequence_to_midi(e, mid)
new = sq.midi_to_sequence(mid)
g2 = sq.SeqG.read(raw)
g2.sequences[1] = new
out2 = g2.write()
check(not sq.check_rebuild(raw, out2, [1]), "replacing sequence 1 leaves 0 and 2 byte-identical")
p_ = [struct.unpack_from("<I", raw, 12 + 8 + 4 * k)[0] for k in range(2)]       # seq 0 track 0 and 1
p2 = struct.unpack_from("<I", raw, 12 + 72 + 8)[0]                               # seq 1 track 0 (replaced)
check(out2[:12 + 72] == raw[:12 + 72] and out2[12 + 2 * 72:p_[0] - 0] == raw[12 + 2 * 72:p_[0]]
      and out2[p_[0]:p2] == raw[p_[0]:p2], "untouched headers and data stay at their original offsets")
check(out2[p2:len(raw)] == bytes(len(raw) - p2), "the replaced sequence's old bytes are zeroed, not left as stale data")
reread = sq.SeqG.read(out2)
check([e[1] for e in reread.sequences[0].tracks[1].events] == [e[1] for e in g.sequences[0].tracks[1].events],
      "the track before the zeroed data does not absorb it")
back = sq.SeqG.read(out2)
check(len(back.sequences[1].tracks[0].events) >= len(notes), "the replaced sequence reads back")
g3 = sq.SeqG.read(raw)
g3.sequences = [new]
one = g3.write()
check(len(sq.SeqG.read(one).sequences) == 1, "a container rebuilt from scratch reads back")

# MIDI export timing: overlapping notes must keep their start times
def starts(path):
    s2 = sq.midi_to_sequence(path)
    return sorted((ev[0], ev[2], ev[4]) for ev in s2.tracks[0].events if ev[1] == "note")
check(starts(mid) == sorted(notes), "chord and legato notes keep their start times and lengths",
      str(starts(mid)))
# programs are 1-based in SEQG and 0-based in MIDI; loops and volume must survive too
e3 = sq.Sequence()
t3 = sq.SeqTrack()
t3.append(0, sq.CMD_PROGRAM, 5)
t3.append(0, sq.CMD_LOOP, 0xFF)
t3.append(0, sq.CMD_VOLUME, 100)
t3.append(10, "note", 60, 90, 20)
t3.append(40, sq.CMD_END, 0, 0)
e3.tracks[3] = t3
e3.tracks[9] = sq.SeqTrack()
e3.tracks[9].append(0, sq.CMD_LOOP, 0xFF)                       # a track with nothing but a loop marker
e3.tracks[9].append(40, sq.CMD_END, 0, 0)
mid3 = os.path.join(tmp, "loop.mid")
sq.sequence_to_midi(e3, mid3)
r3 = sq.midi_to_sequence(mid3)
check([e[2] for e in r3.tracks[3].events if e[1] == sq.CMD_PROGRAM] == [5], "instrument numbers survive MIDI export and import")
check(any(e[1] == sq.CMD_LOOP and e[2] == 0xFF for e in r3.tracks[3].events), "a loop marker survives MIDI export and import")
check(any(e[1] == sq.CMD_LOOP for e in r3.tracks[9].events), "a track holding only a loop marker keeps it")

# an event that has no MIDI form must not disturb what follows it
e2 = sq.Sequence()
t2 = sq.SeqTrack()
t2.append(0, "note", 60, 100, 240)
t2.append(50, "ctrl", 0x55, 1)
t2.append(60, sq.CMD_TEMPO, 3)
t2.append(120, "note", 64, 100, 60)
e2.tracks[0] = t2
mid2 = os.path.join(tmp, "skip.mid")
sq.sequence_to_midi(e2, mid2)
check(starts(mid2) == [(0, 60, 240), (120, 64, 60)], "skipped events do not shift later notes", str(starts(mid2)))

if len(sys.argv) > 1:
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".seq"):
            print(f"\nReal file: {arg}")
            data = open(arg, "rb").read()
            real = sq.SeqG.read(data)
            check(real.write() == data, "open > save gives the same file back",
                  f"{len(data)} bytes in, {len(real.write())} out")
            lost = [(i, t) for i, sqn in enumerate(real.sequences) for t, tk in enumerate(sqn.tracks)
                    if tk.raw and tk.raw != data[sqn.track_pointers[t]:sqn.track_pointers[t] + len(tk.raw)]]
            check(not lost, "every track's bytes match the file", f"{len(lost)} differ")
            unk = sum(1 for sqn in real.sequences for tk in sqn.tracks for e in tk.events if e[1] == "unknown")
            check(unk == 0, "every event in the file is understood", f"{unk} unknown")
            same = sum(1 for sqn in real.sequences for tk in sqn.tracks
                       if sq._encode_track(tk) == tk.raw[:len(sq._encode_track(tk))])
            total = sum(1 for sqn in real.sequences for tk in sqn.tracks if tk.events)
            check(same == total, "every track re-encodes to its original bytes", f"{same}/{total}")
            bad = 0
            for n, sqn in enumerate(real.sequences):
                pth = os.path.join(tempfile.mkdtemp(), "r.mid")
                sq.sequence_to_midi(sqn, pth)
                back = sq.midi_to_sequence(pth)
                for a, b in zip(sqn.tracks, back.tracks):
                    na = sorted((e[0], e[2], e[3], e[4]) for e in a.events if e[1] == "note")
                    nb = sorted((e[0], e[2], e[3], e[4]) for e in b.events if e[1] == "note")
                    la = sorted(e[0] for e in a.events if e[1] == sq.CMD_LOOP)
                    lb = sorted(e[0] for e in b.events if e[1] == sq.CMD_LOOP)
                    bad += (na != nb) + (la != lb)
            check(bad == 0, "notes and loops survive MIDI export and import in every sequence", f"{bad} differ")

print("\nBackup")
from gt3bgm.backup import backup_files
srcdir, root = tempfile.mkdtemp(), tempfile.mkdtemp()
for nm, blob in (("music.inf", b"MSEQ" + bytes(20)), ("music.seq", raw), ("music.ins", b"INST" + bytes(500))):
    with open(os.path.join(srcdir, nm), "wb") as f:
        f.write(blob)
b1, done = backup_files(srcdir, ["music.inf", "music.seq", "music.ins"], root)
check(all(open(os.path.join(b1, n), "rb").read() == open(os.path.join(srcdir, n), "rb").read() for n in done),
      "backup copies match the originals", f"{len(done)} files")
b2, _ = backup_files(srcdir, done, root)
check(b1 != b2 and os.path.isdir(b1) and os.path.isdir(b2), "a second backup gets its own folder")
try:
    backup_files(srcdir, done, srcdir)
    check(False, "backing up into the source folder is refused")
except ValueError:
    check(True, "backing up into the source folder is refused")
try:
    backup_files(srcdir, ["nope.seq"], root)
    check(False, "a missing file is reported")
except ValueError:
    check(True, "a missing file is reported")

aup = [a for a in sys.argv[1:] if not a.lower().endswith(".seq")]
if aup:
    print("\nAudacity project")
    from gt3bgm.audacity import Project
    try:
        pr = Project(aup[0])
        check(pr.rate > 0, "project parses", pr.summary())
        cuts = pr.cut_times()
        check(cuts == sorted(cuts) and all(c >= 0 for c in cuts), "cut times are sorted and non-negative",
              f"{len(cuts)} of them")
    except Exception as e:
        check(False, "project parses", str(e))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
