# GT3BGMTool

Add your own music to **Gran Turismo 3** (PS2), or manage what is already there.

GT3BGMTool converts a WAV file into the PS2's streamed audio format, adds it to the game's music list, and can set the camera cuts used by the replay menu's *sync to beat* option. It also has a second tab for the game's sequenced (MIDI-style) menu and dealer music.


## Features

**Race BGM** (`data/bgm/ads.inf` and `.ads` files)

- Add new songs from a WAV or an Audacity project, with a title and artist
- Set replay camera cuts from an Audacity project, a tempo grid, or an Audacity label file
- Replace the audio of an existing song, re-time its cuts, rename it, or remove it from the list
- Save any song out of the game as WAV (or MP3 if `ffmpeg` is installed)
- Checks your build before you use it, so mistakes show up here rather than in game

**Sequenced music** (`data/music/music.inf`, `music.seq`, `music.ins`)

- List tracks and export sequences as MIDI, one at a time or all at once
- Replace a sequence from a MIDI file
- Extract the instrument samples (WAV and VAG) and build a SoundFont (`.sf2`)

Standard library only. There is nothing to `pip install`.

## Requirements

- **Python 3.8 or newer** with Tkinter (included in the Windows and macOS installers from python.org; on Linux install `python3-tk`)
- **Your game's extracted files.** You need to be able to see `data/bgm/ads.inf` on your computer
- **Silentwarior112's [PS2DL](https://github.com/Silentwarior112/PS2DL) tool**, to rebuild the ISO once your files are in place
- *Optional:* [Audacity](https://www.audacityteam.org/) for preparing audio and cut points
- *Optional:* [ffmpeg](https://ffmpeg.org/) on your PATH, only if you want to export songs as MP3

> **Back up first.** Copy your original `data/bgm/` folder somewhere safe before you start.

## Quick start: adding a race song

### 1. Prepare the audio

The audio must be a **16-bit PCM, 44.1 kHz, stereo WAV**. The tool converts the *format* to PS-ADPCM, but it does not resample or master your audio. If the file is the wrong sample rate, the tool will tell you and stop.

### 2. Set up camera cuts (optional)

The game uses a list of cut points for each song to time the camera in the replay menu's *sync to beat* mode. The easiest way to make them:

1. Open your song in Audacity.
2. Split the track at each place you want a cut (`Ctrl+I`).
3. Save the project as `.aup3`, and also export the audio with **File → Export → WAV (16-bit PCM)**.

When you add the song, pick the `.aup3`. The tool reads the splits from the project and then asks for the exported WAV. Keep the WAV next to the project and it will be found automatically.

Other ways to set cuts, all in the same dialog:

| Option | What it does |
| --- | --- |
| From the project | Uses your clip splits (and any label tracks) from the `.aup3`, and can also mark beats and bars from the project tempo |
| From a tempo | You give the BPM, the time of the first beat, and how many bars between cuts |
| From an Audacity label file | Exported labels named `cut`, `bar` or `beat` become cuts, bars and beats |
| None | No cuts. The game falls back to its own timer |

### 3. Add the song

1. Double-click **`GT3BGMTool.bat`** (Windows), or run `python gt3bgmtool.py` (any platform).
2. On the **Race BGM** tab, click **Open ads.inf…** and choose the `ads.inf` in your game's `data/bgm/` folder.
3. Click **Add new music…**, choose your WAV or `.aup3`, and give it a title and artist.
4. Choose how the camera cuts are made (see above) and click **Add**. The song appears in the list in green.

### 4. Export

1. Click **Export files…** and choose an output folder. Encoding runs in the background, so watch the progress bar and log while it works.
2. The tool writes `ads.inf` plus `<name>.ads` and `i_<name>.ads` for each song you added or changed, and then checks the result. Only use the build if it reports **Everything checks out**.
3. **Copy those files into the game's `data/bgm/` folder.** Export does not touch your game files itself.
4. Rebuild the ISO with PS2DL.

### 5. Test in game

GT3 stores the music list in your save and only rebuilds it from the defaults. Go to **Options → Back to Default Settings** once, and your new songs will appear in the music list. Without this step the list will not change.

## Editing existing songs

Select a song in the list, then use the **Selected song** buttons:

| Button | What it does |
| --- | --- |
| Save audio as… | Export the song to WAV (or MP3 if ffmpeg is available) |
| Replace audio… | Swap in new audio and keep the title and artist |
| Re-time cuts… | Set new camera cuts without touching the audio |
| Remove from list | Drop the song from the music list |
| Rename | Change the title and artist shown in game |

Rows are coloured to show state: green for new, orange for edited, red for a song whose `.ads` file is missing from the folder.

## Sequenced music

The **Sequenced music** tab works on the menu and dealer music in `data/music/`.

1. Click **Open music folder…** and choose the folder containing `music.inf`, `music.seq` and `music.ins`.
2. Click **Back up originals…** and choose a folder. The tool makes a new dated folder inside it and copies your original files there, checking each copy matches. Do this before anything else.
3. Use **Export MIDI…** or **Export all MIDI…** to get the sequences out.
4. Use **Replace sequence from MIDI…** to swap one in, then **Save files…** and choose a **different folder**. The tool never writes over the folder you opened, so your originals are safe. It refuses to save into the same folder.
5. **Extract instruments…** and **Build SoundFont…** pull the instrument samples out of `music.ins`.

### Command line

The same features are available without the GUI:

```
python gt3seqtool.py -d path/to/music -o my_backups backup
python gt3seqtool.py -d path/to/music list
python gt3seqtool.py -d path/to/music info
python gt3seqtool.py -d path/to/music export-midi 0 main01.mid
python gt3seqtool.py -d path/to/music export-all-midi midi_out
python gt3seqtool.py -d path/to/music -o my_output replace-seq 0 new_song.mid
python gt3seqtool.py -d path/to/music -o my_output save
python gt3seqtool.py -d path/to/music extract-instruments instruments_out
python gt3seqtool.py -d path/to/music build-sf2 sf2_out
```

## Limits and things to know

- The in-game **Favorite Music List** holds a maximum of **64 songs**.
- Every song's file name must be **unique**. The game identifies songs by a hash of the file name.
- The tool refuses to open an `ads.inf` it cannot rebuild byte for byte. If you get that warning, please report the file.
- Saving sequenced music always writes all three files (`music.inf`, `music.seq`, `music.ins`) to the folder you choose. Anything you did not replace is copied byte-for-byte, so opening and saving without changes gives back identical files.
- Race BGM audio is never resampled or normalised for you. Prepare it in Audacity first.
- The `.bat` launcher is Windows-only. On macOS and Linux, run `python3 gt3bgmtool.py`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "This WAV is … Hz. GT3 wants 44100 Hz" | Resample to 44.1 kHz in Audacity (set the project rate, then export) |
| "Prepare the audio as 16-bit PCM WAV first" | Export from Audacity as **WAV (Microsoft) signed 16-bit PCM** |
| New songs do not appear in game | Run **Options → Back to Default Settings** once, and check the files were copied to `data/bgm/` |
| Song shows red / "no .ads in this folder" | The `.ads` file is not in the same folder as `ads.inf` |
| MP3 export gives a WAV instead | Install `ffmpeg` and make sure it is on your PATH |
| Double-clicking the `.bat` says Python was not found | Install Python from python.org and tick **Add python.exe to PATH** |
| Cuts are missing from the project | Check the track is actually split (Audacity shows a line at each split) |

## Checking the tool itself

```
python selftest.py
python selftest.py song.aup3     # also parses a real Audacity project
```

This uses data the tool builds itself, so no game files are needed. It proves the code works, but it is not a substitute for testing with real game files.

## Credits and licence

Released under the terms in [LICENSE](LICENSE). Uses Python's standard library only.

Rebuilding the ISO relies on Silentwarior112's PS2DL tool.