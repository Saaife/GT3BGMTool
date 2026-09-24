"""Saving a song out of the game, to listen to or to archive.

WAV is written here, by hand, and always works. MP3 needs an encoder, and bundling one would cost this tool
its "standard library only" promise - so if ffmpeg happens to be on PATH we hand the WAV to it, and if it is
not, we say so plainly rather than writing a file that is secretly a WAV with the wrong extension.
"""

from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
from . import psadpcm as ps


def have_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def save(path: str, rate: int, pcm: list[list[int]], bitrate: str = "192k") -> str:
    """Write the audio to `path`. Returns a line describing what happened."""
    if not path.lower().endswith(".mp3"):
        ps.write_wav(path, rate, pcm)
        return f"wrote {os.path.basename(path)}"

    ffmpeg = have_ffmpeg()
    if not ffmpeg:
        alt = os.path.splitext(path)[0] + ".wav"
        ps.write_wav(alt, rate, pcm)
        return (f"ffmpeg is not on PATH, so MP3 is not available - wrote {os.path.basename(alt)} instead. "
                "Install ffmpeg, or convert the WAV yourself.")

    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        ps.write_wav(tmp, rate, pcm)
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", tmp, "-b:a", bitrate, path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"ffmpeg exited {r.returncode}")
        return f"wrote {os.path.basename(path)} via ffmpeg at {bitrate}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
