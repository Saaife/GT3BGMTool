"""Checks run before the tool tells anyone "this is good to go".

The point is to catch the things that only show up in game, where a mistake costs a boot, a menu reset and a
race: a rewrite that disturbs PD's songs, a marker table that points past the end of a song, a name collision
(the Favorite Music List keys songs by a hash of the FILE name), or an .ads that does not decode back.
"""

from __future__ import annotations
import math
from .adsinf import AdsInf, MUSIC_LIST_CAP, RACE_GROUP
from . import psadpcm as ps


class Result:
    def __init__(self):
        self.checks: list[tuple[bool, str]] = []

    def add(self, ok: bool, text: str) -> None:
        self.checks.append((ok, text))

    @property
    def ok(self) -> bool:
        return all(ok for ok, _ in self.checks)

    def report(self) -> str:
        lines = [("PASS  " if ok else "FAIL  ") + text for ok, text in self.checks]
        head = "Everything checks out." if self.ok else "Something is wrong - do not use this build:"
        return head + "\n\n" + "\n".join(lines)


def verify(original: bytes, rebuilt: bytes, encoded: dict[str, bytes],
           changed: set[str] = frozenset(), removed: set[str] = frozenset()) -> Result:
    """original/rebuilt: the ads.inf before and after editing. encoded: {song name: .ads bytes} for songs
    whose audio this session wrote. `changed` and `removed` are the edits the user asked for, so they are
    reported as intentional instead of counted as damage."""
    r = Result()
    try:
        before = AdsInf.read(original)
        after = AdsInf.read(rebuilt)
    except Exception as e:                                  # a file we cannot read back is a hard fail
        r.add(False, f"the rebuilt ads.inf does not parse: {e}")
        return r

    old = {before.name(s): s for s in before.songs}
    new = {after.name(s): s for s in after.songs}
    missing = [n for n in old if n not in new and n not in removed]
    r.add(not missing, f"all {len(old) - len(removed)} kept songs still present" if not missing
          else f"songs lost in the rewrite: {', '.join(missing)}")
    if removed:
        gone = sorted(n for n in removed if n not in new)
        r.add(len(gone) == len(removed), f"you removed {len(gone)} song(s): {', '.join(gone[:6])}"
              if len(gone) == len(removed) else "a song you removed is still in the index")

    disturbed, retitled = [], []
    for n, s in old.items():
        if n not in new:
            continue
        t = new[n]
        same_markers = [list(c) for c in s.table.ch] == [list(c) for c in t.table.ch]
        structural = before.text_of(s, 1) == after.text_of(t, 1) and s.group == t.group
        if n in changed:                                  # only the file name and group are sacred here
            if not structural:
                disturbed.append(n)
            continue
        if not (structural and same_markers and s.table.length == t.table.length):
            disturbed.append(n)
        elif any(before.text_of(s, i) != after.text_of(t, i) for i in (2, 3)):
            retitled.append(n)
    r.add(not disturbed, "untouched songs keep their file name, markers, length and group" if not disturbed
          else f"songs were disturbed without being asked: {', '.join(disturbed[:6])}")
    if retitled:
        r.add(True, f"you retitled {len(retitled)} song(s): {', '.join(retitled[:6])}")
    if changed:
        r.add(True, f"you edited {len(changed)} existing song(s): {', '.join(sorted(changed)[:6])}")

    race = after.group_songs(RACE_GROUP)
    r.add(len(race) <= MUSIC_LIST_CAP,
          f"race group holds {len(race)} songs (the Favorite Music List caps at {MUSIC_LIST_CAP})")
    names = [after.name(s).lower() for s in after.songs]
    dupes = {n for n in names if names.count(n) > 1}
    r.add(not dupes, "every song name is unique (they are keyed by a hash of the file name)" if not dupes
          else f"duplicate song names: {', '.join(dupes)}")

    for name, ads in encoded.items():
        song = new.get(name)
        if song is None:
            r.add(False, f"'{name}' is missing from the rebuilt ads.inf")
            continue
        want = ps.samples_per_channel(ads)
        r.add(song.table.length == want,
              f"'{name}' length matches its audio ({song.table.length} samples)" if song.table.length == want
              else f"'{name}' length is {song.table.length} but the .ads holds {want}")
        problems = song.table.problems()
        r.add(not problems, f"'{name}' markers are sorted and inside the song" if not problems
              else f"'{name}': " + "; ".join(problems))

        try:
            rate, back = ps.read_ads(ads)
            r.add(rate == 44100 and len(back) == 2,
                  f"'{name}' decodes back as {rate} Hz stereo" if rate == 44100 and len(back) == 2
                  else f"'{name}' decodes as {rate} Hz, {len(back)} channel(s) - the game expects 44100 stereo")
        except Exception as e:
            r.add(False, f"'{name}' does not decode back: {e}")
    return r


def snr(original: list[list[int]], ads: bytes) -> float:
    """Round-trip quality of an encode, in dB. PD-like material lands around 35-48 dB."""
    _, back = ps.read_ads(ads)
    sig = err = 0
    for c in range(min(len(original), len(back))):
        src, dst = original[c], back[c]
        for i, x in enumerate(src):
            j = i + ps.FRAME_SAMPLES          # PD's silent lead-in frame
            if j >= len(dst):
                break
            d = x - dst[j]
            sig += x * x
            err += d * d
    return 10 * math.log10(sig / err) if err else 99.0
