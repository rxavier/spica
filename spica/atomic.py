"""Write a file in one step, so a reader never sees half of it.

The queue, the plan, the request list, the pools, the station rack and the alias
file all replace small text files while another process may be reading them. The
temp name is the full file name plus `.tmp`, so two writers of different files can
never share one.
"""

from __future__ import annotations

import os
from pathlib import Path


def temp_path(path):
    """The temporary name a write to `path` uses: the full name plus `.tmp`."""
    target = Path(path)
    return target.with_name(target.name + ".tmp")


def atomic_write(path, text: str, *, mode: int | None = None) -> None:
    """Replace `path` with `text`, optionally setting the file mode first."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(target)
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(target)
