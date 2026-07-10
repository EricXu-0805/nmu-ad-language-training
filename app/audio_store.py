"""音频字节本地存储(M6)。字节只落本机磁盘 data/audio/,不上云。

此前后端只登记元数据;有了字节落库口,checksum 闸门才能做真校验、
导出才能真打包音频、本地 ASR 才有音频来源。文件名 = raw_audio_id + 按 MIME 推断的扩展名。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"

# 防路径穿越:音频 id 只允许安全字符(前端生成 aud-<uuid>,天然满足)。
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

_EXT_BY_MIME = {
    "audio/webm": ".webm", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
}


def ext_for(mime: str | None) -> str:
    if not mime:
        return ".webm"
    return _EXT_BY_MIME.get(mime.split(";")[0].strip().lower(), ".webm")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_blob(raw_audio_id: str, data: bytes, mime: str | None) -> tuple[Path, str]:
    """写入字节并返回 (路径, sha256)。同 id 重传覆盖(先清旧扩展名防残留双份)。"""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for old in AUDIO_DIR.glob(f"{raw_audio_id}.*"):
        old.unlink()
    p = AUDIO_DIR / f"{raw_audio_id}{ext_for(mime)}"
    p.write_bytes(data)
    return p, sha256_hex(data)


def find_blob(raw_audio_id: str) -> Path | None:
    if not SAFE_ID.match(raw_audio_id) or not AUDIO_DIR.exists():
        return None
    for p in AUDIO_DIR.glob(f"{raw_audio_id}.*"):
        return p
    return None


def delete_blob(raw_audio_id: str) -> bool:
    """物理删除字节。只应在删除闸门放行后调用(见 main.audio_delete)。"""
    p = find_blob(raw_audio_id)
    if p:
        p.unlink()
        return True
    return False
