#!/usr/bin/env python3
"""研究者账号管理 CLI（无自助注册，账号由管理员在服务器上开）。

用法（在 platform/ 目录下）：
  ./.venv/bin/python scripts/manage_users.py create 丁老师 --role admin
  ./.venv/bin/python scripts/manage_users.py list
  ./.venv/bin/python scripts/manage_users.py passwd 丁老师
  ./.venv/bin/python scripts/manage_users.py disable 某离组研究生
  ./.venv/bin/python scripts/manage_users.py enable  某研究生

容器里：docker compose exec app python scripts/manage_users.py create <名>

口令：默认交互式两次输入（不回显、不进 shell 历史/进程表）；自动化用 --password-stdin
从标准输入读一行。DB 由 DATABASE_URL 决定（未设则默认 data/app.db）。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from app import auth, db  # noqa: E402
from app.db import init_db  # noqa: E402
from app.models import AuthSession, ResearchUser  # noqa: E402


def _read_password(args) -> str:
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < 8:
            sys.exit("密码至少 8 位")
        return pw
    while True:
        pw = getpass.getpass("设置密码（≥8 位，不回显）: ")
        if len(pw) < 8:
            print("太短，至少 8 位，重来。"); continue
        if pw != getpass.getpass("再输一次确认: "):
            print("两次不一致，重来。"); continue
        return pw


def cmd_create(s: Session, args) -> None:
    if s.get(ResearchUser, args.username):
        sys.exit(f"账号 {args.username} 已存在（改密码用 passwd）")
    pw = _read_password(args)
    s.add(ResearchUser(
        username=args.username,
        display_id=(args.display_id or args.username),
        password_hash=auth.hash_password(pw),
        role=args.role,
        created_at=datetime.now(),
    ))
    s.commit()
    print(f"✓ 已建账号 {args.username}（角色 {args.role}，审计身份 {args.display_id or args.username}）")
    if not auth.require_auth_env():
        print("⚠️ 提醒：公网部署务必设 REQUIRE_AUTH=1（并配 TLS 反代），否则认证形同虚设。")


def cmd_passwd(s: Session, args) -> None:
    user = s.get(ResearchUser, args.username)
    if not user:
        sys.exit(f"无此账号 {args.username}")
    user.password_hash = auth.hash_password(_read_password(args))
    s.add(user)
    # 改密后吊销该用户所有在用会话，强制重新登录。
    for row in s.exec(select(AuthSession).where(AuthSession.username == args.username)):
        s.delete(row)
    s.commit()
    print(f"✓ 已重置 {args.username} 的密码，并吊销其全部在用会话")


def _set_disabled(s: Session, username: str, disabled: bool) -> None:
    user = s.get(ResearchUser, username)
    if not user:
        sys.exit(f"无此账号 {username}")
    user.disabled = disabled
    s.add(user)
    if disabled:  # 停用即吊销其会话
        for row in s.exec(select(AuthSession).where(AuthSession.username == username)):
            s.delete(row)
    s.commit()
    print(f"✓ {username} 已{'停用（会话已吊销）' if disabled else '启用'}")


def cmd_list(s: Session, _args) -> None:
    users = list(s.exec(select(ResearchUser).order_by(ResearchUser.username)))
    if not users:
        print("（暂无账号。用 create 新建，第一个建议 --role admin）"); return
    print(f"{'用户名':<16}{'审计身份':<16}{'角色':<12}{'状态':<8}{'最近登录'}")
    for u in users:
        print(f"{u.username:<16}{u.display_id:<16}{u.role:<12}"
              f"{'停用' if u.disabled else '启用':<8}"
              f"{u.last_login_at.isoformat(timespec='minutes') if u.last_login_at else '—'}")


def main() -> None:
    p = argparse.ArgumentParser(description="研究者账号管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="新建账号")
    c.add_argument("username")
    c.add_argument("--display-id", default=None, help="落到锁分/量表的审计身份，默认同用户名")
    c.add_argument("--role", choices=["researcher", "admin"], default="researcher")
    c.add_argument("--password-stdin", action="store_true", help="从 stdin 读密码（自动化用）")

    pw = sub.add_parser("passwd", help="重置密码（吊销其会话）")
    pw.add_argument("username")
    pw.add_argument("--password-stdin", action="store_true")

    d = sub.add_parser("disable", help="停用账号（吊销其会话）")
    d.add_argument("username")
    e = sub.add_parser("enable", help="启用账号")
    e.add_argument("username")
    sub.add_parser("list", help="列出账号")

    args = p.parse_args()
    init_db(db.engine)
    with Session(db.engine) as s:
        if args.cmd == "create":
            cmd_create(s, args)
        elif args.cmd == "passwd":
            cmd_passwd(s, args)
        elif args.cmd == "disable":
            _set_disabled(s, args.username, True)
        elif args.cmd == "enable":
            _set_disabled(s, args.username, False)
        elif args.cmd == "list":
            cmd_list(s, args)


if __name__ == "__main__":
    main()
