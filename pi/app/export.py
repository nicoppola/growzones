"""Streaming tar export for a session.

We build the tar chunk-by-chunk so the Pi Zero never holds the whole archive
in memory. The generator yields bytes; FastAPI's StreamingResponse pushes
them straight to the wire.
"""
from __future__ import annotations

import io
import tarfile
from typing import Iterator

from . import storage


class _ChunkBuffer(io.RawIOBase):
    """tarfile sink: collects writes, lets the generator drain between files."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[bytes] = []

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def write(self, b) -> int:  # type: ignore[override]
        data = bytes(b)
        self.chunks.append(data)
        return len(data)

    def drain(self) -> bytes:
        if not self.chunks:
            return b""
        out = b"".join(self.chunks)
        self.chunks = []
        return out


def stream_session_tar(session_id: str) -> Iterator[bytes]:
    session = storage.session_dir(session_id)
    if not session.exists():
        raise FileNotFoundError(f"session {session_id} not on disk")
    members = []
    manifest = storage.session_manifest_path(session_id)
    if manifest.exists():
        members.append(manifest)
    log = storage.session_log_path(session_id)
    if log.exists():
        members.append(log)
    members.extend(sorted(session.glob("*.jpg")))

    buf = _ChunkBuffer()
    with tarfile.open(fileobj=buf, mode="w|") as tar:
        for path in members:
            tar.add(str(path), arcname=f"{session_id}/{path.name}")
            chunk = buf.drain()
            if chunk:
                yield chunk
    tail = buf.drain()
    if tail:
        yield tail


def tar_filename(session_id: str) -> str:
    return f"growzones-session-{session_id}.tar"
