#!/usr/bin/env python3
"""生产空闲门:数 nmu 访问日志里近 N 分钟的真实使用请求,0 条才算空闲。

存在的理由:2026-11-03 起国内入口(droplet 上的 haproxy 按 TCP 原样转到本机 443)把互联网
扫描器带了进来,日志里成串的 GET /.env、/.git/config、/actuator/env……都被 SPA 兜底回成
index.html,状态 200(与 /patient 同一份外壳,没有漏文件)。旧过滤是一串 grep:不在轮询与
页面名单上的请求都算在用,扫描器一扫空闲门就过不去,11-05 晚上线脚本 277 就停在这一步。

这里反过来:方法 + 路径对得上应用的一条真路由,才可能算在用,再减去轮询与 401;对不上
任何真路由的一律是噪声,页面外壳与静态文件也不算。路由表是抄在下面的字面量:这个脚本经
ssh 的 stdin 送到生产机、用系统 python3 跑,不能 import app/。tests/test_prod_idle_gate.py
拿 app.routes 逐条对表,新加的接口忘了进表就红,不会悄悄变成噪声。表跟的是本 checkout:
删或改名接口的那次上线,换版前要跑生产当前 SHA 那一版的这份脚本,旧接口的真请求才不会算成噪声。

退出码:0 空闲;1 有人在用;2 journal 读不了或有解析不了的访问行(调用方一律按不空闲算)。
只打一行中文汇总,只有各类计数:不打路径、IP、ID、查询串。

只用标准库,兼容 Python 3.10(生产机 Ubuntu 22.04 的系统 python3)。在 Mac 上:
  ssh root@<生产机> '/usr/bin/python3 -I - --minutes 10' < scripts/prod_idle_gate.py
ssh 带 -n(或 stdin 接了 /dev/null)时远端跑的是空程序,也退 0:没打出那行汇总同样按不空闲算。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# 旧过滤排除的那一批轮询,按方法收紧:旧 grep 只认路径子串,连 PUT /live/state(操作端写游标、
# 老人端报录音)和 POST /ai/provider-readiness/probe(管理员点「实测」)也一并放过,这两条现在算在用。
# /device/heartbeat 与 /build-id.txt 不是应用路由,落到下面的噪声与页面里,照样不算。
POLLING = """
GET /health
GET /health/ready
HEAD /health/ready
POST /device/attach
GET /live/state
POST /live/patient-heartbeat
GET /sessions/{session_id}/autopilot/next
GET /sessions/{session_id}/autopilot/voice-settings
GET /sessions/{session_id}/autopilot/status
GET /caregiver/sessions/{session_id}/status
GET /sessions/{session_id}/runtime
GET /ai/provider-readiness
"""

REAL = """
POST /ai/provider-readiness/probe
GET /asr/hotwords
POST /asr/transcribe/{raw_audio_id}
GET /assessment-events/today
GET /assessment-events/{event_id}
POST /assessment-events/{event_id}/cancel
POST /assessment-events/{event_id}/close
POST /assessment-events/{event_id}/start
POST /assessment-instances/{instance_id}/approve-defer
POST /assessment-instances/{instance_id}/complete
POST /assessment-instances/{instance_id}/recording-authorizations
PUT /assessment-instances/{instance_id}/responses/{item_key}
POST /audio
DELETE /audio/{raw_audio_id}
GET /audio/{raw_audio_id}
GET /audio/{raw_audio_id}/blob
PUT /audio/{raw_audio_id}/blob
POST /audio/{raw_audio_id}/checksum
POST /audio/{raw_audio_id}/export
POST /audio/{raw_audio_id}/reliability-review
GET /audit
GET /audit/verify
GET /auth/config
POST /auth/login
POST /auth/logout
GET /auth/me
PUT /caregiver/sessions/{session_id}/activation
POST /caregiver/sessions/{session_id}/help-requests
GET /caregiver/sessions/{session_id}/help-requests/{request_id}
POST /caregiver/sessions/{session_id}/help-requests/{request_id}/dispositions
GET /caregiver/today
POST /caregiver/visit-plans/{plan_id}/start
GET /cloud-processing/policy
GET /content/autopilot-protocol
HEAD /content/autopilot-protocol
GET /content/item-bank
GET /content/item-bank-bundle
HEAD /content/item-bank-bundle
GET /content/scale-protocol
GET /content/week1-reply-bank
HEAD /content/week1-reply-bank
GET /content/week1-script
HEAD /content/week1-script
GET /content/week1-warmup
POST /device/pair
GET /exports/{batch_id}
GET /exports/{batch_id}/{unknown_path:path}
GET /governance/withdrawn-audio
POST /items/{item_event_id}/turns
POST /judge/build-input
POST /judge/classify
GET /live/console-state
PUT /live/state
GET /patients
POST /patients
GET /patients/{patient_id}
PATCH /patients/{patient_id}
GET /patients/{patient_id}/assessment-events
POST /patients/{patient_id}/assessment-events
PATCH /patients/{patient_id}/cloud-processing
GET /patients/{patient_id}/questionnaire-records
POST /patients/{patient_id}/questionnaire-records
GET /patients/{patient_id}/questionnaire-review.csv
GET /patients/{patient_id}/scales
POST /patients/{patient_id}/scales
GET /patients/{patient_id}/sessions
GET /patients/{patient_id}/withdrawal
POST /patients/{patient_id}/withdrawal
GET /quality/ai-metrics
POST /questionnaire-records/{record_id}/ai-draft
POST /questionnaire-records/{record_id}/lock
PUT /questionnaire-records/{record_id}/values
GET /questionnaires/definitions
GET /research/v1/dictionary
GET /research/v1/meta
GET /research/v1/{dataset_key}
POST /score/double
POST /score/multi
POST /score/single
POST /sessions
GET /sessions/{session_id}
POST /sessions/{session_id}/abnormal
POST /sessions/{session_id}/abort
GET /sessions/{session_id}/ai-usage
GET /sessions/{session_id}/attempts
POST /sessions/{session_id}/attempts/process
GET /sessions/{session_id}/audio-receipts
POST /sessions/{session_id}/autopilot/activate
POST /sessions/{session_id}/autopilot/adjudicate
POST /sessions/{session_id}/autopilot/commands/{command_key}/acks
POST /sessions/{session_id}/autopilot/commands/{command_key}/barge-in-authorization
POST /sessions/{session_id}/autopilot/commands/{command_key}/drain-ack
POST /sessions/{session_id}/autopilot/commands/{command_key}/recording-authorization
POST /sessions/{session_id}/autopilot/commands/{command_key}/tts
GET /sessions/{session_id}/autopilot/drain-target
POST /sessions/{session_id}/autopilot/resume
POST /sessions/{session_id}/autopilot/start
POST /sessions/{session_id}/autopilot/takeover
GET /sessions/{session_id}/closeout
PUT /sessions/{session_id}/closeout
POST /sessions/{session_id}/complete
POST /sessions/{session_id}/export
POST /sessions/{session_id}/finish-intervention
POST /sessions/{session_id}/interaction-presentations
POST /sessions/{session_id}/interactions
POST /sessions/{session_id}/items
GET /sessions/{session_id}/journal
GET /sessions/{session_id}/outcome-summary
GET /sessions/{session_id}/patient-asset/current
HEAD /sessions/{session_id}/patient-asset/current
POST /sessions/{session_id}/patient-pause
GET /sessions/{session_id}/patient-plan
GET /sessions/{session_id}/patient-presentation
POST /sessions/{session_id}/pause
GET /sessions/{session_id}/plan
GET /sessions/{session_id}/rapport/playback
PUT /sessions/{session_id}/rapport/playback
POST /sessions/{session_id}/rapport/replies
POST /sessions/{session_id}/rapport/utterances/{utterance_id}/tts
POST /sessions/{session_id}/recording-authorization
POST /sessions/{session_id}/resume
PUT /sessions/{session_id}/runtime/cursor
GET /sessions/{session_id}/scores
POST /sessions/{session_id}/technical-pause
POST /sessions/{session_id}/warmup
GET /tts/speak
POST /tts/speak
POST /turns/{turn_id}/ai-judge
PATCH /turns/{turn_id}/confirm
PATCH /turns/{turn_id}/lock
GET /visit-plans
POST /visit-plans
GET /visit-plans/today
POST /visit-plans/{plan_id}/approve
POST /visit-plans/{plan_id}/cancel
POST /visit-plans/{plan_id}/start
"""

# SPA 外壳(web/src/App.tsx 的几条客户端路由)与构建产物;应用对它们只回 index.html 或静态文件。
PAGE_ROUTES = ("/patient", "/console", "/acceptance", "/device-check")
PAGE_FILES = ("/", "/index.html", "/build-id.txt", "/robots.txt")
PAGE_PREFIXES = ("/assets/", "/favicon")

# uvicorn 访问日志:`INFO:     203.0.113.9:0 - "GET /health HTTP/1.1" 200 OK`。路径已被 uvicorn
# 百分号编码过,查询串原样跟在 ? 后面。
ACCESS = re.compile(
    r'^(?:INFO: +)?\S* - "(?P<method>\S+) (?P<target>\S+) HTTP/[0-9.]+" (?P<status>[0-9]{3})(?: .*)?$')
# 应用没有 WebSocket 路由,带 Upgrade 头的扫描只会被拒;uvicorn 记成另一种格式。
WEBSOCKET = re.compile(r'^(?:INFO: +)?\S* - "WebSocket \S+" \S+$')
# journald 把超过 LineMax(默认 48K)的一行硬拆成几条记录:前半截只剩方法和截断的目标,
# 后半截没有 ` - "`,不像访问行。扫描器发一条超长 URL,不该因此挡门 10 分钟。
LINE_MAX = 48 * 1024
SPLIT_HEAD = re.compile(r'^(?:INFO: +)?\S* - "(?P<method>\S+) (?P<target>\S+)$')
# 像访问日志却不符合上面格式的(截断、带颜色码、uvicorn 换了格式):宁可按不空闲算。
LOOKS_LIKE_ACCESS = re.compile(r' - ".*HTTP/|"(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) ')


def _template(template: str) -> re.Pattern[str]:
    # 与 Starlette 同一套:{name} 是一段,{name:path} 可跨段。
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile("".join(
        (".*" if part.endswith(":path}") else "[^/]+") if part.startswith("{") else re.escape(part)
        for part in parts))


def _table(block: str) -> list[tuple[str, str, re.Pattern[str]]]:
    rows = []
    for line in block.strip().splitlines():
        method, template = line.split()
        rows.append((method, template, _template(template)))
    return rows


POLLING_ROUTES = _table(POLLING)
REAL_ROUTES = _table(REAL)


def classify(method: str, target: str, status: int) -> str:
    path = target.split("?", 1)[0]
    if any(method == m and pattern.fullmatch(path) for m, _, pattern in POLLING_ROUTES):
        return "polling"
    if any(method == m and pattern.fullmatch(path) for m, _, pattern in REAL_ROUTES):
        return "unauthenticated" if status == 401 else "real"
    if method in ("GET", "HEAD") and (
            path in PAGE_FILES or path.startswith(PAGE_PREFIXES)
            or any(path == page or path.startswith(page + "/") for page in PAGE_ROUTES)):
        return "page"
    return "noise"


def tally(lines: list[str]) -> tuple[dict[str, int], int]:
    counts = dict.fromkeys(("real", "polling", "page", "unauthenticated", "noise"), 0)
    anomalies = 0
    for line in lines:
        matched = ACCESS.match(line)
        head = SPLIT_HEAD.match(line) if len(line.encode("utf-8")) >= LINE_MAX else None
        if matched:
            counts[classify(matched["method"], matched["target"], int(matched["status"]))] += 1
        elif head:
            # 状态码拆到了后半截,认不出 401:对得上真路由的照样算在用。
            counts[classify(head["method"], head["target"], 0)] += 1
        elif WEBSOCKET.match(line):
            counts["noise"] += 1
        elif LOOKS_LIKE_ACCESS.search(line):
            anomalies += 1
    return counts, anomalies


def main(argv: list[str] | None = None, *, run=subprocess.run, geteuid=os.geteuid) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--stdin", action="store_true",
                        help="从标准输入读 journalctl -o cat 的输出(测试用),不跑 journalctl")
    args = parser.parse_args(argv)
    if args.minutes <= 0:
        parser.error("--minutes 要是正整数")
    window = f"近 {args.minutes} 分钟"
    if args.stdin:
        raw = sys.stdin.buffer.read()
    else:
        # 非 root 读 journal 只看得到自己的那份,nmu 的一行都没有,看上去就是空闲。
        if geteuid() != 0:
            print("不是 root 跑的,读不全 nmu 的 journal,按不空闲算")
            return 2
        try:
            done = run(["journalctl", "-u", "nmu", "--since", f"-{args.minutes} min",
                        "--no-pager", "-o", "cat"], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"{window}的 journal 读不了({error.__class__.__name__}),按不空闲算")
            return 2
        if done.returncode != 0:
            print(f"{window}的 journal 读不了(journalctl 退出码 {done.returncode}),按不空闲算")
            return 2
        raw = done.stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        print(f"{window}的日志里有不是 UTF-8 的字节,按不空闲算")
        return 2
    counts, anomalies = tally(text.splitlines())
    summary = (f"{window}真实使用 {counts['real']} 条(不算:轮询 {counts['polling']}、"
               f"页面与静态 {counts['page']}、未登录被拒 {counts['unauthenticated']}、"
               f"对不上真路由的噪声 {counts['noise']})")
    if anomalies:
        print(f"{summary};另有 {anomalies} 行像访问日志却解析不了,按不空闲算")
        return 2
    if counts["real"]:
        print(f"{summary},有人在用")
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
