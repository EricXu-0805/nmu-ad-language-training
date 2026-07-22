"""server-authoritative autopilot command and control ledger

Revision ID: 6a1d9c3e7f24
Revises: f4b8c1d6a702
Create Date: 2026-07-18

Purely additive: existing sessions, runtime cursors, audio, attempts and research
truth are untouched.  New autopilot rows are created only by later service logic.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "6a1d9c3e7f24"
down_revision: Union[str, Sequence[str], None] = "f4b8c1d6a702"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Persist the patient device's ACK high-water mark on its capability row so
    # process restarts and competing API workers cannot accept an older event.
    # The server default backfills capabilities created by the preceding revision.
    with op.batch_alter_table("patientdevicecapability") as batch_op:
        batch_op.add_column(sa.Column(
            "last_autopilot_event_seq", sa.Integer(), nullable=False,
            server_default=sa.text("0")))
        batch_op.create_check_constraint(
            "ck_patient_device_capability_autopilot_seq_nonnegative",
            "last_autopilot_event_seq >= 0")

    op.create_table(
        "runtimecommand",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("command_seq", sa.Integer(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("turn_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempt_seq", sa.Integer(), nullable=False),
        sa.Column("prompt_level", sa.Integer(), nullable=False),
        sa.Column("scope_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("control_generation", sa.Integer(), nullable=False),
        sa.Column("runner_generation", sa.Integer(), nullable=False),
        sa.Column("issued_capability_token_hash",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("issued_device_id_hash",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("predecessor_command_id", sa.Integer(), nullable=True),
        sa.Column("trigger_ack_idempotency_key",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("expected_raw_audio_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("payload_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("result_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("lease_owner", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("succeeded_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("command_seq >= 1",
                           name="ck_runtime_command_seq_positive"),
        sa.CheckConstraint("turn_seq >= 1",
                           name="ck_runtime_command_turn_positive"),
        sa.CheckConstraint("attempt_seq >= 1",
                           name="ck_runtime_command_attempt_positive"),
        sa.CheckConstraint("prompt_level >= 0 AND prompt_level <= 3",
                           name="ck_runtime_command_prompt_level"),
        sa.CheckConstraint("control_generation >= 1",
                           name="ck_runtime_command_control_generation"),
        sa.CheckConstraint("runner_generation >= 1",
                           name="ck_runtime_command_runner_generation"),
        sa.CheckConstraint("revision >= 0", name="ck_runtime_command_revision"),
        sa.CheckConstraint("scope_key IN ('p0a_sim_first_single_v1')",
                           name="ck_runtime_command_scope"),
        sa.CheckConstraint("kind IN ('tts','record')",
                           name="ck_runtime_command_kind"),
        sa.CheckConstraint(
            "state IN ('pending','started','succeeded','failed','cancelled')",
            name="ck_runtime_command_state"),
        sa.CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_runtime_command_lease_complete"),
        sa.CheckConstraint(
            "(state NOT IN ('succeeded','failed','cancelled') OR "
            "(lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_runtime_command_terminal_lease_released"),
        sa.CheckConstraint(
            "((kind = 'tts' AND predecessor_command_id IS NULL AND "
            "trigger_ack_idempotency_key IS NULL AND expected_raw_audio_id IS NULL) OR "
            "(kind = 'record' AND predecessor_command_id IS NOT NULL AND "
            "trigger_ack_idempotency_key IS NOT NULL AND "
            "expected_raw_audio_id IS NOT NULL))",
            name="ck_runtime_command_record_prerequisite"),
        sa.CheckConstraint("(state != 'started' OR started_at IS NOT NULL)",
                           name="ck_runtime_command_started_timestamp"),
        sa.CheckConstraint("(state != 'succeeded' OR succeeded_at IS NOT NULL)",
                           name="ck_runtime_command_succeeded_timestamp"),
        sa.CheckConstraint("(state != 'failed' OR failed_at IS NOT NULL)",
                           name="ck_runtime_command_failed_timestamp"),
        sa.CheckConstraint("(state != 'cancelled' OR cancelled_at IS NOT NULL)",
                           name="ck_runtime_command_cancelled_timestamp"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["issued_capability_token_hash"],
            ["patientdevicecapability.token_hash"]),
        sa.ForeignKeyConstraint(["expected_raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.ForeignKeyConstraint(
            ["predecessor_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_runtime_command_predecessor_same_session"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key",
                            name="uq_runtime_command_idempotency_key"),
        sa.UniqueConstraint("id", "session_id",
                            name="uq_runtime_command_id_session"),
        sa.UniqueConstraint("session_id", "command_seq",
                            name="uq_runtime_command_session_seq"),
        sa.UniqueConstraint("predecessor_command_id",
                            name="uq_runtime_command_predecessor"),
        sa.UniqueConstraint("expected_raw_audio_id",
                            name="uq_runtime_command_expected_raw_audio"),
    )
    op.create_index("ix_runtimecommand_session_id", "runtimecommand",
                    ["session_id"], unique=False)
    op.create_index("ix_runtimecommand_item_id", "runtimecommand",
                    ["item_id"], unique=False)
    op.create_index("ix_runtimecommand_issued_capability_token_hash",
                    "runtimecommand", ["issued_capability_token_hash"], unique=False)
    op.create_index("ix_runtimecommand_predecessor_command_id", "runtimecommand",
                    ["predecessor_command_id"], unique=False)
    op.create_index("ix_runtimecommand_expected_raw_audio_id", "runtimecommand",
                    ["expected_raw_audio_id"], unique=False)
    op.create_index("ix_runtime_command_session_state_lease", "runtimecommand",
                    ["session_id", "state", "lease_expires_at"], unique=False)

    op.create_table(
        "runtimecommandack",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ack_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("command_revision", sa.Integer(), nullable=False),
        sa.Column("control_generation", sa.Integer(), nullable=False),
        sa.Column("runner_generation", sa.Integer(), nullable=False),
        sa.Column("device_event_seq", sa.Integer(), nullable=False),
        sa.Column("device_id_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("capability_token_hash",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payload_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("receipt_server_seq", sa.Integer(), nullable=True),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("checksum", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("byte_count", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("device_event_seq >= 1",
                           name="ck_runtime_command_ack_device_seq_positive"),
        sa.CheckConstraint("command_revision >= 0",
                           name="ck_runtime_command_ack_command_revision"),
        sa.CheckConstraint("control_generation >= 1",
                           name="ck_runtime_command_ack_control_generation"),
        sa.CheckConstraint("runner_generation >= 1",
                           name="ck_runtime_command_ack_runner_generation"),
        sa.CheckConstraint(
            "ack_type IN ('tts_started','tts_ended','tts_failed',"
            "'record_started','record_stopped','record_failed')",
            name="ck_runtime_command_ack_type"),
        sa.CheckConstraint(
            "((ack_type = 'record_stopped' AND receipt_server_seq IS NOT NULL AND "
            "raw_audio_id IS NOT NULL AND checksum IS NOT NULL AND "
            "byte_count IS NOT NULL AND duration_seconds IS NOT NULL) OR "
            "(ack_type != 'record_stopped' AND receipt_server_seq IS NULL AND "
            "raw_audio_id IS NULL AND checksum IS NULL AND byte_count IS NULL AND "
            "duration_seconds IS NULL))",
            name="ck_runtime_command_ack_capture_tuple"),
        sa.CheckConstraint("byte_count IS NULL OR byte_count > 0",
                           name="ck_runtime_command_ack_bytes_positive"),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR "
            "(duration_seconds >= 0 AND duration_seconds <= 21600)",
            name="ck_runtime_command_ack_duration"),
        sa.ForeignKeyConstraint(
            ["command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_runtime_command_ack_command_same_session"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["capability_token_hash"], ["patientdevicecapability.token_hash"]),
        sa.ForeignKeyConstraint(
            ["receipt_server_seq"], ["audiocapturereceipt.server_seq"]),
        sa.ForeignKeyConstraint(["raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", "idempotency_key",
                            name="uq_runtime_command_ack_command_idempotency"),
        sa.UniqueConstraint("command_id", "ack_type",
                            name="uq_runtime_command_ack_command_type"),
        sa.UniqueConstraint("session_id", "device_id_hash", "device_event_seq",
                            name="uq_runtime_command_ack_device_seq"),
    )
    op.create_index("ix_runtimecommandack_command_id", "runtimecommandack",
                    ["command_id"], unique=False)
    op.create_index("ix_runtimecommandack_session_id", "runtimecommandack",
                    ["session_id"], unique=False)
    op.create_index("ix_runtimecommandack_ack_type", "runtimecommandack",
                    ["ack_type"], unique=False)
    op.create_index("ix_runtimecommandack_capability_token_hash", "runtimecommandack",
                    ["capability_token_hash"], unique=False)
    op.create_index("ix_runtimecommandack_receipt_server_seq", "runtimecommandack",
                    ["receipt_server_seq"], unique=False)
    op.create_index("ix_runtimecommandack_raw_audio_id", "runtimecommandack",
                    ["raw_audio_id"], unique=False)
    op.create_index("ix_runtime_command_ack_command_type", "runtimecommandack",
                    ["command_id", "ack_type"], unique=False)

    op.create_table(
        "sessionautopilotstate",
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scope_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("mode", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("control_generation", sa.Integer(), nullable=False),
        sa.Column("runner_generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_command_seq", sa.Integer(), nullable=False),
        sa.Column("current_command_id", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("lease_acquired_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("control_generation >= 0",
                           name="ck_session_autopilot_control_generation"),
        sa.CheckConstraint("runner_generation >= 0",
                           name="ck_session_autopilot_runner_generation"),
        sa.CheckConstraint("revision >= 0",
                           name="ck_session_autopilot_revision"),
        sa.CheckConstraint("next_command_seq >= 1",
                           name="ck_session_autopilot_next_command_seq"),
        sa.CheckConstraint("scope_key IN ('disabled','p0a_sim_first_single_v1')",
                           name="ck_session_autopilot_scope"),
        sa.CheckConstraint("mode IN ('disabled','autonomous','manual')",
                           name="ck_session_autopilot_mode"),
        sa.CheckConstraint(
            "status IN ('idle','running','waiting_tts','waiting_recording',"
            "'processing_attempt','manual_draining','paused','scope_completed',"
            "'failed')",
            name="ck_session_autopilot_status"),
        sa.CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL AND "
            "lease_acquired_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND "
            "lease_acquired_at IS NOT NULL))",
            name="ck_session_autopilot_lease_complete"),
        sa.CheckConstraint(
            "((scope_key = 'disabled' AND mode = 'disabled' AND status = 'idle' "
            "AND control_generation = 0 AND runner_generation = 0 "
            "AND current_command_id IS NULL) OR "
            "(scope_key = 'p0a_sim_first_single_v1' "
            "AND mode IN ('autonomous','manual')))",
            name="ck_session_autopilot_scope_mode"),
        sa.CheckConstraint(
            "((status IN ('waiting_tts','waiting_recording','processing_attempt',"
            "'manual_draining') AND current_command_id IS NOT NULL) OR "
            "status NOT IN ('waiting_tts','waiting_recording','processing_attempt',"
            "'manual_draining'))",
            name="ck_session_autopilot_waiting_has_command"),
        sa.CheckConstraint(
            "status != 'scope_completed' OR current_command_id IS NULL",
            name="ck_session_autopilot_completed_clears_command"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["current_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_session_autopilot_current_command_same_session"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index("ix_sessionautopilotstate_current_command_id",
                    "sessionautopilotstate", ["current_command_id"], unique=False)
    op.create_index("ix_session_autopilot_state_lease", "sessionautopilotstate",
                    ["lease_expires_at"], unique=False)

    op.create_table(
        "autopilotcontrolevent",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scope_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("control_generation", sa.Integer(), nullable=False),
        sa.Column("runner_generation", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=True),
        sa.Column("actor_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reason_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("from_mode", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("to_mode", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("from_status", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("to_status", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("payload_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("event_seq >= 1",
                           name="ck_autopilot_control_event_seq_positive"),
        sa.CheckConstraint("control_generation >= 0",
                           name="ck_autopilot_control_event_control_generation"),
        sa.CheckConstraint("runner_generation >= 0",
                           name="ck_autopilot_control_event_runner_generation"),
        sa.CheckConstraint("scope_key IN ('disabled','p0a_sim_first_single_v1')",
                           name="ck_autopilot_control_event_scope"),
        sa.CheckConstraint(
            "event_type IN ('start','pause','resume','takeover','drain_complete',"
            "'generation_bump','failure','scope_complete')",
            name="ck_autopilot_control_event_type"),
        sa.CheckConstraint("actor_type IN ('system','researcher','device')",
                           name="ck_autopilot_control_event_actor_type"),
        sa.CheckConstraint(
            "from_mode IS NULL OR from_mode IN ('disabled','autonomous','manual')",
            name="ck_autopilot_control_event_from_mode"),
        sa.CheckConstraint(
            "to_mode IS NULL OR to_mode IN ('disabled','autonomous','manual')",
            name="ck_autopilot_control_event_to_mode"),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('idle','running','waiting_tts','waiting_recording',"
            "'processing_attempt','manual_draining','paused','scope_completed',"
            "'failed')",
            name="ck_autopilot_control_event_from_status"),
        sa.CheckConstraint(
            "to_status IS NULL OR to_status IN "
            "('idle','running','waiting_tts','waiting_recording',"
            "'processing_attempt','manual_draining','paused','scope_completed',"
            "'failed')",
            name="ck_autopilot_control_event_to_status"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_autopilot_control_command_same_session"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key",
                            name="uq_autopilot_control_event_idempotency"),
        sa.UniqueConstraint("session_id", "event_seq",
                            name="uq_autopilot_control_event_session_seq"),
    )
    op.create_index("ix_autopilotcontrolevent_session_id", "autopilotcontrolevent",
                    ["session_id"], unique=False)
    op.create_index("ix_autopilotcontrolevent_event_type", "autopilotcontrolevent",
                    ["event_type"], unique=False)
    op.create_index("ix_autopilotcontrolevent_command_id", "autopilotcontrolevent",
                    ["command_id"], unique=False)
    op.create_index("ix_autopilot_control_event_session_created",
                    "autopilotcontrolevent", ["session_id", "created_at"], unique=False)


def downgrade() -> None:
    # Development-only rollback. Production research databases migrate forward.
    op.drop_index("ix_autopilot_control_event_session_created",
                  table_name="autopilotcontrolevent")
    op.drop_index("ix_autopilotcontrolevent_command_id",
                  table_name="autopilotcontrolevent")
    op.drop_index("ix_autopilotcontrolevent_event_type",
                  table_name="autopilotcontrolevent")
    op.drop_index("ix_autopilotcontrolevent_session_id",
                  table_name="autopilotcontrolevent")
    op.drop_table("autopilotcontrolevent")

    op.drop_index("ix_session_autopilot_state_lease",
                  table_name="sessionautopilotstate")
    op.drop_index("ix_sessionautopilotstate_current_command_id",
                  table_name="sessionautopilotstate")
    op.drop_table("sessionautopilotstate")

    op.drop_index("ix_runtime_command_ack_command_type",
                  table_name="runtimecommandack")
    op.drop_index("ix_runtimecommandack_raw_audio_id",
                  table_name="runtimecommandack")
    op.drop_index("ix_runtimecommandack_receipt_server_seq",
                  table_name="runtimecommandack")
    op.drop_index("ix_runtimecommandack_capability_token_hash",
                  table_name="runtimecommandack")
    op.drop_index("ix_runtimecommandack_ack_type",
                  table_name="runtimecommandack")
    op.drop_index("ix_runtimecommandack_session_id",
                  table_name="runtimecommandack")
    op.drop_index("ix_runtimecommandack_command_id",
                  table_name="runtimecommandack")
    op.drop_table("runtimecommandack")

    op.drop_index("ix_runtime_command_session_state_lease",
                  table_name="runtimecommand")
    op.drop_index("ix_runtimecommand_expected_raw_audio_id",
                  table_name="runtimecommand")
    op.drop_index("ix_runtimecommand_predecessor_command_id",
                  table_name="runtimecommand")
    op.drop_index("ix_runtimecommand_item_id", table_name="runtimecommand")
    op.drop_index("ix_runtimecommand_issued_capability_token_hash",
                  table_name="runtimecommand")
    op.drop_index("ix_runtimecommand_session_id", table_name="runtimecommand")
    op.drop_table("runtimecommand")

    with op.batch_alter_table("patientdevicecapability") as batch_op:
        batch_op.drop_constraint(
            "ck_patient_device_capability_autopilot_seq_nonnegative",
            type_="check")
        batch_op.drop_column("last_autopilot_event_seq")
