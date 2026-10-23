"""Session-owned disk cache with leases; never deletes permanent market data.

No browser-unload promise: expired directories are reclaimed by a daemon while
the app is running, or on a later launch. All deletion targets must be owned,
direct children with an unguessable ID and a matching manifest.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
from threading import RLock, Thread
import time
import uuid

from src.stock_data.files import atomic_write_json

TTL_SECONDS = 600
_LOCK = RLock()
_WORKERS: set[str] = set()
_ID = re.compile(r"[0-9a-f]{32}")


def session_directory(root: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
        raise ValueError("无效临时会话标识。")
    root = Path(root).resolve()
    path = root / session_id
    if path.is_symlink():
        raise ValueError("临时会话目录不能是符号链接。")
    return path


def touch(root: Path, session_id: str, *, now: float | None = None, create=False) -> Path:
    with _LOCK:
        path = session_directory(root, session_id)
        manifest = path / "lease.json"
        if not path.exists() and not create:
            return path
        if manifest.is_symlink():
            raise ValueError("临时会话元数据不能是符号链接。")
        if path.exists() and not manifest.exists():
            raise ValueError("拒绝接管没有会话标记的临时目录。")
        if manifest.exists() and not owned(root, session_id):
            raise ValueError("拒绝续期其他来源或无效标记的目录。")
        stamp = time.time() if now is None else now
        atomic_write_json({"owner": "novice_research_session_v1", "session_id": session_id,
                           "last_seen": stamp}, manifest)
        return path


def owned(root: Path, session_id: str) -> bool:
    try:
        path = session_directory(root, session_id)
        manifest = path / "lease.json"
        if manifest.is_symlink():
            return False
        data = json.loads(manifest.read_text())
        return data.get("owner") == "novice_research_session_v1" and data.get("session_id") == session_id
    except (OSError, ValueError, TypeError):
        return False


def clear_session(root: Path, session_id: str) -> bool:
    with _LOCK:
        if not owned(root, session_id):
            return False
        shutil.rmtree(session_directory(root, session_id))
        return True


def sweep(root: Path, *, now: float | None = None, ttl=TTL_SECONDS) -> list[str]:
    """Expire disconnected leases; active sessions renew every 30 seconds."""
    removed = []
    now = time.time() if now is None else now
    root = Path(root)
    with _LOCK:
        for path in root.iterdir() if root.is_dir() else []:
            if not _ID.fullmatch(path.name) or not owned(root, path.name):
                continue
            try:
                lease = json.loads((path / "lease.json").read_text())
                stamp = float(lease["last_seen"])
                if now - stamp >= ttl and clear_session(root, path.name):
                    removed.append(path.name)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return removed


def start_cleaner(root: Path) -> None:
    root = Path(root).resolve()
    with _LOCK:
        if str(root) in _WORKERS:
            return
        _WORKERS.add(str(root))
    def clean():
        while True:
            try:
                sweep(root)
            except OSError:
                pass  # The UI exposes explicit cleanup; do not crash other sessions.
            time.sleep(30)
    Thread(target=clean, name="research-temp-cache-cleaner", daemon=True).start()


def get_session_id(state) -> str:
    if "temporary_cache_id" not in state:
        state["temporary_cache_id"] = uuid.uuid4().hex
    return state["temporary_cache_id"]


@contextmanager
def protect(root: Path, session_id: str):
    """A cleanup sweep cannot race a fetch/write/promotion in this process."""
    with _LOCK:
        path = touch(root, session_id, create=True)
        try:
            yield path
        finally:
            touch(root, session_id)
