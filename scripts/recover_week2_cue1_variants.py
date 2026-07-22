#!/usr/bin/env python3
"""Recover Week-2 cue-1 branches from the authoritative Word source.

This is a deliberately narrow, fail-closed repair tool.  It only normalizes the
source document's missing/uneven outer quotation marks; it never writes new
clinical wording.  The source file and normalized paragraph stream must match
the reviewed SHA-256 values before the item bank can be changed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "20260629" / "第二周训练内容.docx"
BANK_PATH = ROOT / "platform" / "content" / "item_bank_v1.json"
SOURCE_SHA256 = "b3310b61bdc6afb437cbc05785bd6f4e1f6c30dd53ad0999eb2c0fea10c3891a"
NORMALIZED_TEXT_SHA256 = (
    "b7f2ad1d4389ee6193721402b1d39d9c3cc7a15d2341807471a5fc4627d06c55"
)
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BRANCH_MARKERS = {
    "unknown": "② 完全没有提及",
    "close": "③ 提及",
    "silence": "④ 出现沉默",
}


def _paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{{{WORD_NS}}}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{{{WORD_NS}}}t":
                parts.append(node.text or "")
            elif node.tag == f"{{{WORD_NS}}}tab":
                parts.append("\t")
            elif node.tag in {f"{{{WORD_NS}}}br", f"{{{WORD_NS}}}cr"}:
                parts.append("\n")
        raw = "".join(parts)
        paragraphs.append(" ".join(raw.split()))
    return paragraphs


def _cue_text(raw: str) -> str:
    text = re.sub(r"^第1次提示[：:]", "", raw).strip()
    # The Flower source paragraph is missing its closing quotation mark.  Only
    # the optional outer Chinese quotes are removed; inner wording is untouched.
    return text.removeprefix("“").removesuffix("”").strip()


def _single_items(paragraphs: list[str]) -> dict[str, dict]:
    starts: list[tuple[int, str]] = []
    for index, text in enumerate(paragraphs[:507]):
        match = re.fullmatch(r"[1-5]\.(.+)", text)
        if match is None:
            continue
        if not any(
            value.startswith("完成条件")
            for value in paragraphs[index + 1:index + 4]
        ):
            continue
        starts.append((index, match.group(1)))
    if len(starts) != 20:
        raise RuntimeError(f"expected 20 single-element items, found {len(starts)}")

    recovered: dict[str, dict] = {}
    for ordinal, (start, target) in enumerate(starts):
        stop = starts[ordinal + 1][0] if ordinal + 1 < len(starts) else 507
        section = paragraphs[start:stop]
        variants: dict[str, dict] = {}
        for branch, marker in BRANCH_MARKERS.items():
            matches = [
                offset for offset, value in enumerate(section)
                if marker in value
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"{target}:{branch} expected one marker, found {len(matches)}"
                )
            paragraph_index = start + matches[0] + 1
            raw = paragraphs[paragraph_index]
            if not raw.startswith("第1次提示"):
                raise RuntimeError(
                    f"{target}:{branch} source P{paragraph_index} is not cue-1"
                )
            text = _cue_text(raw)
            if not text:
                raise RuntimeError(f"{target}:{branch} extracted empty cue")
            variants[branch] = {
                "text": text,
                "source_paragraph_index": paragraph_index,
            }
        recovered[f"SE_{target}"] = variants
    return recovered


def main() -> None:
    source_bytes = SOURCE.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("Week-2 source file SHA-256 changed; manual review required")
    paragraphs = _paragraphs(SOURCE)
    normalized_digest = hashlib.sha256(
        "\n".join(paragraphs).encode("utf-8")
    ).hexdigest()
    if normalized_digest != NORMALIZED_TEXT_SHA256:
        raise RuntimeError(
            "Week-2 normalized paragraph digest changed; manual review required"
        )
    recovered = _single_items(paragraphs)

    bank = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    rows = bank.get("single_element")
    if not isinstance(rows, list) or len(rows) != 20:
        raise RuntimeError("item bank does not contain the expected 20 single rows")
    seen: set[str] = set()
    for row in rows:
        item_id = row.get("item_id")
        if item_id not in recovered or item_id in seen:
            raise RuntimeError(f"unexpected or duplicate item-bank row: {item_id!r}")
        seen.add(item_id)
        cue1 = (row.get("cues") or {}).get("1")
        if not isinstance(cue1, dict):
            raise RuntimeError(f"{item_id} has no cue-1 object")
        variants = recovered[item_id]
        if cue1.get("text") != variants["unknown"]["text"]:
            raise RuntimeError(
                f"{item_id} legacy cue-1 differs from the source unknown branch"
            )
        cue1["variants"] = variants
    if seen != set(recovered):
        raise RuntimeError("item bank and source item sets differ")
    bank["cue1_variant_source_locator_scheme"] = (
        "python-docx compatible body paragraph index, zero-based"
    )
    bank["draft_revision"] = "2026-07-19.2"
    BANK_PATH.write_text(
        json.dumps(bank, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
