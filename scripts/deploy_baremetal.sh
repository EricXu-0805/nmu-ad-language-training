#!/usr/bin/env bash
# Historical command name retained only to fail closed for old runbooks.
#
# The former implementation contained ssh, rsync, remote service stop/start,
# migration and rollback commands.  Those executable lines have been removed,
# so starting this file at a later line cannot bypass the stop.
set -euo pipefail
umask 077

echo "已停用：裸机 rsync 发布/回滚路径已删除。" >&2
echo "正式候选只能按 DEPLOY.md 第 3、8、9 章使用不可变镜像、候选数据卷和恢复证据。" >&2
exit 64
