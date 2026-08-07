"""HARNESS ONLY —— 「签字后零代码可收真实受试者」的工程彩排入口。

**这不是生产入口,永远不得用于真实受试者或公网部署。**
与 tts_ack_harness 同一隔离契约(私有 root/临时库/进程内重定向),但验证的问题相反:
tts_ack 验证的是模拟自动驾驶黄金链;本入口验证**真实研究档案**在签字工件到位后,
不改一行代码、不开任何模拟开关,就能走完 建档→安排→审核→开场 的生产准入链。

为此本进程必须:
  * 不设置 ALLOW_SIMULATION_DATA / ENABLE_AUTOPILOT_P0A_SIMULATION——真实路径
    依赖它们即彩排失败;
  * 注入两份 STAGING 合成签字工件:合成冻结题库(ready_for_research=True)与
    研究批准图片交付清单(research_release_approved=True)。两者都走生产严格
    schema/契约校验,不打任何补丁;所有合成值带 STAGING 标记,绝不回写 content/。

合成冻结题库替真实内容组占位的只有工程结构(rubric/难度/多要素骨架),
不承载任何临床判定语义——真实冻结仍由内容组+PI 完成并双签。

运行手册(与 tts_ack_harness 相同的 root 契约):
    export NMU_HARNESS_ROOT="$(mktemp -d)"; chmod 700 "$NMU_HARNESS_ROOT"
    export DATABASE_URL="sqlite:///$NMU_HARNESS_ROOT/rehearsal.db"
    export AUDIO_DIR="$NMU_HARNESS_ROOT/audio"
    export NMU_HARNESS_TTS_CACHE_DIR="$NMU_HARNESS_ROOT/tts-cache"
    export REQUIRE_AUTH=1 CONSOLE_PIN=... NMU_HARNESS_PASSWORD=...
    unset ALLOW_SIMULATION_DATA ENABLE_AUTOPILOT_P0A_SIMULATION
    ./.venv/bin/python -m harness.research_rehearsal_harness --migrate-and-seed
    ./.venv/bin/python -m harness.research_rehearsal_harness --serve --port 8822
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from .tts_ack_harness import (
    CANONICAL_BANK_PATH,
    PLATFORM_ROOT,
    DeterministicHarnessAsrEngine,
    DeterministicHarnessTtsEngine,
    HarnessConfig,
    HarnessConfigError,
    _assert_imported_engine_binding,
    _pre_app_import_guard,
    canonical_bank_loader,
    harness_marker_middleware_class,
    migrate,
    resolve_config,
)

REHEARSAL_PATIENT_ID = "REH-R001"
CANONICAL_MANIFEST_PATH = PLATFORM_ROOT / "content" / "patient_asset_delivery_v1.json"
_FORBIDDEN_SIMULATION_FLAGS = (
    "ALLOW_SIMULATION_DATA", "ENABLE_AUTOPILOT_P0A_SIMULATION")


def resolve_rehearsal_config(env=None) -> HarnessConfig:
    """在 tts_ack 的隔离契约之上,反转模拟开关要求:必须缺席。

    resolve_config 强制两个模拟开关=1,这里先临时补上让路径/PIN/口令校验复用,
    再断言真实进程环境里它们不存在——彩排的核心命题就是"真实路径不需要它们"。
    """
    base = dict(os.environ if env is None else env)
    for flag in _FORBIDDEN_SIMULATION_FLAGS:
        if (base.get(flag) or "").strip():
            raise HarnessConfigError(
                f"彩排进程不得设置 {flag}:真实研究路径依赖模拟开关即彩排失败")
        base[flag] = "1"
    config = resolve_config(base)
    if config.tts_mode != "synthetic":
        raise HarnessConfigError("彩排入口只支持 synthetic TTS 模式")
    return config


# ----------------------------------------------------------------------------
# STAGING 合成签字工件(纯 JSON 变换;严格校验交给生产 loader)
# ----------------------------------------------------------------------------
def _canonical_digest(payload: dict, exclude: set[str]) -> str:
    body = {k: v for k, v in payload.items() if k not in exclude}
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()


def _synthetic_rubric(seed: str) -> dict:
    return {
        "rubric_version": "STAGING-SYN-1",
        "decision_policy": "any_acceptable_expression",
        "acceptable_expressions": [seed, f"用{seed}"],
        "cues": {"1": f"想一想{seed}是做什么用的?", "2": f"提示:和{seed}有关。"},
        "tell_answer": f"这个的答案和{seed}有关。",
    }


def write_frozen_research_content(root: Path) -> tuple[Path, Path]:
    """在 root/content 下生成合成冻结题库 + 研究批准交付清单,返回两个路径。"""
    out = root / "content"
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.loads(CANONICAL_BANK_PATH.read_text("utf-8"))

    raw["qc_status"] = "frozen"
    raw["draft_revision"] = "STAGING-SYN-FREEZE-1"
    raw["note"] = "STAGING 合成冻结:仅用于彩排工程链路,禁止用于真实受试者内容"

    for item in raw["single_element"]:
        if not item.get("acceptable_expressions"):
            item["acceptable_expressions"] = [item["target_word"]]
        if not item.get("difficulty_level"):
            item["difficulty_level"] = "STAGING-中"

    for item in raw["double_element"]:
        rubrics = item.get("operational_rubrics") or {}
        for role, seed in (("左作用", item.get("left_word") or "左词"),
                           ("右作用", item.get("right_word") or "右词"),
                           ("关系识别", item.get("pair_title") or "关系")):
            rubrics.setdefault(role, _synthetic_rubric(seed))
        item["operational_rubrics"] = rubrics

    image_ids = [it["image_id"] for it in raw["single_element"] if it.get("image_id")]
    multi = []
    for index, position in enumerate(raw.get("source_unstructured_positions") or []):
        key = position["source_position_key"] if isinstance(position, dict) else str(position)
        scene = key.split(":")[-1]
        multi.append({
            "item_id": f"ME_STAGING_{index + 1:02d}",
            "task_type": "多要素",
            "scene_title": scene,
            "initial_prompt": f"请看这张{scene}的图,说说您看到了什么?",
            "key_elements": ["要素一", "要素二"],
            "image_id": image_ids[index % len(image_ids)],
            "item_set_type": "训练集",
            "difficulty_level": "STAGING-中",
            "raw_source_ref": key,
            "operational_rubrics": {
                key_element: _synthetic_rubric(key_element)
                for key_element in ("要素一", "要素二")
            },
        })
    raw["multi_element"] = multi
    raw["source_unstructured_positions"] = []
    raw["source_unstructured_blockers"] = []
    raw["source_protocol_position_count"] = (
        len(raw["single_element"]) + 5 * len(raw["double_element"])
        + sum(len(item["key_elements"]) for item in multi))

    manifest = json.loads(CANONICAL_MANIFEST_PATH.read_text("utf-8"))
    manifest["manifest_version_id"] = "wk2-private-webp-staging-research.1"
    manifest["purpose"] = "research_and_simulation_byte_allowlist"
    manifest["release_scope"] = "research_and_simulation"
    manifest["research_release_status"] = "approved_for_research_presentation"
    manifest.pop("research_release_approvals", None)
    scope_digest = _canonical_digest(
        manifest, {"definition_sha256", "research_release_approvals"})
    manifest["research_release_approvals"] = {
        "source_transform_rights": {
            "approved_by": "STAGING-彩排签署人(非真实审批)",
            "approved_at": date.today().isoformat(),
            "scope_sha256": scope_digest},
        "content": {
            "approved_by": "STAGING-彩排签署人(非真实审批)",
            "approved_at": date.today().isoformat(),
            "scope_sha256": scope_digest},
    }
    manifest["definition_sha256"] = _canonical_digest(
        manifest, {"definition_sha256"})
    raw["patient_asset_delivery_manifest"] = {
        "version_id": manifest["manifest_version_id"],
        "definition_sha256": manifest["definition_sha256"],
    }

    bank_path = out / "item_bank_v1.json"
    manifest_path = out / "patient_asset_delivery_v1.json"
    bank_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return bank_path, manifest_path


def install(config: HarnessConfig):
    """导入生产 app,重定向存储与内容注入点,注入合成签字工件。"""
    _pre_app_import_guard(config)
    from app import db

    _assert_imported_engine_binding(config, db)

    from app import asr, audio_store, content, export, patient_asset, tts
    from app.main import app

    for directory in (config.audio_dir, config.tts_cache_dir, config.asr_scratch_dir,
                      config.export_dir, config.controlled_export_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    audio_store.AUDIO_DIR = config.audio_dir
    tts.CACHE_DIR = config.tts_cache_dir
    asr.SCRATCH_DIR = config.asr_scratch_dir
    export.EXPORT_DIR = config.export_dir
    export.CONTROLLED_AUDIO_DIR = config.controlled_export_dir

    original_loader = content.load_item_bank
    bank_path, manifest_path = write_frozen_research_content(config.root)
    bank = original_loader(bank_path)  # 生产严格 schema 校验,不打补丁
    content.load_item_bank = canonical_bank_loader(original_loader, bank)
    patient_asset.DELIVERY_MANIFEST_PATH = manifest_path

    readiness = content.content_readiness(bank)
    if readiness["ready_for_research"] is not True:
        raise HarnessConfigError(
            "合成冻结题库未达 ready_for_research=True:"
            f" errors={readiness['errors'][:3]} warnings={readiness['warnings'][:3]}")
    if patient_asset.research_release_approved() is not True:
        raise HarnessConfigError("合成研究批准清单未通过生产契约校验")

    tts._engine = None
    tts.get_engine = DeterministicHarnessTtsEngine
    tts.get_autopilot_engine = DeterministicHarnessTtsEngine
    asr.get_engine = DeterministicHarnessAsrEngine

    app.add_middleware(harness_marker_middleware_class())
    return app


def seed(config: HarnessConfig) -> dict[str, str]:
    """建临时 admin + 真实研究档案,并用生产 create→approve 走到已审核安排。

    刻意停在 approved:开场(start)由浏览器里的研究者操作完成,那才是彩排要看的
    真实一线流程。approve 在这里成功本身就是核心证据——真实档案 + 冻结题库,
    生产门禁逐项复验后放行。
    """
    _pre_app_import_guard(config)
    from app import db

    _assert_imported_engine_binding(config, db)

    from sqlmodel import Session, select

    from app import auth, visit_plan_service
    from app.models import ConsentType, Patient, ResearchUser
    from app.visit_plan_contract import VisitPlanCreateIn, VisitPlanMutationIn

    with Session(db.engine) as session:
        display_id = auth.validate_display_id(config.actor_username)
        if session.get(ResearchUser, config.actor_username) is None:
            session.add(ResearchUser(
                username=config.actor_username,
                display_id=display_id,
                password_hash=auth.hash_password(config.actor_password),
                role="admin",
                created_at=datetime.now(),
            ))
        if session.get(Patient, REHEARSAL_PATIENT_ID) is None:
            session.add(Patient(
                patient_id=REHEARSAL_PATIENT_ID,
                is_simulation_subject=False,
                consent_status="已同意",
                consent_type=ConsentType.本人同意,
                consent_person="本人",
                mandarin_eligible=True,
                recording_allowed=True,
                secondary_use_allowed=True,
            ))
        session.commit()

        from app.models import VisitPlan

        existing = session.exec(select(VisitPlan).where(
            VisitPlan.patient_id == REHEARSAL_PATIENT_ID,
            VisitPlan.status.in_(("approved", "started")),  # type: ignore[attr-defined]
        )).first()
        if existing is not None:
            return {"plan_id": existing.plan_id,
                    "status": existing.status, "reused": "1"}

        suffix = hashlib.sha256(
            f"{config.root}|{display_id}".encode()).hexdigest()[:16]
        receipt = visit_plan_service.create_plan(
            session,
            body=VisitPlanCreateIn(
                idempotency_key=f"rehearsal-create-{suffix}",
                patient_id=REHEARSAL_PATIENT_ID,
                scheduled_date=date.today(),
                week_no=2,
                phase_type="正式训练",
                event_line="正式训练",
            ),
            actor_id=display_id,
        )
        receipt = visit_plan_service.approve_plan(
            session, plan_id=receipt.plan_id,
            body=VisitPlanMutationIn(
                idempotency_key=f"rehearsal-approve-{suffix}",
                expected_revision=receipt.revision),
            actor_id=display_id)
        session.commit()
        return {"plan_id": receipt.plan_id,
                "status": receipt.status,
                "data_classification": receipt.data_classification,
                "reused": "0"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.research_rehearsal_harness",
        description="HARNESS ONLY —— 真实受试者收人链路彩排入口,禁止用于生产。")
    parser.add_argument("--migrate-and-seed", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8822)
    args = parser.parse_args(argv)
    if not (args.migrate_and_seed or args.serve):
        parser.error("必须显式选择 --migrate-and-seed 或 --serve")

    try:
        config = resolve_rehearsal_config()
        _pre_app_import_guard(config)
    except HarnessConfigError as exc:
        print(f"彩排环境校验失败:{exc}", file=sys.stderr)
        return 2

    if args.migrate_and_seed:
        migrate(config)

    app = install(config)
    print(f"彩排配置:{config.scrubbed()}", file=sys.stderr)

    if args.migrate_and_seed:
        print(f"彩排安排:{seed(config)}", file=sys.stderr)
    if args.serve:
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
