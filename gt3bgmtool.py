"""GT3BGMTool - manage the music in Gran Turismo 3.

Two modes:
  Race BGM   — data/bgm/ads.inf + .ads files (streamed PS-ADPCM, camera cuts)
  Sequenced  — data/music/music.inf + music.seq + music.ins (menu / dealer music)

Standard library only: no pip install.

Race BGM audio must already be a 16-bit 44.1 kHz stereo WAV - this tool converts the FORMAT (to the PS2's
PS-ADPCM .ads), it does not resample or master for you. Cut points can come straight from an Audacity project.

Sequenced mode can list tracks, export sequences as MIDI, and replace a sequence from a MIDI file.
"""

from __future__ import annotations
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from gt3bgm import __version__
from gt3bgm.adsinf import AdsInf, Song, RACE_GROUP, MUSIC_LIST_CAP
from gt3bgm.markers import MarkerSet
from gt3bgm.audacity import Project
from gt3bgm import psadpcm as ps
from gt3bgm import export as ex
from gt3bgm import verify as vfy
from gt3bgm.mseq import Mseq, MseqSong
from gt3bgm.seqg import SeqG, sequence_to_midi, midi_to_sequence, check_rebuild
from gt3bgm.inst import Inst
from gt3bgm.backup import backup_files

APP = "GT3BGMTool"
REMINDER = ("After copying the files into data/bgm/, run Options → \"Back to Default Settings\" once. "
            "GT3 keeps the music list in your save and only rebuilds it from defaults - without that step "
            "the list you see in game will not match the one you just built.")


class CutsFrame(ttk.LabelFrame):
    """Where the camera cuts come from. Used when adding a song and when re-timing one."""

    def __init__(self, master, project: Project | None = None):
        super().__init__(master, text="Camera cuts", padding=8)
        self.project = project
        cuts = project.cut_times() if project else []
        self.mode = tk.StringVar(value="project" if len(cuts) > 1 else "grid")
        self.bpm = tk.StringVar(value=f"{project.tempo:g}" if project and project.tempo else "120")
        self.first = tk.StringVar(value="0.0")
        self.bars = tk.StringVar(value="2")
        self.labels = tk.StringVar(value="")
        self.also_beats = tk.BooleanVar(value=True)

        r = 0
        if project:
            found = project.label_times()
            what = f"{len(cuts)} clip split(s)"
            if found:
                what += " + " + ", ".join(f"{len(v)} '{k}'" for k, v in sorted(found.items()))
            ttk.Radiobutton(self, text=f"From the project — {what}", value="project",
                            variable=self.mode).grid(column=0, row=0, sticky="w", columnspan=4)
            tempo = (f"also mark beats and bars from the project tempo ({project.tempo:g} BPM)"
                     if project.tempo else "also mark beats and bars from the tempo below")
            ttk.Checkbutton(self, variable=self.also_beats, text=tempo).grid(
                column=0, row=1, sticky="w", columnspan=4, padx=(20, 0), pady=(0, 6))
            r = 2

        ttk.Radiobutton(self, text="From a tempo", value="grid", variable=self.mode).grid(
            column=0, row=r, sticky="w", columnspan=4)
        ttk.Label(self, text="BPM").grid(column=0, row=r + 1, sticky="e", padx=(20, 4))
        ttk.Entry(self, textvariable=self.bpm, width=7).grid(column=1, row=r + 1, sticky="w")
        ttk.Label(self, text="first beat (s)").grid(column=2, row=r + 1, sticky="e", padx=(10, 4))
        ttk.Entry(self, textvariable=self.first, width=7).grid(column=3, row=r + 1, sticky="w")
        ttk.Label(self, text="cut every N bars").grid(column=2, row=r + 2, sticky="e", padx=(10, 4))
        ttk.Entry(self, textvariable=self.bars, width=7).grid(column=3, row=r + 2, sticky="w")

        ttk.Radiobutton(self, text="From an Audacity label file", value="labels", variable=self.mode).grid(
            column=0, row=r + 3, sticky="w", columnspan=4, pady=(8, 0))
        ttk.Entry(self, textvariable=self.labels, width=34).grid(
            column=0, row=r + 4, columnspan=3, sticky="w", padx=(20, 4))
        ttk.Button(self, text="Browse…", command=self._browse).grid(column=3, row=r + 4, sticky="w")
        ttk.Label(self, text="labels named cut / bar / beat", foreground="#666").grid(
            column=0, row=r + 5, columnspan=4, sticky="w", padx=(20, 0))

        ttk.Radiobutton(self, text="None - the game falls back to its own timer", value="none",
                        variable=self.mode).grid(column=0, row=r + 6, sticky="w", columnspan=4, pady=(8, 0))

    def _browse(self):
        p = filedialog.askopenfilename(title="Audacity label file", filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if p:
            self.labels.set(p)
            self.mode.set("labels")

    def problem(self) -> str | None:
        if self.mode.get() == "grid":
            try:
                float(self.bpm.get()), float(self.first.get()), int(self.bars.get())
            except ValueError:
                return "BPM, first beat and bars need to be numbers."
        if self.mode.get() == "labels" and not os.path.isfile(self.labels.get()):
            return "Pick a label file, or choose another way to time the cuts."
        return None

    def values(self) -> dict:
        return {"mode": self.mode.get(), "bpm": self.bpm.get(), "first": self.first.get(),
                "bars": self.bars.get(), "labels": self.labels.get(),
                "project": self.project, "also_beats": self.also_beats.get()}


def build_markers(opt: dict, rate: int, length: int, log=lambda s: None) -> MarkerSet:
    """Turn the cut settings into a marker table for a song of `length` samples."""
    table = MarkerSet(rate, length)
    mode = opt["mode"]
    if mode == "project":
        project: Project = opt["project"]
        labels = project.label_times()
        cuts = sorted(set(project.cut_times()) | set(labels.get("cut", [])))
        if opt["also_beats"]:
            bpm = project.tempo or float(opt["bpm"] or 0)
            if bpm > 0:
                table.grid(bpm, 0.0, project.beats_per_bar, 2, with_cuts=False)
                log(f"  beats and bars from {bpm:g} BPM")
        for name, chan in (("beat", 1), ("bar", 2)):
            if labels.get(name):
                table.set_times(chan, labels[name])
        for name, times in labels.items():                  # ch5-9 for the unused director channels
            if name.startswith("ch") and name[2:].isdigit():
                table.set_times(int(name[2:]), times)
        table.set_cuts(cuts)
        log(f"  {len(table.ch[3])} camera cut(s) from the project")
    elif mode == "grid":
        table.grid(float(opt["bpm"]), float(opt["first"]), 4, max(1, int(opt["bars"])))
    elif mode == "labels" and opt["labels"]:
        n = table.load_labels(opt["labels"])
        log(f"  read {n} markers from the label file")
    return table


def pick_audio(parent) -> tuple[str, Project | None, int, list] | None:
    """Ask for an Audacity project or a WAV and return the audio plus the project, if there was one."""
    picked = filedialog.askopenfilename(
        title="Choose an Audacity project or a 16-bit 44.1 kHz stereo WAV",
        filetypes=[("Audacity project or WAV", "*.aup3 *.wav"), ("Audacity project", "*.aup3"),
                   ("WAV audio", "*.wav"), ("All files", "*.*")])
    if not picked:
        return None
    project, wav = None, picked
    if picked.lower().endswith(".aup3"):
        try:
            project = Project(picked)
        except Exception as e:
            messagebox.showerror(APP, f"Could not read that project:\n{e}")
            return None
        if project.rate != 44100:
            messagebox.showerror(APP, f"This project runs at {project.rate} Hz. GT3 wants 44100 - set the "
                                      "project rate in Audacity, then export.")
            return None
        wav = project.find_wav()
        if not wav:
            messagebox.showinfo(APP, "Found the cut points in that project.\n\nNow point me at the WAV you "
                                     "exported from it (File → Export → WAV, 16-bit PCM). The project "
                                     "stores 32-bit float behind Audacity's mixer and effects, so the export "
                                     "is the only faithful copy of what you hear.\n\nPut the WAV next to the "
                                     "project under the same name and it will be picked up automatically.")
            wav = filedialog.askopenfilename(title="The WAV exported from that project",
                                             filetypes=[("WAV audio", "*.wav"), ("All files", "*.*")])
            if not wav:
                return None
    try:
        rate, pcm = ps.read_wav(wav)
    except Exception as e:
        messagebox.showerror(APP, f"{e}\n\nPrepare the audio as 16-bit PCM WAV first.")
        return None
    if rate != 44100:
        messagebox.showerror(APP, f"This WAV is {rate} Hz. GT3 wants 44100 Hz - resample it first, "
                                  "then bring it back here.")
        return None
    return wav, project, rate, pcm


class AddSongDialog(tk.Toplevel):
    """Name the song and choose how its camera cuts are timed."""

    def __init__(self, master, wav_path: str, seconds: float, project: Project | None = None):
        super().__init__(master)
        self.title("Add music")
        self.resizable(False, False)
        self.result = None
        base = project.name if project else os.path.splitext(os.path.basename(wav_path))[0]
        safe = "".join(ch for ch in base.lower().replace(" ", "_") if ch.isalnum() or ch == "_")[:14] or "song"

        self.name = tk.StringVar(value=safe)
        self.title_ = tk.StringVar(value=base[:32])
        self.artist = tk.StringVar(value="")

        f = ttk.Frame(self, padding=12)
        f.grid(sticky="nsew")
        head = f"{os.path.basename(wav_path)}  •  {seconds:.1f} s"
        if project:
            head += f"\n{os.path.basename(project.path)}  •  {project.summary()}"
        ttk.Label(f, text=head).grid(column=0, row=0, columnspan=3, sticky="w", pady=(0, 10))

        for i, (label, var, hint) in enumerate((
                ("File name", self.name, "letters, digits and _ ; must be unique"),
                ("Title", self.title_, "shown in the music list"),
                ("Artist", self.artist, "shown next to the title"))):
            ttk.Label(f, text=label).grid(column=0, row=1 + i, sticky="w", pady=2)
            ttk.Entry(f, textvariable=var, width=28).grid(column=1, row=1 + i, sticky="w", pady=2)
            ttk.Label(f, text=hint, foreground="#666").grid(column=2, row=1 + i, sticky="w", padx=(8, 0))

        self.cuts = CutsFrame(f, project)
        self.cuts.grid(column=0, row=4, columnspan=3, sticky="ew", pady=(12, 0))

        btns = ttk.Frame(f)
        btns.grid(column=0, row=5, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).grid(column=0, row=0, padx=4)
        ttk.Button(btns, text="Add", command=self._ok).grid(column=1, row=0)

        self.transient(master)
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        name = self.name.get().strip().lower()
        if not name or not all(c.isalnum() or c == "_" for c in name):
            messagebox.showerror(APP, "The file name needs to be letters, digits or _.", parent=self)
            return
        bad = self.cuts.problem()
        if bad:
            messagebox.showerror(APP, bad, parent=self)
            return
        self.result = dict(self.cuts.values(), name=name, title=self.title_.get().strip() or name,
                           artist=self.artist.get().strip())
        self.destroy()


class RetimeDialog(tk.Toplevel):
    """Re-do the camera cuts of a song that is already in the list."""

    def __init__(self, master, song_label: str, project: Project | None):
        super().__init__(master)
        self.title("Re-time cuts")
        self.resizable(False, False)
        self.result = None

        f = ttk.Frame(self, padding=12)
        f.grid(sticky="nsew")
        ttk.Label(f, text=song_label).grid(column=0, row=0, sticky="w", pady=(0, 10))
        self.cuts = CutsFrame(f, project)
        self.cuts.grid(column=0, row=1, sticky="ew")
        btns = ttk.Frame(f)
        btns.grid(column=0, row=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).grid(column=0, row=0, padx=4)
        ttk.Button(btns, text="Apply", command=self._ok).grid(column=1, row=0)

        self.transient(master)
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        bad = self.cuts.problem()
        if bad:
            messagebox.showerror(APP, bad, parent=self)
            return
        self.result = self.cuts.values()
        self.destroy()


class SeqMusicFrame(ttk.Frame):
    """Tab for GT3 sequenced menu music (music.inf / music.seq / music.ins)."""

    def __init__(self, master, log_fn):
        super().__init__(master, padding=8)
        self.log_fn = log_fn
        self.mseq: Mseq | None = None
        self.seqg: SeqG | None = None
        self.inst: Inst | None = None
        self.music_dir = ""
        self.dirty = False
        # GT2-style: list of (name, SeqG) and (name, Inst) when no music.inf pack is present
        self.gt2_seqs: list[tuple[str, SeqG]] = []
        self.gt2_insts: list[tuple[str, Inst]] = []
        self.mode = "gt3"  # "gt3" or "gt2"

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        bar = ttk.Frame(self)
        bar.grid(column=0, row=0, sticky="ew")
        ttk.Button(bar, text="Open music folder…", command=self.open_folder).grid(column=0, row=0)
        self.export_mid_btn = ttk.Button(bar, text="Export MIDI…", command=self.export_midi, state="disabled")
        self.export_mid_btn.grid(column=1, row=0, padx=6)
        self.export_all_btn = ttk.Button(bar, text="Export all MIDI…", command=self.export_all_midi, state="disabled")
        self.export_all_btn.grid(column=2, row=0)
        self.replace_btn = ttk.Button(bar, text="Replace sequence from MIDI…", command=self.replace_seq, state="disabled")
        self.replace_btn.grid(column=3, row=0, padx=6)
        self.save_btn = ttk.Button(bar, text="Save files…", command=self.save_files, state="disabled")
        self.save_btn.grid(column=4, row=0)
        self.extract_ins_btn = ttk.Button(bar, text="Extract instruments…", command=self.extract_instruments, state="disabled")
        self.extract_ins_btn.grid(column=5, row=0, padx=6)
        self.sf2_btn = ttk.Button(bar, text="Build SoundFont…", command=self.build_soundfont, state="disabled")
        self.sf2_btn.grid(column=6, row=0)
        self.backup_btn = ttk.Button(bar, text="Back up originals…", command=self.backup_originals, state="disabled")
        self.backup_btn.grid(column=7, row=0, padx=6)
        self.path_lbl = ttk.Label(bar, text="no folder open", foreground="#666")
        self.path_lbl.grid(column=8, row=0, padx=12, sticky="w")

        info = ttk.LabelFrame(self, text="Sequence set", padding=6)
        info.grid(column=0, row=1, sticky="ew", pady=(8, 0))
        self.info_lbl = ttk.Label(info, text="Open a GT3 music/ folder (music.inf+seq+ins).")
        self.info_lbl.grid(column=0, row=0, sticky="w")

        cols = ("idx", "name", "title", "artist", "seq", "bpm")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=16, selectmode="browse")
        for c, w, h in zip(cols, (40, 120, 120, 140, 50, 50),
                           ("#", "Name", "Title", "Artist", "Seq", "BPM")):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(column=0, row=2, sticky="nsew", pady=(8, 0))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        sb.grid(column=1, row=2, sticky="ns", pady=(8, 0))
        self.tree.configure(yscrollcommand=sb.set)

        hint = ttk.Label(self, text="GT3: music.inf + music.seq + music.ins.  "
                         "GT2: any folder of .seq / .ins files (SEQG/INST).  "
                         "Race BGM is on the other tab.",
                         foreground="#666", wraplength=900)
        hint.grid(column=0, row=3, sticky="w", pady=(8, 0))

    def _set_btns(self, state: str):
        for b in (self.export_mid_btn, self.export_all_btn, self.replace_btn,
                  self.save_btn, self.extract_ins_btn, self.sf2_btn, self.backup_btn):
            b.configure(state=state)

    def open_folder(self):
        d = filedialog.askdirectory(title="GT3 music/ folder, or GT2 folder of .seq/.ins files")
        if not d:
            return
        inf_p = os.path.join(d, "music.inf")
        seq_p = os.path.join(d, "music.seq")
        ins_p = os.path.join(d, "music.ins")

        # Prefer GT3 pack if present
        if os.path.isfile(inf_p) and os.path.isfile(seq_p) and os.path.isfile(ins_p):
            try:
                mseq = Mseq.open(inf_p)
                seqg = SeqG.open(seq_p)
                inst = Inst.open(ins_p)
            except Exception as e:
                messagebox.showerror(APP, f"Could not read the GT3 music files:\n{e}")
                return
            self.mode = "gt3"
            self.mseq, self.seqg, self.inst = mseq, seqg, inst
            self.gt2_seqs, self.gt2_insts = [], []
            self.music_dir = d
            self.dirty = False
            self.path_lbl.configure(text=d)
            self.info_lbl.configure(
                text=f"GT3 pack — {len(mseq.songs)} songs  ·  {seqg.sequence_count()} sequences  ·  "
                     f"instrument bank {inst.size:,} bytes")
            self._set_btns("normal")
            self.refresh()
            self.log_fn(f"Opened GT3 sequenced music in {d}")
            return

        # GT2 / loose files: any *.seq (SEQG) and *.ins (INST)
        seq_files = sorted(
            f for f in os.listdir(d)
            if f.lower().endswith(".seq") and os.path.isfile(os.path.join(d, f)))
        ins_files = sorted(
            f for f in os.listdir(d)
            if f.lower().endswith(".ins") and os.path.isfile(os.path.join(d, f)))

        if not seq_files and not ins_files:
            messagebox.showerror(
                APP,
                "This folder has neither a GT3 pack\n"
                "  (music.inf + music.seq + music.ins)\n"
                "nor any GT2-style .seq / .ins files.")
            return

        gt2_seqs: list[tuple[str, SeqG]] = []
        errors = []
        for name in seq_files:
            try:
                s = SeqG.open(os.path.join(d, name))
                gt2_seqs.append((name, s))
            except Exception as e:
                errors.append(f"{name}: {e}")

        gt2_insts: list[tuple[str, Inst]] = []
        for name in ins_files:
            try:
                i = Inst.open(os.path.join(d, name))
                gt2_insts.append((name, i))
            except Exception as e:
                errors.append(f"{name}: {e}")

        if not gt2_seqs:
            msg = "No readable SEQG (.seq) files found."
            if errors:
                msg += "\n\n" + "\n".join(errors[:8])
            messagebox.showerror(APP, msg)
            return

        self.mode = "gt2"
        self.mseq = None
        # Build a synthetic multi-sequence SeqG view + fake Mseq list for the tree
        self.gt2_seqs = gt2_seqs
        self.gt2_insts = gt2_insts
        self.seqg = self._gt2_as_seqg()
        self.mseq = self._gt2_as_mseq()
        self.inst = gt2_insts[0][1] if gt2_insts else Inst(raw=b"INST" + b"\0" * 12)
        self.music_dir = d
        self.dirty = False
        self.path_lbl.configure(text=d)
        ins_summary = ", ".join(n for n, _ in gt2_insts) if gt2_insts else "(none)"
        self.info_lbl.configure(
            text=f"GT2 / loose files — {len(gt2_seqs)} .seq  ·  {len(gt2_insts)} .ins  ({ins_summary})")
        self._set_btns("normal")
        # Replace is OK; save writes individual .seq files back
        self.refresh()
        self.log_fn(f"Opened GT2-style sequenced files in {d} "
                    f"({len(gt2_seqs)} seq, {len(gt2_insts)} ins)")
        if errors:
            self.log_fn("Some files skipped: " + "; ".join(errors[:5]))

    def _gt2_as_seqg(self) -> SeqG:
        """Flatten each GT2 single-seq file into one combined SeqG for the UI."""
        from gt3bgm.seqg import Sequence
        combined = SeqG(sequences=[])
        for name, sg in self.gt2_seqs:
            if sg.sequences:
                combined.sequences.append(sg.sequences[0])
            else:
                combined.sequences.append(Sequence())
        return combined

    def _gt2_as_mseq(self) -> Mseq:
        """One synthetic song entry per GT2 .seq file."""
        songs = []
        for i, (name, sg) in enumerate(self.gt2_seqs):
            stem = os.path.splitext(name)[0]
            songs.append(MseqSong(
                name=stem,
                seq_file=name,
                title=stem,
                artist="",
                seq_index=i,
            ))
        return Mseq(version=1, songs=songs)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self.mseq or not self.seqg:
            return
        for i, s in enumerate(self.mseq.songs):
            bpm = ""
            if 0 <= s.seq_index < len(self.seqg.sequences):
                bpm = f"{self.seqg.sequences[s.seq_index].bpm:.0f}"
            self.tree.insert("", "end", iid=str(i), values=(
                i, s.name, s.title, s.artist, s.seq_index, bpm))

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def export_midi(self):
        if not self.seqg or not self.mseq:
            return
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo(APP, "Select a song first.")
            return
        song = self.mseq.songs[idx]
        seq_i = song.seq_index
        if not 0 <= seq_i < len(self.seqg.sequences):
            messagebox.showerror(APP, f"Song points at sequence {seq_i}, which does not exist.")
            return
        out = filedialog.asksaveasfilename(
            title="Export sequence as MIDI",
            defaultextension=".mid",
            initialfile=f"{song.name}.mid",
            filetypes=[("MIDI", "*.mid"), ("All", "*.*")])
        if not out:
            return
        try:
            sequence_to_midi(self.seqg.sequences[seq_i], out)
        except Exception as e:
            messagebox.showerror(APP, f"Export failed:\n{e}")
            return
        self.log_fn(f"Exported sequence {seq_i} ({song.name}) → {out}")
        messagebox.showinfo(APP, f"Wrote {out}")

    def export_all_midi(self):
        if not self.seqg or not self.mseq:
            return
        out_dir = filedialog.askdirectory(title="Folder for MIDI files")
        if not out_dir:
            return
        try:
            for i, seq in enumerate(self.seqg.sequences):
                name = f"seq{i:02d}"
                for s in self.mseq.songs:
                    if s.seq_index == i:
                        name = s.name
                        break
                path = os.path.join(out_dir, f"{name}.mid")
                sequence_to_midi(seq, path)
                self.log_fn(f"  [{i}] {path}")
        except Exception as e:
            messagebox.showerror(APP, f"Export failed:\n{e}")
            return
        messagebox.showinfo(APP, f"Wrote {self.seqg.sequence_count()} MIDI files to\n{out_dir}")

    def replace_seq(self):
        if not self.seqg or not self.mseq:
            return
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo(APP, "Select a song first (its sequence will be replaced).")
            return
        song = self.mseq.songs[idx]
        seq_i = song.seq_index
        if not 0 <= seq_i < len(self.seqg.sequences):
            messagebox.showerror(APP, f"Song points at sequence {seq_i}, which does not exist.")
            return
        mid = filedialog.askopenfilename(
            title="MIDI file to use as the new sequence",
            filetypes=[("MIDI", "*.mid *.midi"), ("All", "*.*")])
        if not mid:
            return
        try:
            old = self.seqg.sequences[seq_i]
            new = midi_to_sequence(mid, master_volume=old.master_volume)
            if new.tempo_ms == 500_000 and old.tempo_ms:
                new.tempo_ms = old.tempo_ms
            self.seqg.sequences[seq_i] = new
            self.dirty = True
            self.refresh()
            active = sum(1 for t in new.tracks if t.events)
            self.log_fn(f"Replaced sequence {seq_i} from {os.path.basename(mid)} "
                        f"({new.bpm:.0f} BPM, {active} tracks). Click Save files to write it to a separate folder.")
            messagebox.showinfo(APP, f"Sequence {seq_i} replaced.\n"
                                     f"{new.bpm:.0f} BPM, {active} active tracks.\n\n"
                                     "Click Save files… to write music.seq into a separate folder.")
        except Exception as e:
            messagebox.showerror(APP, f"Could not import that MIDI:\n{e}")

    def build_soundfont(self):
        """Build SF2 SoundFont file(s) from loaded .ins bank(s)."""
        if not self.music_dir:
            return
        banks: list[tuple[str, Inst]] = []
        if self.mode == "gt2":
            banks = list(self.gt2_insts)
        elif self.inst is not None:
            banks = [("music.ins", self.inst)]
        if not banks:
            messagebox.showinfo(APP, "No instrument bank (.ins) is loaded.")
            return

        if len(banks) == 1:
            default_name = os.path.splitext(banks[0][0])[0] + ".sf2"
            out = filedialog.asksaveasfilename(
                title="Save SoundFont",
                defaultextension=".sf2",
                initialfile=default_name,
                filetypes=[("SoundFont", "*.sf2"), ("All", "*.*")])
            if not out:
                return
            targets = [(banks[0][0], banks[0][1], out)]
        else:
            out_dir = filedialog.askdirectory(title="Folder for SoundFont files")
            if not out_dir:
                return
            targets = [
                (name, inst, os.path.join(out_dir, os.path.splitext(name)[0] + ".sf2"))
                for name, inst in banks
            ]

        try:
            for name, inst, path in targets:
                if not inst.samples:
                    inst = Inst.read(inst.raw)
                inst.extract_sf2(path)
                self.log_fn(f"SoundFont {len(inst.samples)} samples from {name} → {path}")
        except Exception as e:
            messagebox.showerror(APP, f"SoundFont build failed:\n{e}")
            return

        if len(targets) == 1:
            messagebox.showinfo(APP, f"Wrote {targets[0][2]}\n\n"
                                     f"{len(targets[0][1].samples)} presets (one per sample).\n"
                                     "Program numbers = sample index. Load in a DAW or player "
                                     "alongside the exported MIDI.")
        else:
            messagebox.showinfo(APP, f"Wrote {len(targets)} SoundFont files.\n"
                                     "Each bank has one preset per sample.")

    def extract_instruments(self):
        """Decode SPU-ADPCM samples from .ins bank(s) to WAV + VAG."""
        if not self.music_dir:
            return
        out_dir = filedialog.askdirectory(title="Folder for extracted instrument samples")
        if not out_dir:
            return

        banks: list[tuple[str, Inst]] = []
        if self.mode == "gt2":
            banks = list(self.gt2_insts)
        elif self.inst is not None:
            banks = [("music.ins", self.inst)]

        if not banks:
            messagebox.showinfo(APP, "No instrument bank (.ins) is loaded.")
            return

        total_written = []
        try:
            for name, inst in banks:
                # Re-parse to ensure samples are split (older loaded objects may lack them)
                if not inst.samples:
                    inst = Inst.read(inst.raw)
                sub = os.path.join(out_dir, os.path.splitext(name)[0])
                written = inst.extract_all(sub, also_vag=True)
                total_written.extend(written)
                self.log_fn(f"Extracted {len(inst.samples)} samples from {name} → {sub}")
        except Exception as e:
            messagebox.showerror(APP, f"Extraction failed:\n{e}")
            return

        n_wav = sum(1 for p in total_written if p.endswith(".wav"))
        messagebox.showinfo(
            APP,
            f"Extracted {n_wav} samples (WAV + VAG) from {len(banks)} bank(s)\n"
            f"into {out_dir}\n\n"
            "Sample rate is assumed 22050 Hz (typical for SPU banks).\n"
            "Program/note mapping is not yet included — these are the raw samples.")

    def _original_file_names(self) -> list[str]:
        """The files on disk that belong to the folder that is open."""
        if self.mode == "gt2":
            return [n for n, _ in self.gt2_seqs] + [n for n, _ in self.gt2_insts]
        return ["music.inf", "music.seq", "music.ins"]

    def backup_originals(self):
        """Copy the original files into a new time-stamped folder. Nothing is changed or overwritten."""
        if not self.music_dir:
            return
        dest_root = filedialog.askdirectory(
            title="Put the backup into which folder? (a new dated folder is made inside it)")
        if not dest_root:
            return
        try:
            dest, names = backup_files(self.music_dir, self._original_file_names(), dest_root)
        except Exception as e:
            messagebox.showerror(APP, f"Backup failed:\n{e}")
            return
        self.log_fn(f"Backed up {len(names)} file(s) to {dest}")
        messagebox.showinfo(APP, "Backed up and verified:\n  " + "\n  ".join(names) +
                                 f"\n\ninto\n{dest}")

    def save_files(self):
        """Write the result into a folder of the user's choosing. The loaded files are never touched."""
        if not self.seqg or not self.music_dir:
            return
        out = filedialog.askdirectory(
            title="Save into which folder? (not the folder you opened - your originals stay as they are)")
        if not out:
            return
        same = os.path.normcase(os.path.realpath(out)) == os.path.normcase(os.path.realpath(self.music_dir))
        if same:
            messagebox.showerror(APP, "That is the folder you opened.\n\nPick a different folder so your "
                                      "original files are left alone.")
            return
        try:
            # (name, bytes to write, original bytes, replaced sequence indices or None for a plain copy)
            files: list[tuple[str, bytes, bytes, list[int] | None]] = []
            if self.mode == "gt2":
                for i, (name, sg) in enumerate(self.gt2_seqs):
                    if not sg.sequences:
                        continue                                    # unreadable file: nothing to write
                    sg.sequences[0] = self.seqg.sequences[i]       # carries any replacement
                    files.append((name, sg.write(), sg.raw, sg.replaced_indices()))
                for name, inst in self.gt2_insts:
                    files.append((name, inst.write(), inst.raw, None))
            else:
                files.append(("music.inf", self.mseq.raw, self.mseq.raw, None))
                files.append(("music.seq", self.seqg.write(), self.seqg.raw, self.seqg.replaced_indices()))
                files.append(("music.ins", self.inst.write(), self.inst.raw, None))

            for name, data, original, replaced in files:
                if replaced is None:
                    problems = [] if data == original else ["this file was not meant to change but does"]
                else:
                    problems = check_rebuild(original, data, replaced)
                if problems:
                    raise ValueError(f"{name} did not rebuild cleanly, nothing was written:\n  "
                                     + "\n  ".join(problems[:8]))
            written = []
            for name, data, original, replaced in files:
                with open(os.path.join(out, name), "wb") as f:
                    f.write(data)
                if data == original:
                    note = "identical to the original"
                else:
                    note = f"{len(replaced)} replaced"
                written.append(f"{name} ({len(data)} B, {note})")
        except Exception as e:
            messagebox.showerror(APP, f"Could not write files:\n{e}")
            return
        self.dirty = False
        self.log_fn("Saved to " + out + ": " + ", ".join(written))
        messagebox.showinfo(APP, "Wrote:\n  " + "\n  ".join(written) + f"\n\ninto {out}\n\n"
                                 "Copy them into the game's data/music/ folder to use them.")


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=10)
        self.root = root
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.inf: AdsInf | None = None
        self.inf_path = ""
        self.bgm_dir = ""
        self.original = b""
        self.encoded: dict[str, bytes] = {}     # name -> .ads bytes this session produced
        self.changed: set[str] = set()          # existing songs the user edited on purpose
        self.removed: set[str] = set()          # entries taken out of the index

        nb = ttk.Notebook(self)
        nb.grid(column=0, row=0, sticky="nsew")

        # --- Race BGM tab (existing UI) ---
        race = ttk.Frame(nb, padding=4)
        nb.add(race, text="Race BGM (ads.inf)")
        race.columnconfigure(0, weight=1)
        race.rowconfigure(3, weight=1)

        bar = ttk.Frame(race)
        bar.grid(column=0, row=0, sticky="ew")
        ttk.Button(bar, text="Open ads.inf…", command=self.open_inf).grid(column=0, row=0)
        self.add_btn = ttk.Button(bar, text="Add new music…", command=self.add_music, state="disabled")
        self.add_btn.grid(column=1, row=0, padx=6)
        self.export_btn = ttk.Button(bar, text="Export files…", command=self.export, state="disabled")
        self.export_btn.grid(column=2, row=0)
        self.path_lbl = ttk.Label(bar, text="no file open", foreground="#666")
        self.path_lbl.grid(column=3, row=0, padx=12, sticky="w")

        sel = ttk.LabelFrame(race, text="Selected song", padding=6)
        sel.grid(column=0, row=1, sticky="ew", pady=(8, 0))
        self.sel_btns = []
        for i, (label, cmd) in enumerate((("Save audio as…", self.save_audio),
                                          ("Replace audio…", self.replace_audio),
                                          ("Re-time cuts…", self.retime),
                                          ("Remove from list", self.remove_song))):
            b = ttk.Button(sel, text=label, command=cmd, state="disabled")
            b.grid(column=i, row=0, padx=(0, 6))
            self.sel_btns.append(b)
        self.e_title = tk.StringVar()
        self.e_artist = tk.StringVar()
        ttk.Label(sel, text="Title").grid(column=4, row=0, sticky="e", padx=(12, 2))
        ttk.Entry(sel, textvariable=self.e_title, width=24).grid(column=5, row=0)
        ttk.Label(sel, text="Artist").grid(column=6, row=0, sticky="e", padx=(8, 2))
        ttk.Entry(sel, textvariable=self.e_artist, width=18).grid(column=7, row=0)
        self.rename_btn = ttk.Button(sel, text="Rename", command=self.apply_rename, state="disabled")
        self.rename_btn.grid(column=8, row=0, padx=6)

        cols = ("group", "name", "title", "artist", "length", "audio", "markers")
        self.tree = ttk.Treeview(race, columns=cols, show="headings", height=15, selectmode="extended")
        for c, w in zip(cols, (46, 108, 215, 150, 62, 74, 200)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(column=0, row=3, sticky="nsew", pady=(8, 0))
        self.tree.tag_configure("new", foreground="#0a7")
        self.tree.tag_configure("edited", foreground="#c60")
        self.tree.tag_configure("missing", foreground="#b00")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        sb = ttk.Scrollbar(race, orient="vertical", command=self.tree.yview)
        sb.grid(column=1, row=3, sticky="ns", pady=(8, 0))
        self.tree.configure(yscrollcommand=sb.set)

        self.progress = ttk.Progressbar(race, mode="determinate")
        self.progress.grid(column=0, row=4, sticky="ew", pady=(8, 0))
        self.log = tk.Text(race, height=9, wrap="word")
        self.log.grid(column=0, row=5, sticky="ew", pady=(8, 0))
        self.log.configure(state="disabled")
        self.say(f"{APP}. Open your game's data/bgm/ads.inf to begin.")
        self.say(REMINDER)

        # --- Sequenced music tab ---
        self.seq_frame = SeqMusicFrame(nb, log_fn=self.say)
        nb.add(self.seq_frame, text="Sequenced music (music.inf)")

    def ui(self, fn, *args) -> None:
        """Tk may only be touched from the thread that owns it; the encoder runs on another one."""
        if threading.current_thread() is threading.main_thread():
            fn(*args)
        else:
            self.root.after(0, fn, *args)

    def say(self, text: str) -> None:
        self.ui(self._say, text)

    def _say(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _progress(self, pct: float) -> None:
        self.progress.configure(value=pct)

    def _buttons(self, state: str) -> None:
        self.add_btn.configure(state=state)
        self.export_btn.configure(state=state)
        self.on_select()

    def refresh(self) -> None:
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        if not self.inf:
            return
        for i, s in enumerate(self.inf.songs):
            name, path = self.inf.name(s), self._audio_path(s)
            if name in self.encoded:
                audio, tag = ("new" if s.is_new else "replaced"), ("new" if s.is_new else "edited")
            elif path and os.path.isfile(path):
                audio, tag = "on disk", ("edited" if name in self.changed else "")
            else:
                audio, tag = "MISSING", "missing"
            self.tree.insert("", "end", iid=str(i), tags=(tag,) if tag else (), values=(
                s.group, name, self.inf.text_of(s, 2), self.inf.text_of(s, 3),
                f"{s.table.seconds:.1f}s", audio, s.table.summary()))
        for iid in keep:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        race = len(self.inf.group_songs(RACE_GROUP))
        self.path_lbl.configure(text=f"{self.inf_path}   •   {len(self.inf.songs)} songs, "
                                     f"{race}/{MUSIC_LIST_CAP} in the race list")

    def selected_songs(self) -> list[Song]:
        return [self.inf.songs[int(i)] for i in self.tree.selection()] if self.inf else []

    def selected(self) -> Song | None:
        songs = self.selected_songs()
        return songs[0] if songs else None

    def _audio_path(self, song: Song) -> str:
        """Where this song's .ads lives next to the index we opened."""
        return os.path.join(self.bgm_dir, self.inf.file_name(song)) if self.bgm_dir else ""

    def _audio_bytes(self, song: Song) -> bytes | None:
        name = self.inf.name(song)
        if name in self.encoded:
            return self.encoded[name]
        p = self._audio_path(song)
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def open_inf(self) -> None:
        p = filedialog.askopenfilename(title="Open GT3 ads.inf",
                                       filetypes=[("GT3 song index", "ads.inf"), ("All files", "*.*")])
        if not p:
            return
        try:
            with open(p, "rb") as f:
                data = f.read()
            inf = AdsInf.read(data)
        except Exception as e:
            messagebox.showerror(APP, f"Could not read that file:\n{e}")
            return
        if inf.write() != data:
            messagebox.showwarning(APP, "This ads.inf does not rebuild byte-for-byte, so editing it is not safe. "
                                        "Please report the file - it is a variant the tool does not understand yet.")
            return
        self.inf, self.original, self.inf_path = inf, data, p
        self.bgm_dir = os.path.dirname(os.path.abspath(p))
        self.encoded.clear()
        self.changed.clear()
        self.removed.clear()
        self._buttons("normal")
        self.refresh()
        missing = sum(1 for s in inf.songs if not os.path.isfile(self._audio_path(s)))
        self.say(f"Opened {p} - {len(inf.songs)} songs, rebuild verified byte-identical." +
                 (f" {missing} song(s) have no .ads in this folder." if missing else ""))

    def add_music(self) -> None:
        got = pick_audio(self.root)
        if not got:
            return
        wav, project, rate, pcm = got
        d = AddSongDialog(self.root, wav, len(pcm[0]) / rate, project)
        if not d.result:
            return
        opt = d.result
        if any(self.inf.name(s).lower() == opt["name"] for s in self.inf.songs):
            messagebox.showerror(APP, f"'{opt['name']}' is already used. Song file names must be unique.\n\n"
                                      "To change that song instead, select it and use Replace audio.")
            return
        self._buttons("disabled")
        threading.Thread(target=self._encode, args=(wav, rate, pcm, opt, None), daemon=True).start()

    def replace_audio(self) -> None:
        song = self.selected()
        if not song:
            return
        name = self.inf.name(song)
        got = pick_audio(self.root)
        if not got:
            return
        wav, project, rate, pcm = got
        if not messagebox.askyesno(APP, f"Replace the audio under '{name}' with "
                                        f"{os.path.basename(wav)} ({len(pcm[0]) / rate:.1f} s)?\n\n"
                                        f"{self.inf.file_name(song)} will be rewritten when you export. The "
                                        "entry keeps its name, so the game and your save will not notice."):
            return
        opt = dict(mode="keep", project=project, bpm="120", first="0.0", bars="2", labels="", also_beats=True)
        if project and len(project.cut_times()) > 1:
            if messagebox.askyesno(APP, "That project has clip splits. Use them as the new camera cuts?"):
                opt["mode"] = "project"
        self._buttons("disabled")
        threading.Thread(target=self._encode, args=(wav, rate, pcm, opt, song), daemon=True).start()

    def _encode(self, wav, rate, pcm, opt, song: Song | None) -> None:
        """Runs off the UI thread: converting audio takes about half the song's length in time."""
        try:
            self.say(f"Converting {os.path.basename(wav)} …")
            ads = ps.write_ads(rate, pcm, progress=lambda f: self.ui(self._progress, f * 100))
            self.ui(self._progress, 100)
            length = ps.samples_per_channel(ads)
            if opt["mode"] == "keep":
                table = None                     # keep the song's existing cuts, trimmed to the new length
            else:
                table = build_markers(opt, rate, length, self.say)
            quality = vfy.snr(pcm, ads)
            self.ui(self._finish, opt, ads, table, quality, song, length)
        except Exception as e:
            self.say(f"FAILED: {e}")
            self.ui(messagebox.showerror, APP, str(e))
            self.ui(self._progress, 0)
            self.ui(self._buttons, "normal")

    def _finish(self, opt, ads, table, quality, song: Song | None, length: int) -> None:
        if song is None:
            song = self.inf.add_song(RACE_GROUP, opt["name"], opt["title"], opt["artist"], table)
            name = opt["name"]
            self.say(f"Added '{name}' - {table.seconds:.1f} s, {table.summary()}, "
                     f"conversion quality {quality:.1f} dB.")
        else:
            name = self.inf.name(song)
            if table is None:                    # keeping the old cuts: drop any that fall off the new end
                table = song.table
                dropped = sum(1 for c in table.ch for v in c if v >= length)
                table.length = length
                for i, chan in enumerate(table.ch):
                    table.ch[i] = [v for v in chan if v < length]
                if dropped:
                    self.say(f"  {dropped} marker(s) fell past the end of the new audio and were dropped")
            song.table = table
            self.changed.add(name)
            self.say(f"Replaced the audio under '{name}' - {table.seconds:.1f} s, {table.summary()}, "
                     f"conversion quality {quality:.1f} dB.")
        self.encoded[name] = ads
        self.refresh()
        self._progress(0)
        self._buttons("normal")

    def retime(self) -> None:
        song = self.selected()
        if not song:
            return
        name = self.inf.name(song)
        project = None
        if messagebox.askyesno(APP, "Take the new cut points from an Audacity project?\n\n"
                                    "Yes - pick a project whose clip splits are the cuts.\n"
                                    "No - set them from a tempo or a label file."):
            p = filedialog.askopenfilename(title="Audacity project", filetypes=[("Audacity project", "*.aup3")])
            if not p:
                return
            try:
                project = Project(p)
            except Exception as e:
                messagebox.showerror(APP, f"Could not read that project:\n{e}")
                return
        d = RetimeDialog(self.root, f"{name}  •  {song.table.seconds:.1f} s  •  "
                                    f"now: {song.table.summary()}", project)
        if not d.result:
            return
        song.table = build_markers(d.result, song.table.rate, song.table.length, self.say)
        self.changed.add(name)
        self.say(f"Re-timed '{name}' - {song.table.summary()}.")
        self.refresh()

    def save_audio(self) -> None:
        songs = self.selected_songs()
        if not songs:
            return
        if len(songs) > 1:
            folder = filedialog.askdirectory(title=f"Save {len(songs)} songs into which folder?")
            if not folder:
                return
            for s in songs:
                self._write_audio(s, os.path.join(folder, self.inf.name(s) + ".wav"))
            return
        song = songs[0]
        kinds = [("WAV audio", "*.wav")] + ([("MP3 audio", "*.mp3")] if ex.have_ffmpeg() else [])
        out = filedialog.asksaveasfilename(title=f"Save '{self.inf.name(song)}'", defaultextension=".wav",
                                           initialfile=self.inf.name(song) + ".wav", filetypes=kinds)
        if out:
            self._write_audio(song, out)

    def _write_audio(self, song: Song, out: str) -> None:
        name = self.inf.name(song)
        data = self._audio_bytes(song)
        if not data:
            self.say(f"'{name}': no {self.inf.file_name(song)} in this folder, nothing to save.")
            return
        try:
            rate, pcm = ps.read_ads(data)
            note = ex.save(out, rate, pcm)
            self.say(f"'{name}': {note} ({len(pcm[0]) / rate:.1f} s, {rate} Hz)")
        except Exception as e:
            self.say(f"'{name}': could not save - {e}")
            messagebox.showerror(APP, f"Could not save '{name}':\n{e}")

    def remove_song(self) -> None:
        songs = self.selected_songs()
        if not songs:
            return
        names = [self.inf.name(s) for s in songs]
        stock = [n for n, s in zip(names, songs) if not s.is_new]
        text = "Remove from the music list:\n  " + "\n  ".join(names)
        if stock:
            text += ("\n\nNote: " + ", ".join(stock[:4]) +
                     " came with the file you opened, not from this session.")
        text += "\n\nThe .ads files stay on disk - only the index entry goes away, so nothing is lost."
        if not messagebox.askyesno(APP, text):
            return
        was_there = self._original_names()
        for s, n in zip(songs, names):
            self.inf.remove_song(s)
            self.encoded.pop(n, None)
            self.changed.discard(n)
            if n in was_there:                  # only count it as a removal if the opened file had it
                self.removed.add(n)
        self.say(f"Removed {len(names)} entr{'y' if len(names) == 1 else 'ies'}: {', '.join(names)}. "
                 "Export and reinstall for the game to see it.")
        self.refresh()

    def _original_names(self) -> set[str]:
        base = AdsInf.read(self.original)
        return {base.name(s) for s in base.songs}

    def on_select(self, _evt=None) -> None:
        songs = self.selected_songs()
        many = "normal" if songs else "disabled"            # save and remove take a whole selection
        one = "normal" if len(songs) == 1 else "disabled"   # replace and re-time act on one song
        for b, state in zip(self.sel_btns, (many, one, one, many)):
            b.configure(state=state)
        self.rename_btn.configure(state=one)
        if len(songs) == 1:
            self.e_title.set(self.inf.text_of(songs[0], 2))
            self.e_artist.set(self.inf.text_of(songs[0], 3))

    def apply_rename(self) -> None:
        s = self.selected()
        if not s:
            return
        self.inf.rename(s, self.e_title.get().strip(), self.e_artist.get().strip())
        self.refresh()
        self.say(f"Renamed '{self.inf.name(s)}' to \"{self.inf.text_of(s, 2)}\" / {self.inf.text_of(s, 3)}.")

    def export(self) -> None:
        if not self.inf:
            return
        out = filedialog.askdirectory(title="Export the files into which folder?")
        if not out:
            return
        try:
            rebuilt = self.inf.write()
            with open(os.path.join(out, "ads.inf"), "wb") as f:
                f.write(rebuilt)
            for name, ads in self.encoded.items():
                song = next((s for s in self.inf.songs if self.inf.name(s) == name), None)
                if song is None:
                    continue                                   # added then removed again
                stem = os.path.splitext(self.inf.file_name(song))[0]
                with open(os.path.join(out, f"{stem}.ads"), "wb") as f:
                    f.write(ads)
                with open(os.path.join(out, f"i_{stem}.ads"), "wb") as f:
                    f.write(ads)
        except Exception as e:
            messagebox.showerror(APP, f"Could not write the files:\n{e}")
            return

        live = {n: a for n, a in self.encoded.items() if any(self.inf.name(s) == n for s in self.inf.songs)}
        result = vfy.verify(self.original, rebuilt, live, self.changed, self.removed)
        self.say("")
        self.say(result.report())
        files = ["ads.inf"] + [f"{n}.ads and i_{n}.ads" for n in live]
        if result.ok:
            self.say("")
            self.say(REMINDER)
            messagebox.showinfo(APP, "Everything checks out.\n\n"
                                     f"Written to {out}:\n  " + "\n  ".join(files) +
                                     "\n\nCopy them into the game's data/bgm/ folder, then run\n"
                                     "Options → \"Back to Default Settings\" once so GT3 rebuilds its "
                                     "music list. The list will not change without that step.")
        else:
            messagebox.showerror(APP, result.report())


def main() -> None:
    root = tk.Tk()
    root.title(f"{APP} {__version__}")
    root.geometry("1080x760")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()


# made by a human and a machine.
