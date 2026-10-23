from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd

from src.stock_data.errors import StockCacheError


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    """Write a CSV beside its destination and replace it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", suffix=".tmp",
            prefix=f".{path.name}.", dir=path.parent, delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            frame.to_csv(temp_file, index=False)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        raise StockCacheError(f"本地 CSV 写入失败，原缓存已保留：{path.name}：{exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def atomic_write_json(payload: dict, path: Path) -> None:
    """Atomically write UTF-8 JSON metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".tmp",
            prefix=f".{path.name}.", dir=path.parent, delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(payload, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        raise StockCacheError(f"本地元数据写入失败，原元数据已保留：{path.name}：{exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise StockCacheError(f"本地元数据无法读取：{path.name}：{exc}") from exc
