"""explicit repeat intent bindings, replay provenance and append-only ledger

Revision ID: d3f8b5c1a704
Revises: c7d4f9a1e603
Create Date: 2026-07-30

R2: a patient may ask to hear the prompt again.  That request is not an answer,
so it produces a repeat-ledger row instead of an AttemptEvent and never
consumes attempt_seq, a cue or a prompt level.

Every new protocol-binding column is nullable and default-free on the existing
tables, and no migration ever guesses a version for them: a plan, session,
command or capture that predates the approved protocol keeps ``(NULL, NULL)``.
The database only forbids filling exactly one column of a pair.

``attemptcaptureprocessing.repeat_admission_semantics`` is the one deliberate
exception, and it is a marker rather than a binding.  Rows that already existed
when this migration ran are backfilled ``legacy_pre_repeat`` — recording that
they were admitted before the protocol existed — and the column then becomes
NOT NULL with no database default, so every later row must state its admission
semantics explicitly.  A legacy row still carries no binding, no repeat request
and no repeat disposition; it is routed to the dedicated legacy recovery path
instead of the pre-repeat answer flow.

Downgrade is fail-closed.  Once a repeat request, a replayed TTS or a repeat
terminal capture exists there is no lossless way back — the evidence would have
to be deleted or a fake Attempt invented, so the downgrade refuses instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— AutoString 类型


revision: str = "d3f8b5c1a704"
down_revision: Union[str, Sequence[str], None] = "c7d4f9a1e603"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hex64_sql(column: str) -> str:
    """SQL for "this column really is 64 lowercase hex characters".

    Byte-identical to the model-side helper by design, and deliberately not
    imported from it: a migration must keep compiling against the schema it
    shipped with.  Stripping every allowed digit and requiring an empty
    remainder is the portable form — ``GLOB`` is SQLite-only and a syntax
    error on PostgreSQL, which is the deployment target.
    """
    stripped = column
    for digit in "0123456789abcdef":
        stripped = f"replace({stripped}, '{digit}', '')"
    return f"(length({column}) = 64 AND {stripped} = '')"


_REPEAT_BINDING_CHECK = (
    "((repeat_protocol_version_id IS NULL AND "
    "repeat_protocol_definition_digest IS NULL) OR "
    "(repeat_protocol_version_id IS NOT NULL AND "
    "repeat_protocol_definition_digest IS NOT NULL AND "
    "length(trim(repeat_protocol_version_id)) > 0 AND "
    + _hex64_sql("repeat_protocol_definition_digest") + "))"
)

_REPLAY_PROVENANCE_CHECK = (
    "((replay_source_command_id IS NULL AND replay_ordinal IS NULL AND "
    "replay_source_payload_sha256 IS NULL) OR "
    "(replay_source_command_id IS NOT NULL AND "
    "replay_ordinal IS NOT NULL AND replay_ordinal = 1 AND "
    "replay_source_payload_sha256 IS NOT NULL AND "
    + _hex64_sql("replay_source_payload_sha256") + "))"
)

_REPLAY_REQUIRES_REPEAT_BINDING_CHECK = (
    "((replay_source_command_id IS NULL AND replay_ordinal IS NULL AND "
    "replay_source_payload_sha256 IS NULL) OR "
    "(repeat_protocol_version_id IS NOT NULL AND "
    "repeat_protocol_definition_digest IS NOT NULL AND "
    "length(trim(repeat_protocol_version_id)) > 0 AND "
    + _hex64_sql("repeat_protocol_definition_digest") + "))"
)

_ADMISSION_MARKER_BINDING_CHECK = (
    "((repeat_admission_semantics = 'legacy_pre_repeat' AND "
    "repeat_protocol_version_id IS NULL AND "
    "repeat_protocol_definition_digest IS NULL AND "
    "repeat_request_id IS NULL AND "
    "(disposition IS NULL OR disposition = 'answer_candidate')) OR "
    "(repeat_admission_semantics = 'repeat_bound' AND "
    "repeat_protocol_version_id IS NOT NULL AND "
    "repeat_protocol_definition_digest IS NOT NULL AND "
    "length(trim(repeat_protocol_version_id)) > 0 AND "
    + _hex64_sql("repeat_protocol_definition_digest") + "))"
)

_CAPTURE_DISPOSITION_CHECK = (
    "disposition IS NULL OR disposition IN "
    "('answer_candidate','repeat_replayed','repeat_limit_paused')"
)

_CAPTURE_TERMINAL_EXCLUSIVITY_CHECK = (
    "((processing_status = 'received' AND final_attempt_id IS NULL "
    "AND repeat_request_id IS NULL) OR "
    "(processing_status = 'technical_failure' "
    "AND final_attempt_id IS NOT NULL AND repeat_request_id IS NULL) OR "
    "(processing_status = 'asr_completed' "
    "AND disposition IS NOT NULL AND disposition = 'answer_candidate' "
    "AND final_attempt_id IS NOT NULL AND repeat_request_id IS NULL) OR "
    "(processing_status = 'asr_completed' AND disposition IS NOT NULL "
    "AND disposition IN ('repeat_replayed','repeat_limit_paused') "
    "AND final_attempt_id IS NULL AND repeat_request_id IS NOT NULL))"
)

_LEGACY_CAPTURE_DISPOSITION_CHECK = (
    "disposition IS NULL OR disposition IN ('answer_candidate')"
)

_LEGACY_CAPTURE_FINAL_ATTEMPT_CHECK = (
    "((processing_status IN ('asr_completed','technical_failure') "
    "AND final_attempt_id IS NOT NULL) OR "
    "(processing_status = 'received' AND final_attempt_id IS NULL))"
)


def _add_repeat_binding(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(sa.Column(
            "repeat_protocol_version_id",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "repeat_protocol_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_check_constraint(constraint_name, _REPEAT_BINDING_CHECK)


def _drop_repeat_binding(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="check")
        batch_op.drop_column("repeat_protocol_definition_digest")
        batch_op.drop_column("repeat_protocol_version_id")


def upgrade() -> None:
    """Upgrade schema."""
    _add_repeat_binding("visitplan", "ck_visit_plan_repeat_binding_complete")
    _add_repeat_binding("session", "ck_session_repeat_binding_complete")

    with op.batch_alter_table("runtimecommand") as batch_op:
        batch_op.add_column(sa.Column(
            "repeat_protocol_version_id",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "repeat_protocol_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "replay_source_command_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column(
            "replay_ordinal", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column(
            "replay_source_payload_sha256",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_runtime_command_replay_source", ["replay_source_command_id"])
        batch_op.create_foreign_key(
            "fk_runtime_command_replay_source_same_session",
            "runtimecommand",
            ["replay_source_command_id", "session_id"],
            ["id", "session_id"])
        batch_op.create_check_constraint(
            "ck_runtime_command_repeat_binding_complete", _REPEAT_BINDING_CHECK)
        batch_op.create_check_constraint(
            "ck_runtime_command_replay_provenance_complete",
            _REPLAY_PROVENANCE_CHECK)
        batch_op.create_check_constraint(
            "ck_runtime_command_record_never_replays",
            "(kind != 'record' OR (replay_source_command_id IS NULL AND "
            "replay_ordinal IS NULL AND replay_source_payload_sha256 IS NULL))")
        batch_op.create_check_constraint(
            "ck_runtime_command_replay_requires_repeat_binding",
            _REPLAY_REQUIRES_REPEAT_BINDING_CHECK)
    op.create_index(op.f("ix_runtimecommand_replay_source_command_id"),
                    "runtimecommand", ["replay_source_command_id"])

    with op.batch_alter_table("attemptcaptureprocessing") as batch_op:
        batch_op.add_column(sa.Column(
            "repeat_protocol_version_id",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "repeat_protocol_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "repeat_request_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column(
            "repeat_admission_semantics",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_capture_processing_repeat_request_id", ["repeat_request_id"])
        batch_op.drop_constraint(
            "ck_capture_processing_disposition", type_="check")
        batch_op.create_check_constraint(
            "ck_capture_processing_disposition", _CAPTURE_DISPOSITION_CHECK)
        batch_op.drop_constraint(
            "ck_capture_processing_final_attempt_matches_status", type_="check")
        batch_op.create_check_constraint(
            "ck_capture_processing_final_attempt_matches_status",
            _CAPTURE_TERMINAL_EXCLUSIVITY_CHECK)
        batch_op.create_check_constraint(
            "ck_capture_processing_repeat_binding_complete",
            _REPEAT_BINDING_CHECK)
    op.create_index(op.f("ix_attemptcaptureprocessing_repeat_request_id"),
                    "attemptcaptureprocessing", ["repeat_request_id"])
    # Only rows that already existed when this migration ran are legacy.  The
    # column then becomes NOT NULL with no database default, so a row can only
    # ever be created by code that states its admission semantics explicitly.
    op.execute(
        "UPDATE attemptcaptureprocessing "
        "SET repeat_admission_semantics = 'legacy_pre_repeat' "
        "WHERE repeat_admission_semantics IS NULL")
    with op.batch_alter_table("attemptcaptureprocessing") as batch_op:
        batch_op.alter_column(
            "repeat_admission_semantics",
            existing_type=sqlmodel.sql.sqltypes.AutoString(), nullable=False)
        batch_op.create_check_constraint(
            "ck_capture_processing_admission_semantics",
            "repeat_admission_semantics IN ('legacy_pre_repeat','repeat_bound')")
        batch_op.create_check_constraint(
            "ck_capture_processing_admission_marker_binding",
            _ADMISSION_MARKER_BINDING_CHECK)

    op.create_table(
        "autopilotrepeatrequest",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("capture_processing_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("attempt_seq", sa.Integer(), nullable=False),
        sa.Column("prompt_level", sa.Integer(), nullable=False),
        sa.Column("repeat_ordinal", sa.Integer(), nullable=False),
        sa.Column("outcome", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("record_command_id", sa.Integer(), nullable=False),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_tts_command_id", sa.Integer(), nullable=False),
        sa.Column("source_payload_sha256",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("replay_command_id", sa.Integer(), nullable=True),
        sa.Column("pause_control_event_seq", sa.Integer(), nullable=True),
        sa.Column("asr_confidence", sa.Float(), nullable=True),
        sa.Column("asr_engine_version",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("repeat_protocol_version_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("repeat_protocol_definition_digest",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("phrase_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("normalized_text_sha256",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("is_simulation", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.CheckConstraint("turn_seq >= 1", name="ck_repeat_request_turn_positive"),
        sa.CheckConstraint("attempt_seq >= 1",
                           name="ck_repeat_request_attempt_positive"),
        sa.CheckConstraint("prompt_level >= 0 AND prompt_level <= 3",
                           name="ck_repeat_request_prompt_level"),
        sa.CheckConstraint("repeat_ordinal IN (1, 2)",
                           name="ck_repeat_request_ordinal"),
        sa.CheckConstraint("outcome IN ('replayed','limit_paused')",
                           name="ck_repeat_request_outcome"),
        sa.CheckConstraint(
            "((repeat_ordinal = 1 AND outcome = 'replayed' AND "
            "replay_command_id IS NOT NULL AND "
            "pause_control_event_seq IS NULL) OR "
            "(repeat_ordinal = 2 AND outcome = 'limit_paused' AND "
            "replay_command_id IS NULL AND "
            "pause_control_event_seq IS NOT NULL))",
            name="ck_repeat_request_ordinal_matches_outcome"),
        sa.CheckConstraint(
            "pause_control_event_seq IS NULL OR pause_control_event_seq >= 1",
            name="ck_repeat_request_pause_event_seq_positive"),
        sa.CheckConstraint(
            _hex64_sql("source_payload_sha256"),
            name="ck_repeat_request_source_payload_digest"),
        sa.CheckConstraint(
            _hex64_sql("normalized_text_sha256"),
            name="ck_repeat_request_normalized_text_digest"),
        sa.CheckConstraint(
            "length(trim(repeat_protocol_version_id)) > 0 AND "
            + _hex64_sql("repeat_protocol_definition_digest"),
            name="ck_repeat_request_protocol_binding"),
        sa.CheckConstraint("length(trim(phrase_key)) > 0",
                           name="ck_repeat_request_phrase_key"),
        sa.ForeignKeyConstraint(["capture_processing_id"],
                                ["attemptcaptureprocessing.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(["raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.ForeignKeyConstraint(
            ["record_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_repeat_request_record_command_same_session"),
        sa.ForeignKeyConstraint(
            ["source_tts_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_repeat_request_source_command_same_session"),
        sa.ForeignKeyConstraint(
            ["replay_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_repeat_request_replay_command_same_session"),
        sa.ForeignKeyConstraint(
            ["session_id", "pause_control_event_seq"],
            ["autopilotcontrolevent.session_id",
             "autopilotcontrolevent.event_seq"],
            name="fk_repeat_request_pause_event_same_session"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("capture_processing_id",
                            name="uq_repeat_request_capture_processing"),
        sa.UniqueConstraint("session_id", "item_id", "turn_seq", "attempt_seq",
                            "prompt_level", "repeat_ordinal",
                            name="uq_repeat_request_logical_slot_ordinal"),
        sa.UniqueConstraint("replay_command_id",
                            name="uq_repeat_request_replay_command"),
        sa.UniqueConstraint("source_tts_command_id",
                            name="uq_repeat_request_source_command"),
        sa.UniqueConstraint("session_id", "pause_control_event_seq",
                            name="uq_repeat_request_pause_event"),
    )
    op.create_index("ix_repeat_request_session_created",
                    "autopilotrepeatrequest", ["session_id", "created_at"])
    op.create_index(op.f("ix_autopilotrepeatrequest_capture_processing_id"),
                    "autopilotrepeatrequest", ["capture_processing_id"])
    op.create_index(op.f("ix_autopilotrepeatrequest_session_id"),
                    "autopilotrepeatrequest", ["session_id"])
    op.create_index(op.f("ix_autopilotrepeatrequest_item_id"),
                    "autopilotrepeatrequest", ["item_id"])
    op.create_index(op.f("ix_autopilotrepeatrequest_record_command_id"),
                    "autopilotrepeatrequest", ["record_command_id"])
    op.create_index(op.f("ix_autopilotrepeatrequest_raw_audio_id"),
                    "autopilotrepeatrequest", ["raw_audio_id"])

    # The reverse half of the one-to-one binding.  It can only be created once
    # the ledger table exists, so it is added here rather than inside the
    # capture batch above.
    with op.batch_alter_table("attemptcaptureprocessing") as batch_op:
        batch_op.create_foreign_key(
            "fk_capture_processing_repeat_request",
            "autopilotrepeatrequest", ["repeat_request_id"], ["id"])


_REPEAT_BOUND_TABLES = (
    ("visitplan", "训练安排"),
    ("session", "场次"),
    ("runtimecommand", "运行命令"),
    ("attemptcaptureprocessing", "采集处理行"),
)


def _assert_no_repeat_evidence() -> None:
    """Refuse to erase repeat semantics; never delete them or forge an Attempt.

    A frozen ``(version_id, definition_digest)`` binding is itself irreplaceable
    evidence, not just a convenience column: dropping it would leave a session
    whose repeat semantics were approved and recorded looking exactly like one
    that predates the protocol, and re-upgrading would then mark its captures
    ``legacy_pre_repeat``.  A patient who said "再说一遍" under the approved
    protocol would afterwards be scored as having answered it.  So the downgrade
    only accepts data that is genuinely all-NULL pre-d3 data.
    """
    connection = op.get_bind()
    for table, label in _REPEAT_BOUND_TABLES:
        bound = connection.execute(sa.text(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE repeat_protocol_version_id IS NOT NULL "
            "OR repeat_protocol_definition_digest IS NOT NULL")).scalar()
        if bound:
            raise RuntimeError(
                f"存在已冻结重复请求协议绑定的{label}，禁止降级："
                "降级会删除不可重建的协议绑定证据")
    bound_captures = connection.execute(sa.text(
        "SELECT COUNT(*) FROM attemptcaptureprocessing "
        "WHERE repeat_admission_semantics = 'repeat_bound'")).scalar()
    if bound_captures:
        raise RuntimeError(
            "存在按重复请求协议准入的采集处理行，禁止降级："
            "降级后无法区分它与协议之前的历史采集")
    requests = connection.execute(
        sa.text("SELECT COUNT(*) FROM autopilotrepeatrequest")).scalar()
    if requests:
        raise RuntimeError(
            "存在 explicit repeat 账本记录，禁止降级："
            "降级会删除不可重建的重播证据")
    replays = connection.execute(sa.text(
        "SELECT COUNT(*) FROM runtimecommand "
        "WHERE replay_source_command_id IS NOT NULL "
        "OR replay_ordinal IS NOT NULL "
        "OR replay_source_payload_sha256 IS NOT NULL")).scalar()
    if replays:
        raise RuntimeError(
            "存在 replay TTS 溯源记录，禁止降级：降级会丢失重播命令来源证据")
    captures = connection.execute(sa.text(
        "SELECT COUNT(*) FROM attemptcaptureprocessing "
        "WHERE repeat_request_id IS NOT NULL "
        "OR disposition IN ('repeat_replayed','repeat_limit_paused')")).scalar()
    if captures:
        raise RuntimeError(
            "存在 repeat 终态采集处理行，禁止降级："
            "这些采集没有 AttemptEvent，降级无法诚实表示")


def downgrade() -> None:
    """Downgrade schema."""
    _assert_no_repeat_evidence()

    # The reverse one-to-one foreign key must go before its target table: batch
    # mode reflects the referenced table while rebuilding the capture table.
    with op.batch_alter_table("attemptcaptureprocessing") as batch_op:
        batch_op.drop_constraint(
            "fk_capture_processing_repeat_request", type_="foreignkey")

    op.drop_index(op.f("ix_autopilotrepeatrequest_raw_audio_id"),
                  table_name="autopilotrepeatrequest")
    op.drop_index(op.f("ix_autopilotrepeatrequest_record_command_id"),
                  table_name="autopilotrepeatrequest")
    op.drop_index(op.f("ix_autopilotrepeatrequest_item_id"),
                  table_name="autopilotrepeatrequest")
    op.drop_index(op.f("ix_autopilotrepeatrequest_session_id"),
                  table_name="autopilotrepeatrequest")
    op.drop_index(op.f("ix_autopilotrepeatrequest_capture_processing_id"),
                  table_name="autopilotrepeatrequest")
    op.drop_index("ix_repeat_request_session_created",
                  table_name="autopilotrepeatrequest")
    op.drop_table("autopilotrepeatrequest")

    op.drop_index(op.f("ix_attemptcaptureprocessing_repeat_request_id"),
                  table_name="attemptcaptureprocessing")
    with op.batch_alter_table("attemptcaptureprocessing") as batch_op:
        batch_op.drop_constraint(
            "ck_capture_processing_admission_marker_binding", type_="check")
        batch_op.drop_constraint(
            "ck_capture_processing_admission_semantics", type_="check")
        batch_op.drop_column("repeat_admission_semantics")
        batch_op.drop_constraint(
            "ck_capture_processing_repeat_binding_complete", type_="check")
        batch_op.drop_constraint(
            "ck_capture_processing_final_attempt_matches_status", type_="check")
        batch_op.create_check_constraint(
            "ck_capture_processing_final_attempt_matches_status",
            _LEGACY_CAPTURE_FINAL_ATTEMPT_CHECK)
        batch_op.drop_constraint(
            "ck_capture_processing_disposition", type_="check")
        batch_op.create_check_constraint(
            "ck_capture_processing_disposition",
            _LEGACY_CAPTURE_DISPOSITION_CHECK)
        batch_op.drop_constraint(
            "uq_capture_processing_repeat_request_id", type_="unique")
        batch_op.drop_column("repeat_request_id")
        batch_op.drop_column("repeat_protocol_definition_digest")
        batch_op.drop_column("repeat_protocol_version_id")

    op.drop_index(op.f("ix_runtimecommand_replay_source_command_id"),
                  table_name="runtimecommand")
    with op.batch_alter_table("runtimecommand") as batch_op:
        batch_op.drop_constraint(
            "ck_runtime_command_replay_requires_repeat_binding", type_="check")
        batch_op.drop_constraint(
            "ck_runtime_command_record_never_replays", type_="check")
        batch_op.drop_constraint(
            "ck_runtime_command_replay_provenance_complete", type_="check")
        batch_op.drop_constraint(
            "ck_runtime_command_repeat_binding_complete", type_="check")
        batch_op.drop_constraint(
            "fk_runtime_command_replay_source_same_session", type_="foreignkey")
        batch_op.drop_constraint(
            "uq_runtime_command_replay_source", type_="unique")
        batch_op.drop_column("replay_source_payload_sha256")
        batch_op.drop_column("replay_ordinal")
        batch_op.drop_column("replay_source_command_id")
        batch_op.drop_column("repeat_protocol_definition_digest")
        batch_op.drop_column("repeat_protocol_version_id")

    _drop_repeat_binding("session", "ck_session_repeat_binding_complete")
    _drop_repeat_binding("visitplan", "ck_visit_plan_repeat_binding_complete")
