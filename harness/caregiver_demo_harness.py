"""HARNESS ONLY —— 照护员 20 题本机演练的干净库入口。

这不是生产入口，不得用于真实受试者或公网服务。它复用
``harness.tts_ack_harness`` 已经审核过的私有临时 root、路径边界、
迁移、存储重定向、离线 TTS/ASR 和响应 marker，不再复制一套安全
逻辑。本模块只补照护员工作台需要的干净初始态：

* 一个临时 admin 和一个独立 ``caregiver_operator`` 账号；
* 一个明确标为 simulation 的虚构档案；
* 经生产 VisitPlan ``create → approve`` 账本生成、精确停在
  ``approved`` 的 ``week2-single20-demo-v1`` 安排；
* 一条当前配置下真实执行并持久化的 provider readiness 探针。

本入口刻意不调 ``start_plan``：库内必须为零 Session，admin 也不会抢占
床旁 owner。照护员登录后在工作台点「开始」，才由生产照护员端点原子
建立场次并获得 owner。

``synthetic`` 模式会真实执行确定性 TTS→ASR 探针再写入元数据账本；
``production`` 模式也只调同一生产探针，任何云能力失败都持久化为失败并
拒绝继续，绝不手工造 ready 行。

口令环境变量（明文只在当前进程内存，数据库只存散列）：

* ``NMU_HARNESS_PASSWORD``：临时 admin 口令；
* ``NMU_CAREGIVER_PASSWORD``：临时照护员口令，至少 8 位；
* ``NMU_CAREGIVER_USERNAME``：照护员登录名，默认 ``demo-caregiver``。

日常本机演练请直接执行 ``scripts/run-caregiver-demo20.sh``。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .tts_ack_harness import (
    DEMO_PROFILE_VERSION,
    HarnessConfig,
    HarnessConfigError,
    _assert_imported_engine_binding,
    _pre_app_import_guard,
    install as install_base,
    migrate as migrate_base,
    resolve_config,
)

CAREGIVER_USERNAME_ENV = "NMU_CAREGIVER_USERNAME"
CAREGIVER_PASSWORD_ENV = "NMU_CAREGIVER_PASSWORD"
INSTANCE_MARKER_ENV = "NMU_CAREGIVER_HARNESS_INSTANCE"
INSTANCE_MARKER_HEADER = "X-NMU-Caregiver-Harness-Instance"
DEFAULT_CAREGIVER_USERNAME = "demo-caregiver"
CAREGIVER_PATIENT_ID = "SYN-CG-P001"

_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_INSTANCE_MARKER_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")


@dataclass(frozen=True)
class CaregiverDemoConfig:
    """Shared harness boundary plus the distinct caregiver credentials."""

    base: HarnessConfig
    caregiver_username: str
    caregiver_password: str
    instance_marker: str

    def scrubbed(self) -> dict[str, object]:
        """Safe diagnostics; accounts, passwords and pairing PIN stay absent."""
        safe: dict[str, object] = dict(self.base.scrubbed())
        safe.pop("actor", None)
        return safe


def resolve_caregiver_config(
    env: Mapping[str, str] | None = None,
) -> CaregiverDemoConfig:
    """Reuse the shared fail-closed boundary, then validate caregiver identity."""
    source = os.environ if env is None else env
    base = resolve_config(source)
    raw_username = (
        source.get(CAREGIVER_USERNAME_ENV) or DEFAULT_CAREGIVER_USERNAME
    )
    username = raw_username.strip()
    if raw_username != username or not _USERNAME_RE.fullmatch(username):
        raise HarnessConfigError(
            f"{CAREGIVER_USERNAME_ENV} 必须为 1–64 位字母、数字、点、"
            "下划线或连字符，且必须以字母或数字开头"
        )
    if username == base.actor_username:
        raise HarnessConfigError("照护员账号不得与临时 admin 账号相同")
    password = source.get(CAREGIVER_PASSWORD_ENV) or ""
    if len(password) < 8:
        raise HarnessConfigError(
            f"{CAREGIVER_PASSWORD_ENV} 必须显式设置且至少 8 位")
    if password == base.actor_password:
        raise HarnessConfigError("照护员与临时 admin 不得共用口令")
    instance_marker = source.get(INSTANCE_MARKER_ENV) or ""
    if not _INSTANCE_MARKER_RE.fullmatch(instance_marker):
        raise HarnessConfigError(
            f"{INSTANCE_MARKER_ENV} 必须显式设置为 32–128 位随机安全字符")
    return CaregiverDemoConfig(
        base=base,
        caregiver_username=username,
        caregiver_password=password,
        instance_marker=instance_marker,
    )


def migrate(config: CaregiverDemoConfig) -> None:
    """Delegate migration to the shared private-root implementation."""
    migrate_base(config.base)


def install(config: CaregiverDemoConfig):
    """Delegate storage/engine redirection and marker middleware installation."""
    app = install_base(config.base)
    app.add_middleware(
        caregiver_instance_marker_middleware_class(config.instance_marker))
    return app


def caregiver_instance_marker_middleware_class(instance_marker: str):
    """Bind readiness responses to this exact launcher process, not its port."""
    if not _INSTANCE_MARKER_RE.fullmatch(instance_marker):
        raise HarnessConfigError("照护员 harness 实例标记无效")
    from starlette.middleware.base import BaseHTTPMiddleware

    class _Middleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers[INSTANCE_MARKER_HEADER] = instance_marker
            return response

    return _Middleware


def _assert_user_exact(user, *, username: str, password: str, role: str, auth) -> None:
    if (
        user is None
        or user.username != username
        or user.display_id != username
        or user.role != role
        or user.disabled is not False
        or not auth.verify_password(password, user.password_hash)
    ):
        raise HarnessConfigError(
            f"harness root 内的 {role} 账号不是本次要求的精确状态")


def _assert_patient_exact(patient) -> None:
    if (
        patient is None
        or patient.patient_id != CAREGIVER_PATIENT_ID
        or patient.is_simulation_subject is not True
        or patient.consent_status != "已同意"
        or patient.recording_allowed is not True
        or patient.governance_revision != 0
        or (patient.withdrawal_status or "").strip()
    ):
        raise HarnessConfigError(
            "harness root 内的虚构 Patient 不是本次要求的精确模拟状态")


def _assert_plan_exact(
    session,
    plan,
    *,
    admin_display_id: str,
    visit_plan_service,
    autopilot_plan_profiles,
) -> None:
    from sqlmodel import select

    from app.models import VisitPlanCommand
    from app.models import Session as TrainSession

    if plan is None:
        raise HarnessConfigError("harness root 内缺少照护员 demo20 安排")
    if (
        plan.patient_id != CAREGIVER_PATIENT_ID
        or plan.status != "approved"
        or plan.revision != 2
        # Use the exact production research clock, not the host process's
        # local date.  Those can differ around midnight or under a custom TZ.
        or plan.scheduled_date != visit_plan_service._research_today()  # noqa: SLF001
        or plan.scheduled_time is not None
        or plan.queue_order != 0
        or plan.session_sitting_no != 1
        or plan.week_no != 2
        or str(getattr(plan.phase_type, "value", plan.phase_type)) != "正式训练"
        or str(getattr(plan.event_line, "value", plan.event_line)) != "正式训练"
        or plan.autopilot_profile_version_id != DEMO_PROFILE_VERSION
        or plan.is_simulation is not True
        or plan.data_classification != "simulation"
        or plan.created_by != admin_display_id
        or plan.approved_by != admin_display_id
        or plan.started_by is not None
        or plan.started_at is not None
    ):
        raise HarnessConfigError(
            "harness root 内的 demo20 VisitPlan 不是精确 approved 状态")

    # receipt_for 同时复核投影与只追加命令账本，不单看可变列。
    receipt = visit_plan_service.receipt_for(session, plan)
    if receipt.status != "approved" or receipt.session_id is not None:
        raise HarnessConfigError("approved 安排投影不精确")
    commands = list(session.exec(
        select(VisitPlanCommand)
        .where(VisitPlanCommand.plan_id == plan.plan_id)
        .order_by(VisitPlanCommand.event_seq)
    ))
    if (
        [row.command_type for row in commands] != ["create", "approve"]
        or [row.event_seq for row in commands] != [1, 2]
        or [row.expected_revision for row in commands] != [0, 1]
        or [row.resulting_revision for row in commands] != [1, 2]
        or any(row.actor_id != admin_display_id for row in commands)
    ):
        raise HarnessConfigError("demo20 安排的 create→approve 命令账本不精确")
    if session.exec(select(TrainSession)).first() is not None:
        raise HarnessConfigError("caregiver harness 开场前必须保持零 Session")
    try:
        resolved = autopilot_plan_profiles.resolve_exact_runnable_demo20(plan)
    except autopilot_plan_profiles.PlanProfileError as exc:
        raise HarnessConfigError(
            "approved 安排未解析为精确可运行 demo20") from exc
    if resolved.resolved_position_count != 20 or len(resolved.positions) != 20:
        raise HarnessConfigError("approved 安排没有精确解析为 20 个位置")


def _run_and_persist_provider_probe(
    session,
    *,
    actor_display_id: str,
    provider_readiness,
):
    """Run the real configured probe, persist its exact outcome, then project it."""
    before = provider_readiness.capture_configuration()
    result = provider_readiness.run_synthetic_probe(before)
    after = provider_readiness.capture_configuration()
    changed = (
        before.fingerprint != after.fingerprint
        or before.runtime_contract != after.runtime_contract
    )
    provider_readiness.persist_probe(
        session,
        result=result,
        actor_display_id=actor_display_id,
        config_changed_during_probe=changed,
    )
    session.commit()
    projection = provider_readiness.readiness_projection(
        session, configuration=after)
    if projection.start_allowed is not True:
        failures = ",".join(filter(None, (
            projection.probe_failure_code,
            projection.tts.failure_code,
            projection.asr.failure_code,
            projection.llm.failure_code,
        ))) or projection.status
        raise HarnessConfigError(
            "provider readiness 未通过，不得伪造 ready："
            f"status={projection.status} failures={failures}"
        )
    return projection


def _validate_exact_business_state(session, config: CaregiverDemoConfig):
    from sqlmodel import select

    from app import auth, autopilot_plan_profiles, visit_plan_service
    from app.models import Patient, ResearchUser, VisitPlan
    from app.models import Session as TrainSession

    users = list(session.exec(select(ResearchUser).order_by(ResearchUser.username)))
    patients = list(session.exec(select(Patient).order_by(Patient.patient_id)))
    plans = list(session.exec(select(VisitPlan).order_by(VisitPlan.plan_id)))
    sessions = list(session.exec(select(TrainSession).order_by(TrainSession.session_id)))
    if len(users) != 2 or len(patients) != 1 or len(plans) != 1 or sessions:
        raise HarnessConfigError(
            "harness root 已有非精确照护员演练数据，请换新的私有临时 root")

    admin = session.get(ResearchUser, config.base.actor_username)
    caregiver = session.get(ResearchUser, config.caregiver_username)
    _assert_user_exact(
        admin,
        username=config.base.actor_username,
        password=config.base.actor_password,
        role="admin",
        auth=auth,
    )
    _assert_user_exact(
        caregiver,
        username=config.caregiver_username,
        password=config.caregiver_password,
        role="caregiver_operator",
        auth=auth,
    )
    patient = session.get(Patient, CAREGIVER_PATIENT_ID)
    _assert_patient_exact(patient)
    _assert_plan_exact(
        session,
        plans[0],
        admin_display_id=config.base.actor_username,
        visit_plan_service=visit_plan_service,
        autopilot_plan_profiles=autopilot_plan_profiles,
    )
    return plans[0]


def seed(config: CaregiverDemoConfig) -> dict[str, str]:
    """Create or exactly revalidate the two-account, approved-plan demo state."""
    _pre_app_import_guard(config.base)
    from app import db

    _assert_imported_engine_binding(config.base, db)

    from sqlmodel import Session, select

    from app import auth, provider_readiness, visit_plan_service
    from app.models import Patient, ResearchUser, VisitPlan
    from app.models import Session as TrainSession
    from app.visit_plan_contract import VisitPlanCreateIn, VisitPlanMutationIn

    with Session(db.engine) as session:
        existing_users = list(session.exec(select(ResearchUser)))
        existing_patients = list(session.exec(select(Patient)))
        existing_plans = list(session.exec(select(VisitPlan)))
        existing_sessions = list(session.exec(select(TrainSession)))
        has_business_state = any((
            existing_users, existing_patients, existing_plans, existing_sessions))

        if has_business_state:
            plan = _validate_exact_business_state(session, config)
            reused = "1"
        else:
            admin_display_id = auth.validate_display_id(config.base.actor_username)
            caregiver_display_id = auth.validate_display_id(
                config.caregiver_username)
            session.add(ResearchUser(
                username=config.base.actor_username,
                display_id=admin_display_id,
                password_hash=auth.hash_password(config.base.actor_password),
                role="admin",
                created_at=datetime.now(),
            ))
            session.add(ResearchUser(
                username=config.caregiver_username,
                display_id=caregiver_display_id,
                password_hash=auth.hash_password(config.caregiver_password),
                role="caregiver_operator",
                created_at=datetime.now(),
            ))
            session.add(Patient(
                patient_id=CAREGIVER_PATIENT_ID,
                is_simulation_subject=True,
                consent_status="已同意",
                recording_allowed=True,
            ))
            session.commit()

            # Probe before creating an actionable approved plan.  A production
            # provider failure is persisted as failure and stops here.
            _run_and_persist_provider_probe(
                session,
                actor_display_id=admin_display_id,
                provider_readiness=provider_readiness,
            )

            suffix = hashlib.sha256(
                f"{config.base.root}|{admin_display_id}".encode()
            ).hexdigest()[:16]
            receipt = visit_plan_service.create_plan(
                session,
                body=VisitPlanCreateIn(
                    idempotency_key=f"caregiver-demo-create-{suffix}",
                    patient_id=CAREGIVER_PATIENT_ID,
                    scheduled_date=visit_plan_service._research_today(),  # noqa: SLF001
                    week_no=2,
                    phase_type="正式训练",
                    event_line="正式训练",
                    autopilot_profile_version_id=DEMO_PROFILE_VERSION,
                ),
                actor_id=admin_display_id,
            )
            receipt = visit_plan_service.approve_plan(
                session,
                plan_id=receipt.plan_id,
                body=VisitPlanMutationIn(
                    idempotency_key=f"caregiver-demo-approve-{suffix}",
                    expected_revision=receipt.revision,
                ),
                actor_id=admin_display_id,
            )
            session.commit()
            plan = session.get(VisitPlan, receipt.plan_id)
            _validate_exact_business_state(session, config)
            reused = "0"

        # Re-running the seed is a fresh readiness check, not a fabricated or
        # reused ready receipt.  First creation already ran it above.
        if reused == "1":
            projection = _run_and_persist_provider_probe(
                session,
                actor_display_id=config.base.actor_username,
                provider_readiness=provider_readiness,
            )
            plan = _validate_exact_business_state(session, config)
        else:
            projection = provider_readiness.readiness_projection(session)
            if projection.start_allowed is not True:
                raise HarnessConfigError(
                    "初始建场后 provider readiness 投影意外失效")

        assert plan is not None
        return {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "profile_version_id": plan.autopilot_profile_version_id or "",
            "readiness": projection.status,
            "reused": reused,
        }


def validate_seeded_state(config: CaregiverDemoConfig) -> dict[str, str]:
    """Serve gate: accept only the exact approved state and current ready probe."""
    _pre_app_import_guard(config.base)
    from app import db, provider_readiness

    _assert_imported_engine_binding(config.base, db)
    from sqlmodel import Session

    with Session(db.engine) as session:
        plan = _validate_exact_business_state(session, config)
        projection = provider_readiness.readiness_projection(session)
        if projection.start_allowed is not True:
            raise HarnessConfigError(
                "provider readiness 已失效，请重新执行 --migrate-and-seed")
        return {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "profile_version_id": plan.autopilot_profile_version_id or "",
            "readiness": projection.status,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.caregiver_demo_harness",
        description=(
            "HARNESS ONLY —— 照护员 exact 20 题本机演练；"
            "只使用私有临时库，禁止用于生产。"
        ),
    )
    parser.add_argument(
        "--migrate-and-seed",
        action="store_true",
        help="迁移临时库，建两个账号并生成 approved demo20 安排",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="校验已建状态后，仅在 127.0.0.1 启动演练服务",
    )
    parser.add_argument("--port", type=int, default=8815, help="本机回环端口")
    args = parser.parse_args(argv)
    if not (args.migrate_and_seed or args.serve):
        parser.error("必须显式选择 --migrate-and-seed 或 --serve")
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024..65535 之间")

    try:
        config = resolve_caregiver_config()
        _pre_app_import_guard(config.base)
        if args.migrate_and_seed:
            migrate(config)
        app = install(config)
        print(f"照护员演练配置：{config.scrubbed()}", file=sys.stderr)
        if args.migrate_and_seed:
            print(f"照护员演练安排：{seed(config)}", file=sys.stderr)
        if args.serve:
            print(f"照护员演练就绪：{validate_seeded_state(config)}", file=sys.stderr)
            import uvicorn

            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    except HarnessConfigError as exc:
        print(f"照护员 harness 校验失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
