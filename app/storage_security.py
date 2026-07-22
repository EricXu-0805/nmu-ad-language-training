"""Owner-only filesystem defaults for research data and derived artifacts."""
from __future__ import annotations

import os
from pathlib import Path


def install_private_umask() -> None:
    """Make every subsequently created process file private by default."""
    os.umask(0o077)


def ensure_private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise RuntimeError(f"研究数据目录不得是符号链接: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    if path.is_symlink():
        raise RuntimeError(f"研究数据文件不得是符号链接: {path}")
    if path.exists():
        path.chmod(0o600)
    return path
