"""预合成全部话术进 TTS 缓存 + 出耳测样音。

用法(在 platform/ 下,需先配 DASHSCOPE_API_KEY 环境变量):
  ./.venv/bin/python scripts/presynthesize_tts.py --dry-run          # 只列清单和字符量,不出网
  ./.venv/bin/python scripts/presynthesize_tts.py                    # 用当前 TTS_ENGINE(有 Key 时默认 auto→qwen-tts)打满缓存
  ./.venv/bin/python scripts/presynthesize_tts.py --engine cosyvoice --engine qwen-tts --samples 5
      # 两个云引擎各合成一遍;每个引擎另存 5 句样音到 data/tts-preview/<engine>/ 供耳测选型

只合成白名单闭集(题库+第一周脚本+固定 UI 话术)——和运行时红线同一个集合;
缓存键与运行时一致,预合成过的句子上线后零延迟、零增量出网。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import content, tts  # noqa: E402


def collect_lines() -> list[str]:
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    proto = content.load_autopilot_protocol(content.CONTENT_DIR / "autopilot_protocol_v1.json")
    lines: set[str] = set()
    # 预合成缓存覆盖索引里的全部训练周,新周登记后重跑本脚本即可获得同等缓冲。
    for week in sorted(content.load_item_bank_index()):
        bank = content.load_item_bank_for_week(week)
        lines |= content.tts_allowlist(bank, script, proto)
    return sorted(lines)


def _billing_chars(text: str) -> int:
    """百炼 TTS 计费口径:汉字(CJK 统一表意)按 2 字符计,其余按 1。"""
    return sum(2 if "一" <= ch <= "鿿" else 1 for ch in text)


_ENGINE_PREFIX = {"cosyvoice": "dashscope/cosyvoice", "qwen-tts": "dashscope/qwen", "piper": "piper/"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", action="append", default=None,
                    choices=sorted(_ENGINE_PREFIX),
                    help="cosyvoice / qwen-tts / piper,可多次;缺省用 TTS_ENGINE 的当前解析结果")
    ap.add_argument("--samples", type=int, default=0, help="每引擎另存 N 句样音到 data/tts-preview/")
    ap.add_argument("--dry-run", action="store_true", help="只列清单与字符量,不合成")
    args = ap.parse_args()

    lines = collect_lines()
    bill = sum(_billing_chars(t) for t in lines)
    print(f"白名单话术 {len(lines)} 句,计费 {bill} 字符(汉字按2计)"
          f"(cosyvoice-v2 约 {bill * 2.0 / 10000:.2f} 元,qwen3-tts-flash 约 {bill * 0.8 / 10000:.2f} 元;"
          f"各模型另有免费额度)")
    if args.dry_run:
        for t in lines:
            print(f"  [{len(t):>3}] {t[:40]}{'…' if len(t) > 40 else ''}")
        return 0

    engines = args.engine or [None]
    fail = 0
    for kind in engines:
        if kind is not None:
            os.environ["TTS_ENGINE"] = kind
        tts._engine = None                      # 复位单例,吃新 env
        eng = tts.engine()
        label = kind or eng.version
        if isinstance(eng, tts.NullTtsEngine):
            print(f"✗ [{label}] 引擎不可用(缺 DASHSCOPE_API_KEY 或本地模型),跳过")
            fail += 1
            continue
        if kind and not eng.version.startswith(_ENGINE_PREFIX[kind]):
            # get_engine 的降级逻辑可能把缺 Key 的云引擎悄悄换成 piper——
            # 预合成必须所要即所得,静默换引擎会让人误以为云缓存已打满
            print(f"✗ 请求 {kind} 但解析为 {eng.version}(缺 DASHSCOPE_API_KEY?),跳过")
            fail += 1
            continue
        print(f"— [{eng.version}] 开始合成 {len(lines)} 句 —")
        preview_dir = tts.DATA_DIR / "tts-preview" / eng.version.replace("/", "_")
        ok = miss = 0
        for i, text in enumerate(lines):
            data, ver, cached = tts.speak(text)
            if data is None or ver != eng.version:   # 落到降级层=本引擎这句失败
                miss += 1
                print(f"  ✗ 合成失败: {text[:30]}…")
                continue
            ok += 1
            if args.samples and i < args.samples:
                preview_dir.mkdir(parents=True, exist_ok=True)
                (preview_dir / f"{i:02d}_{text[:12]}.wav").write_bytes(data)
        print(f"  完成: {ok} 句入缓存, {miss} 句失败"
              + (f";样音在 {preview_dir}" if args.samples and ok else ""))
        fail += 1 if miss else 0
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
