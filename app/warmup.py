"""Optional, bounded Week-1 conversation. No recording, scoring or AI inputs."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import content

VERSION = "wk1-warmup-v1"
FILE = "week1_warmup_v1.json"
Topic = Literal["weather", "food", "sleep", "activity"]


class _Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Topic
    label: str = Field(min_length=1, max_length=10)
    ask: str = Field(min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$")
    reply: str = Field(min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$")


class Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    versionId: Literal["wk1-warmup-v1"]
    topics: list[_Topic] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def closed_topics(self):
        if [row.id for row in self.topics] != ["weather", "food", "sleep", "activity"]:
            raise ValueError("warmup topics must be the four ordered topics")
        return self


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    versionId: Literal["wk1-warmup-v1"]
    topic: Topic
    beat: Literal["ask", "reply", "done"]
    seq: int = Field(ge=0, strict=True)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "replay", "reply", "end", "continue"]
    topic: Topic | None = None
    expectedWseq: int = Field(ge=0, strict=True)
    expectedRevision: int = Field(ge=0, strict=True)
    idempotencyKey: str = Field(min_length=8, max_length=128,
                                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

    @model_validator(mode="after")
    def topic_only_on_start(self):
        if (self.action == "start") != (self.topic is not None):
            raise ValueError("topic is required only for start")
        return self


def load_bundle() -> Bundle:
    try:
        data = content._load_strict_json_object(content.CONTENT_DIR / FILE,
                                                label="训练前互动")
        return Bundle.model_validate(data)
    except (OSError, ValueError, TypeError) as exc:
        raise content.FrozenContentUnavailable("warmup content unavailable") from exc


def state(payload: dict | None) -> State | None:
    value = (payload or {}).get("warmup")
    return State.model_validate(value) if value is not None else None


def text_for(current: State) -> str | None:
    bundle = load_bundle()
    if current.beat == "done":
        return None
    row = next(row for row in bundle.topics if row.id == current.topic)
    return row.ask if current.beat == "ask" else row.reply


def quiesce(payload: dict) -> dict:
    """Pause/recovery never implicitly starts another optional conversation."""
    result = dict(payload)
    current = state(result)
    if current is not None:
        result["warmup"] = current.model_copy(update={"beat": "done"}).model_dump()
    return result


def action_digest(action: Action) -> str:
    return hashlib.sha256(json.dumps(action.model_dump(), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
