"""Gran Turismo music.ins / *.ins (INST) — instrument / sample bank.

Header (GT2 / GT3 sequenced banks)
----------------------------------
  0x00  \'INST\'
  0x04  0
  0x08  sample_body_offset   (also mirrored at 0x14)
  0x0C  0
  0x10  sample_body_size
  0x14  sample_body_offset
  0x18  program / tone count (informational)
  0x1C  entry size (usually 0x28)
  0x20… program / tone attribute table
  body  raw SPU-ADPCM frames (16 bytes each)

Sample body is a concatenation of SPU-ADPCM streams. Frame flag bits:
  bit0 = loop/end marker (end of this sample)
  bit1 = loop
  bit2 = loop start

Extraction splits the body on end-of-sample markers, decodes each region
to 16-bit PCM, and writes WAV (and optionally VAG) files.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from gt3bgm.psadpcm import decode_frame, FRAME_BYTES, FRAME_SAMPLES, write_wav

MAGIC = b"INST"
DEFAULT_RATE = 22050  # typical SPU rate for menu/instrument banks


@dataclass
class Sample:
    index: int
    offset: int          # byte offset within sample body
    size: int            # bytes (multiple of 16)
    loop_start: int = -1 # byte offset within sample, or -1
    pcm: list[int] = field(default_factory=list)

    @property
    def frames(self) -> int:
        return self.size // FRAME_BYTES

    @property
    def duration_sec(self) -> float:
        if self.pcm:
            return len(self.pcm) / DEFAULT_RATE
        return self.frames * FRAME_SAMPLES / DEFAULT_RATE


@dataclass
class Inst:
    raw: bytes = b""
    body_offset: int = 0
    body_size: int = 0
    program_count: int = 0
    samples: list[Sample] = field(default_factory=list)

    @classmethod
    def read(cls, data: bytes) -> "Inst":
        if data[:4] != MAGIC:
            raise ValueError(f"Not an INST file (got {data[:4]!r})")
        body_off = struct.unpack_from("<I", data, 0x14)[0]
        body_size = struct.unpack_from("<I", data, 0x10)[0]
        if body_off == 0 or body_off >= len(data):
            body_off = struct.unpack_from("<I", data, 0x08)[0]
        if body_size == 0 or body_off + body_size > len(data):
            body_size = max(0, len(data) - body_off)
        count = struct.unpack_from("<I", data, 0x18)[0]
        obj = cls(raw=data, body_offset=body_off, body_size=body_size, program_count=count)
        obj.samples = obj._split_samples()
        return obj

    @classmethod
    def open(cls, path: str) -> "Inst":
        with open(path, "rb") as f:
            return cls.read(f.read())

    def write(self) -> bytes:
        return self.raw

    @property
    def size(self) -> int:
        return len(self.raw)

    def _body(self) -> bytes:
        return self.raw[self.body_offset: self.body_offset + self.body_size]

    def _split_samples(self) -> list[Sample]:
        body = self._body()
        if len(body) < FRAME_BYTES:
            return []
        samples: list[Sample] = []
        start = 0
        loop_start = -1
        idx = 0
        limit = (len(body) // FRAME_BYTES) * FRAME_BYTES
        for off in range(0, limit, FRAME_BYTES):
            flags = body[off + 1]
            if flags & 0x04:
                loop_start = off - start
            if flags & 0x01:
                end = off + FRAME_BYTES
                size = end - start
                if size >= FRAME_BYTES:
                    samples.append(Sample(
                        index=idx, offset=start, size=size,
                        loop_start=loop_start if loop_start >= 0 else -1,
                    ))
                    idx += 1
                start = end
                loop_start = -1
        if start < limit:
            size = limit - start
            if size >= FRAME_BYTES:
                samples.append(Sample(
                    index=idx, offset=start, size=size,
                    loop_start=loop_start if loop_start >= 0 else -1,
                ))
        return samples

    def decode_sample(self, sample: Sample, rate: int = DEFAULT_RATE) -> list[int]:
        body = self._body()
        chunk = body[sample.offset: sample.offset + sample.size]
        pcm: list[int] = []
        hist = [0, 0]
        for i in range(0, len(chunk) - FRAME_BYTES + 1, FRAME_BYTES):
            decode_frame(chunk[i:i + FRAME_BYTES], hist, pcm)
        sample.pcm = pcm
        return pcm

    def extract_all(
        self,
        out_dir: str,
        rate: int = DEFAULT_RATE,
        also_vag: bool = True,
        progress=None,
    ) -> list[str]:
        os.makedirs(out_dir, exist_ok=True)
        written: list[str] = []
        n = max(1, len(self.samples))
        body = self._body()
        for i, sample in enumerate(self.samples):
            pcm = self.decode_sample(sample, rate=rate)
            wav_path = os.path.join(out_dir, f"sample_{i:03d}.wav")
            write_wav(wav_path, rate, [pcm])
            written.append(wav_path)
            if also_vag:
                vag_path = os.path.join(out_dir, f"sample_{i:03d}.vag")
                _write_vag(
                    vag_path,
                    body[sample.offset: sample.offset + sample.size],
                    rate,
                    name=f"sample_{i:03d}",
                )
                written.append(vag_path)
            if progress:
                progress((i + 1) / n)
        return written

    def extract_sf2(self, path: str, rate: int = DEFAULT_RATE, progress=None) -> str:
        """Build a SoundFont 2 file from all samples in this bank."""
        samples_pcm = []
        n = max(1, len(self.samples))
        for i, sample in enumerate(self.samples):
            pcm = self.decode_sample(sample, rate=rate)
            samples_pcm.append((f"sample_{i:03d}", pcm, rate))
            if progress:
                progress((i + 1) / n)
        write_sf2(path, samples_pcm)
        return path


def _write_vag(path: str, adpcm: bytes, rate: int, name: str = "") -> None:
    name_bytes = (name or "sample")[:16].encode("ascii", errors="replace")
    name_bytes = name_bytes + b"\0" * (16 - len(name_bytes))
    hdr = bytearray(48)
    hdr[0:4] = b"VAGp"
    struct.pack_into(">I", hdr, 4, 0x20)
    struct.pack_into(">I", hdr, 0x0C, len(adpcm))
    struct.pack_into(">I", hdr, 0x10, rate)
    hdr[0x20:0x30] = name_bytes
    with open(path, "wb") as f:
        f.write(hdr)
        f.write(adpcm)


def write_sf2(path: str, samples_pcm: list[tuple[str, list[int], int]], bank: int = 0) -> None:
    """Write a minimal SoundFont 2.04 file.

    samples_pcm: list of (name, mono_pcm_int16_list, sample_rate)
    Each sample becomes one instrument + one preset (program = index % 128).
    """
    import struct
    import os

    def u16(v): return struct.pack("<H", v & 0xFFFF)
    def u32(v): return struct.pack("<I", v & 0xFFFFFFFF)
    def s16(v):
        v = max(-32768, min(32767, int(v)))
        return struct.pack("<h", v)

    # --- sample data: each sample + 46 zero samples of padding (SF2 spec) ---
    smpl = bytearray()
    shdr_entries = []  # (name, start, end, startloop, endloop, rate)
    pos = 0
    for name, pcm, rate in samples_pcm:
        if not pcm:
            continue
        start = pos
        for s in pcm:
            smpl += s16(s)
        # 46 zero samples padding
        smpl += b"\x00\x00" * 46
        end = start + len(pcm)
        # loop whole sample if short; SF2 wants startloop < endloop
        startloop = start
        endloop = max(start + 1, end - 1)
        shdr_entries.append((name[:20], start, end, startloop, endloop, rate))
        pos = end + 46

    if not shdr_entries:
        raise ValueError("No samples to write into SF2")

    # Terminal sample header
    shdr_entries.append(("EOS", 0, 0, 0, 0, 0))

    def padded_name(n: str, size: int = 20) -> bytes:
        b = n.encode("ascii", errors="replace")[:size - 1]
        return b + b"\x00" * (size - len(b))

    # --- pdta chunks ---
    # phdr: presets — one per sample, bank/program
    phdr = bytearray()
    for i, (name, *_) in enumerate(shdr_entries[:-1]):
        phdr += padded_name(name)
        phdr += u16(i % 128)          # preset (program)
        phdr += u16(bank)             # bank
        phdr += u16(i)                # pbag index
        phdr += u32(0) + u32(0) + u32(0)  # library, genre, morphology
    # terminal
    phdr += padded_name("EOP")
    phdr += u16(0) + u16(0) + u16(len(shdr_entries) - 1)
    phdr += u32(0) + u32(0) + u32(0)

    # pbag: one bag per preset + terminal
    pbag = bytearray()
    for i in range(len(shdr_entries) - 1):
        pbag += u16(i)  # gen index
        pbag += u16(0)  # mod index
    pbag += u16(len(shdr_entries) - 1) + u16(0)

    # pmod: empty terminal
    pmod = u16(0) * 5

    # pgen: instrument index for each preset + terminal
    pgen = bytearray()
    for i in range(len(shdr_entries) - 1):
        pgen += u16(41)   # instrument operator
        pgen += u16(i)    # instrument index
    pgen += u16(0) + u16(0)

    # inst: one instrument per sample + terminal
    inst = bytearray()
    for i, (name, *_) in enumerate(shdr_entries[:-1]):
        inst += padded_name(name)
        inst += u16(i)  # ibag index
    inst += padded_name("EOI")
    inst += u16(len(shdr_entries) - 1)

    # ibag
    ibag = bytearray()
    for i in range(len(shdr_entries) - 1):
        ibag += u16(i) + u16(0)
    ibag += u16(len(shdr_entries) - 1) + u16(0)

    # imod terminal
    imod = u16(0) * 5

    # igen: sampleID for each instrument + terminal
    igen = bytearray()
    for i in range(len(shdr_entries) - 1):
        igen += u16(53)  # sampleID
        igen += u16(i)
    igen += u16(0) + u16(0)

    # shdr
    shdr = bytearray()
    for name, start, end, startloop, endloop, rate in shdr_entries:
        shdr += padded_name(name)
        shdr += u32(start) + u32(end) + u32(startloop) + u32(endloop)
        shdr += u32(rate)
        shdr += bytes([60, 0, 0, 0])  # originalPitch=60, pitchCorrection=0, link=0, type=0 (mono)

    def list_chunk(list_id: bytes, parts: list[tuple[bytes, bytes]]) -> bytes:
        body = bytearray()
        for ck_id, data in parts:
            body += ck_id + u32(len(data)) + data
            if len(data) & 1:
                body += b"\x00"
        return b"LIST" + u32(len(list_id) + len(body)) + list_id + body

    info = list_chunk(b"INFO", [
        (b"ifil", u16(2) + u16(4)),  # SF2.04
        (b"isng", b"EMU8000\x00"),
        (b"INAM", b"GT Instruments\x00"),
    ])
    # pad INAM if needed - already null terminated; ensure even
    # rebuild info carefully
    info_parts = [
        (b"ifil", u16(2) + u16(4)),
        (b"isng", b"EMU8000\x00\x00"),
        (b"INAM", b"GT Instruments\x00\x00"),
    ]
    info = list_chunk(b"INFO", info_parts)

    # sdta
    smpl_data = bytes(smpl)
    if len(smpl_data) & 1:
        smpl_data += b"\x00"
    sdta = list_chunk(b"sdta", [(b"smpl", smpl_data)])

    pdta = list_chunk(b"pdta", [
        (b"phdr", bytes(phdr)),
        (b"pbag", bytes(pbag)),
        (b"pmod", bytes(pmod)),
        (b"pgen", bytes(pgen)),
        (b"inst", bytes(inst)),
        (b"ibag", bytes(ibag)),
        (b"imod", bytes(imod)),
        (b"igen", bytes(igen)),
        (b"shdr", bytes(shdr)),
    ])

    riff_body = b"sfbk" + info + sdta + pdta
    riff = b"RIFF" + u32(len(riff_body)) + riff_body
    with open(path, "wb") as f:
        f.write(riff)
