"""GT3BGMTool - the formats behind Gran Turismo 3's music, as plain Python.

Modules:
    psadpcm   the PS2's PS-ADPCM codec, the .ads container and WAV
    markers   the beat-marker tables that drive the replay camera cuts
    adsinf    data/bgm/ads.inf, the song index
    audacity  reading cut points out of an .aup3 project
    export    saving a song out to WAV, or MP3 where ffmpeg exists
    verify    the checks that run before the tool says a build is safe
    mseq      data/music/music.inf (MSEQ sequenced song index)
    seqg      data/music/music.seq (SEQG multi-sequence container)
    inst      data/music/music.ins (INST instrument bank, pass-through)

Nothing here needs a third-party package.
"""

__version__ = "1.1.0-seq"
