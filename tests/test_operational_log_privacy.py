"""Runtime operational output must never contain research identifiers or exceptions."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from app import main as main_module


SENTINEL = (
    "LEAK-ME patient=P-SENTINEL session=S-SENTINEL turn=T-SENTINEL "
    "token=tok-sentinel path=/private/research/sentinel.wav"
)


def test_audit_record_exception_emits_only_stable_code(monkeypatch, capsys):
    def fail_audit(*_args, **_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(main_module.audit, "record", fail_audit)
    request = SimpleNamespace(state=SimpleNamespace(actor="ACTOR-SENTINEL"))

    # Every value available to this path is deliberately identifying.  The
    # best-effort audit contract must still complete without echoing any of it.
    main_module._audit(
        object(), request, f"action-{SENTINEL}", f"summary-{SENTINEL}",
        patient_id="P-SENTINEL", session_id="S-SENTINEL", turn_id=987654,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "[ops] code=audit_append_failed\n"
    assert SENTINEL not in captured.out


def test_operational_error_code_allowlist_fails_closed(capsys):
    main_module._emit_operational_error(SENTINEL)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "[ops] code=operational_error\n"


def test_runtime_output_static_privacy_contract():
    app_root = Path(main_module.__file__).resolve().parent
    output_methods = {
        "debug", "info", "warning", "error", "exception", "critical",
    }
    sensitive_names = {
        "raw_audio_id", "patient_id", "session_id", "turn_id", "turn_key",
        "token", "path", "actor", "username", "display_id",
    }
    output_calls: list[tuple[Path, ast.Call]] = []

    for source_path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), source_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_print = isinstance(node.func, ast.Name) and node.func.id == "print"
            is_logger = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in output_methods
            )
            if is_print or is_logger:
                output_calls.append((source_path, node))

        # No exception binding may flow to print/logger output, regardless of
        # whether the variable is named e, exc, or something less predictable.
        for handler in (
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ):
            if not handler.name:
                continue
            for statement in handler.body:
                for node in ast.walk(statement):
                    if not isinstance(node, ast.Call):
                        continue
                    is_print = (
                        isinstance(node.func, ast.Name) and node.func.id == "print"
                    )
                    is_logger = (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in output_methods
                    )
                    if not (is_print or is_logger):
                        continue
                    referenced = {
                        child.id for child in ast.walk(node)
                        if isinstance(child, ast.Name)
                    }
                    assert handler.name not in referenced, (
                        f"exception text reaches runtime output: "
                        f"{source_path}:{node.lineno}"
                    )

    # Runtime code has exactly two deliberately narrow output choke points,
    # matched by exact path relative to app_root (not basename/order/lineno).
    assert len(output_calls) == 2
    main_rel = Path("main.py")
    tts_rel = Path("tts.py")

    main_calls = [
        item for item in output_calls
        if item[0].relative_to(app_root) == main_rel
    ]
    tts_calls = [
        item for item in output_calls
        if item[0].relative_to(app_root) == tts_rel
    ]
    assert len(main_calls) == 1
    assert len(tts_calls) == 1

    # 1) main.py's allowlisted safe_code choke point: the full
    # print(f"[ops] code={safe_code}", flush=True) shape, frozen exactly.
    source_path, call = main_calls[0]
    referenced = {
        node.id for node in ast.walk(call) if isinstance(node, ast.Name)
    }
    assert referenced.isdisjoint(sensitive_names)
    assert referenced == {"print", "safe_code"}
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        for node in ast.walk(call)
    )
    assert isinstance(call.func, ast.Name) and call.func.id == "print"
    assert len(call.args) == 1
    assert {kw.arg for kw in call.keywords} == {"flush"}
    flush_kw = next(kw for kw in call.keywords if kw.arg == "flush")
    assert isinstance(flush_kw.value, ast.Constant)
    assert flush_kw.value.value is True

    message = call.args[0]
    assert isinstance(message, ast.JoinedStr)
    assert len(message.values) == 2
    literal_part, formatted_part = message.values
    assert isinstance(literal_part, ast.Constant)
    assert literal_part.value == "[ops] code="
    assert isinstance(formatted_part, ast.FormattedValue)
    assert isinstance(formatted_part.value, ast.Name)
    assert formatted_part.value.id == "safe_code"
    assert formatted_part.conversion == -1
    assert formatted_part.format_spec is None

    # 2) app/tts.py's _log_failure(reason): logs only the closed reason enum,
    # via the module-level logger = logging.getLogger("app.tts") binding.
    tts_path, tts_call = tts_calls[0]
    tts_tree = ast.parse(tts_path.read_text(encoding="utf-8"), tts_path)
    log_failure = next(
        node for node in ast.walk(tts_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_log_failure"
    )
    assert log_failure.lineno <= tts_call.lineno <= log_failure.end_lineno

    logger_binding = next(
        node for node in ast.walk(tts_tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "logger"
    )
    assert isinstance(logger_binding.value, ast.Call)
    assert isinstance(logger_binding.value.func, ast.Attribute)
    assert logger_binding.value.func.attr == "getLogger"
    assert isinstance(logger_binding.value.func.value, ast.Name)
    assert logger_binding.value.func.value.id == "logging"
    assert len(logger_binding.value.args) == 1
    assert isinstance(logger_binding.value.args[0], ast.Constant)
    assert logger_binding.value.args[0].value == "app.tts"

    assert isinstance(tts_call.func, ast.Attribute)
    assert tts_call.func.attr == "warning"
    assert isinstance(tts_call.func.value, ast.Name)
    assert tts_call.func.value.id == "logger"
    assert not tts_call.keywords
    assert len(tts_call.args) == 2
    message_arg, reason_arg = tts_call.args
    assert isinstance(message_arg, ast.Constant)
    assert message_arg.value == "qwen_tts_failed reason=%s"
    assert isinstance(reason_arg, ast.Attribute)
    assert reason_arg.attr == "value"
    assert isinstance(reason_arg.value, ast.Name)
    assert reason_arg.value.id == "reason"

    tts_referenced = {
        node.id for node in ast.walk(tts_call) if isinstance(node, ast.Name)
    }
    assert tts_referenced.isdisjoint(sensitive_names)
    assert tts_referenced == {"logger", "reason"}
