"""PS2 PS-ADPCM codec and the .ads container GT3/GT4 use for streamed music.

Layout, read out of both engines rather than guessed:
    SShd | 0x18 | type 0x10 (PS-ADPCM) | rate | channels | interleave 0x400 | -1 | -1
    SSbd | size | then interleave-sized blocks, L, R, L, R ...
Per channel the first frame is silent with flag 6, body frames carry flag 2 and the last frame flag 3.
A frame is 16 bytes: a header byte (predictor << 4 | shift), a flag byte, then 28 samples as 4-bit nibbles.
"""

from __future__ import annotations
import struct

COEF = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
FRAME_SAMPLES = 28
FRAME_BYTES = 16
INTERLEAVE = 0x400


def _clamp16(v: int) -> int:
    return -32768 if v < -32768 else (32767 if v > 32767 else v)


def decode_frame(frame: bytes, hist: list[int], out: list[int]) -> None:
    """Decode one 16-byte frame, appending 28 samples to `out`. `hist` is [h1, h2], updated in place."""
    shift = frame[0] & 0xF
    pred = min(frame[0] >> 4, 4)
    if shift > 12:
        shift = 9
    silent = frame[1] >= 7
    c0, c1 = COEF[pred]
    h1, h2 = hist
    for i in range(FRAME_SAMPLES):
        nib = (frame[2 + (i >> 1)] >> ((i & 1) * 4)) & 0xF
        if nib > 7:
            nib -= 16
        s = 0 if silent else _clamp16((nib << (12 - shift)) + ((c0 * h1 + c1 * h2) >> 6))
        out.append(s)
        h2, h1 = h1, s
    hist[0], hist[1] = h1, h2


def encode_frame(samples: list[int], flag: int, hist: list[int], full_search: bool = True) -> bytes:
    """Encode 28 samples. Tries each predictor with the shift estimate +/-1 against the TRUE decoded history,
    which is what makes the output match the console decoder (and, byte for byte, our C# encoder).
    All integer maths: `q` rounds away from zero, like PD's own quantiser."""
    h1, h2 = hist
    best_err = None
    best = None
    preds = range(5) if full_search else (_estimate_predictor(samples, h1, h2),)
    for p in preds:
        c0, c1 = COEF[p]
        e1, e2, max_res = h1, h2, 0
        for x in samples:
            r = x - ((c0 * e1 + c1 * e2) >> 6)
            if r < 0:
                r = -r
            if r > max_res:
                max_res = r
            e2, e1 = e1, x
        est = 12
        while est > 0 and max_res > 7 * (1 << (12 - est)):
            est -= 1
        lo, hi = max(0, est - 1), min(12, est + 1)
        for shift in range(lo, hi + 1):
            k = 12 - shift
            step = 1 << k
            half = step >> 1
            d1, d2, err = h1, h2, 0
            nibs = []
            add = nibs.append
            for x in samples:
                pv = (c0 * d1 + c1 * d2) >> 6
                num = x - pv
                if num >= 0:
                    q = (num + half) >> k
                    if q > 7:
                        q = 7
                else:
                    q = -((half - num) >> k)
                    if q < -8:
                        q = -8
                v = (q << k) + pv
                if v > 32767:
                    v = 32767
                elif v < -32768:
                    v = -32768
                e = x - v
                err += e * e
                add(q & 0xF)
                d2, d1 = d1, v
                if best_err is not None and err > best_err:
                    break
            else:
                if best_err is None or err < best_err:
                    best_err = err
                    best = (p, shift, nibs, d1, d2)
    p, shift, nibs, d1, d2 = best
    hist[0], hist[1] = d1, d2
    body = bytes((nibs[2 * i] | (nibs[2 * i + 1] << 4)) for i in range(14))
    return bytes((p << 4 | shift, flag)) + body


def _estimate_predictor(samples: list[int], h1: int, h2: int) -> int:
    best_p, best_r = 0, None
    for p in range(5):
        c0, c1 = COEF[p]
        e1, e2, m = h1, h2, 0
        for x in samples:
            r = abs(x - ((c0 * e1 + c1 * e2) >> 6))
            if r > m:
                m = r
            e2, e1 = e1, x
        if best_r is None or m < best_r:
            best_p, best_r = p, m
    return best_p


def read_ads(data: bytes) -> tuple[int, list[list[int]]]:
    """.ads -> (rate, [channel samples...])"""
    if data[:4] != b"SShd":
        raise ValueError("not an .ads file (no SShd header)")
    codec, rate, chans, il = struct.unpack_from("<IIII", data, 8)
    if codec != 0x10:
        raise ValueError(f"codec 0x{codec:X} is not PS-ADPCM (0x10)")
    size = struct.unpack_from("<I", data, 0x24)[0]
    body = 0x28
    frames = size // chans // FRAME_BYTES
    pcm = [[] for _ in range(chans)]
    hist = [[0, 0] for _ in range(chans)]
    fpb = il // FRAME_BYTES
    for f in range(frames):
        block, in_block = divmod(f, fpb)
        for c in range(chans):
            src = body + (block * chans + c) * il + in_block * FRAME_BYTES
            decode_frame(data[src:src + FRAME_BYTES], hist[c], pcm[c])
    return rate, pcm


def write_ads(rate: int, pcm: list[list[int]], full_search: bool = True, progress=None) -> bytes:
    """Stereo 16-bit PCM -> .ads, laid out exactly like PD's files."""
    chans = len(pcm)
    fpb = INTERLEAVE // FRAME_BYTES
    data_frames = (len(pcm[0]) + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    frames = 1 + data_frames                      # PD puts one silent frame in front
    frames = (frames + fpb - 1) // fpb * fpb      # pad to whole interleave blocks
    per_channel = []
    for c in range(chans):
        buf = bytearray(frames * FRAME_BYTES)
        buf[0] = 0x0C
        buf[1] = 6
        hist = [0, 0]
        src = pcm[c]
        n = len(src)
        for f in range(1, frames):
            flag = 3 if f == frames - 1 else 2
            s0 = (f - 1) * FRAME_SAMPLES
            if s0 >= n:
                buf[f * FRAME_BYTES] = 0x0C
                buf[f * FRAME_BYTES + 1] = flag
                continue
            chunk = src[s0:s0 + FRAME_SAMPLES]
            if len(chunk) < FRAME_SAMPLES:
                chunk = list(chunk) + [0] * (FRAME_SAMPLES - len(chunk))
            buf[f * FRAME_BYTES:(f + 1) * FRAME_BYTES] = encode_frame(chunk, flag, hist, full_search)
            if progress and (f & 0x3FF) == 0:
                progress((c * frames + f) / (chans * frames))
        per_channel.append(bytes(buf))

    out = bytearray()
    out += b"SShd" + struct.pack("<IIIIIii", 0x18, 0x10, rate, chans, INTERLEAVE, -1, -1)
    out += b"SSbd" + struct.pack("<I", frames * FRAME_BYTES * chans)
    for b in range(frames // fpb):
        for c in range(chans):
            out += per_channel[c][b * INTERLEAVE:(b + 1) * INTERLEAVE]
    return bytes(out)


def samples_per_channel(ads: bytes) -> int:
    """What the marker table's `length` field must say for this .ads."""
    size, chans = struct.unpack_from("<I", ads, 0x24)[0], struct.unpack_from("<I", ads, 0x10)[0]
    return size // chans // FRAME_BYTES * FRAME_SAMPLES


def read_wav(path: str) -> tuple[int, list[list[int]]]:
    """16-bit PCM WAV -> (rate, [L, R]). Mono is duplicated. Other depths are rejected on purpose:
    the tool converts format, it does not resample or requantise - prepare the audio first."""
    with open(path, "rb") as f:
        d = f.read()
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    p, fmt, chans, rate, bits, data_at, data_len = 12, 0, 0, 0, 0, -1, 0
    while p + 8 <= len(d):
        cid = d[p:p + 4]
        ln = struct.unpack_from("<I", d, p + 4)[0]
        if cid == b"fmt ":
            fmt, chans, rate = struct.unpack_from("<HHI", d, p + 8)
            bits = struct.unpack_from("<H", d, p + 22)[0]
            if fmt == 0xFFFE:
                fmt = struct.unpack_from("<H", d, p + 32)[0]
        elif cid == b"data":
            data_at, data_len = p + 8, min(ln, len(d) - p - 8)
        p += 8 + ln + (ln & 1)
    if data_at < 0 or not chans:
        raise ValueError("WAV has no fmt/data chunk")
    if fmt != 1 or bits != 16:
        raise ValueError(f"need 16-bit PCM WAV (this one is format {fmt}, {bits}-bit)")
    n = data_len // (2 * chans)
    samples = struct.unpack_from(f"<{n * chans}h", d, data_at)
    if chans == 1:
        mono = list(samples)
        return rate, [mono, list(mono)]
    return rate, [list(samples[0::chans]), list(samples[1::chans])]


def write_wav(path: str, rate: int, pcm: list[list[int]]) -> None:
    n, ch = len(pcm[0]), len(pcm)
    inter = [0] * (n * ch)
    for c in range(ch):
        inter[c::ch] = pcm[c]
    body = struct.pack(f"<{n * ch}h", *inter)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * ch * 2, ch * 2, 16))
        f.write(b"data" + struct.pack("<I", len(body)) + body)
