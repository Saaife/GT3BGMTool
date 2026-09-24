"""Back up the original files before anything is changed.

Every backup goes into its own new, time-stamped folder, so a second backup never overwrites the first.
Each copy is read back and compared with the source before it is reported as done.
"""

from __future__ import annotations

import os
import shutil
import time


def backup_files(src_dir: str, names: list[str], dest_root: str) -> tuple[str, list[str]]:
    """Copy src_dir/<name> for each name into a new folder inside dest_root.

    Returns (backup_folder, names_copied). Raises ValueError for an unusable destination or a file that
    is missing, and OSError if a copy does not match its source.
    """
    if os.path.normcase(os.path.realpath(dest_root)) == os.path.normcase(os.path.realpath(src_dir)):
        raise ValueError("The backup folder is the folder the files are in. Pick a different folder.")
    missing = [n for n in names if not os.path.isfile(os.path.join(src_dir, n))]
    if missing:
        raise ValueError("Cannot find: " + ", ".join(missing))
    if not names:
        raise ValueError("There are no files to back up.")

    stem = os.path.basename(os.path.normpath(src_dir)) or "files"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(dest_root, f"{stem}_backup_{stamp}")
    n = 1
    while os.path.exists(dest):                       # two backups in the same second: never reuse a folder
        n += 1
        dest = os.path.join(dest_root, f"{stem}_backup_{stamp}_{n}")
    os.makedirs(dest)

    for name in names:
        src, dst = os.path.join(src_dir, name), os.path.join(dest, name)
        shutil.copy2(src, dst)
        with open(src, "rb") as a, open(dst, "rb") as b:
            while True:
                x, y = a.read(1 << 20), b.read(1 << 20)
                if x != y:
                    raise OSError(f"{name}: the copy does not match the original. The backup is not safe to rely on.")
                if not x:
                    break
    return dest, list(names)
