"""Read files out of a container through the Docker API (the `docker cp` archive endpoint).

This is a plain GET on /containers/{id}/archive, so it works through a read-only socket proxy
(CONTAINERS=1) and needs no exec, no bind mount and no shared filesystem. It is the fallback when
a known app's config isn't a bind mount Wharfmap can read: named volumes, files baked into the
image, or Wharfmap running in a container with nothing mounted.
"""
from __future__ import annotations

import fnmatch
import io
import posixpath
import subprocess
import tarfile

MAX_FILE = 8 * 1024 * 1024  # skip anything bigger; configs are small, media is not


def _archive(container: str, path: str) -> bytes | None:
    try:
        r = subprocess.run(["docker", "cp", f"{container}:{path}", "-"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def read_glob(container: str, pattern: str) -> list[tuple[str, str]]:
    """[(path_in_container, text)] for files matching `pattern` (glob on the final component only).
    Fetches the parent directory as a tar and filters inside it, so one API call per ref."""
    pattern = pattern.rstrip("/")
    if any(ch in pattern for ch in "*?["):
        base, pat = posixpath.split(pattern)
    else:
        base, pat = pattern, None
    blob = _archive(container, base)
    if blob is None:
        return []
    out = []
    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            for m in tf:
                if not m.isfile() or m.size > MAX_FILE:
                    continue
                name = m.name.split("/", 1)[1] if "/" in m.name else ""   # strip the archive's top dir
                full = posixpath.join(base, name) if pat else base
                if pat and ("/" in name or not fnmatch.fnmatch(name, pat)):
                    continue  # only direct children match a trailing glob
                if not pat and name:
                    continue  # asked for one file, got a directory listing
                f = tf.extractfile(m)
                if f:
                    out.append((full, f.read().decode("utf-8", errors="replace")))
    except (tarfile.TarError, OSError, ValueError):
        return []
    return out
