# A15：GitHub main 必需检查验收

2026-09-06 09:45 UTC 前已完成设置及独立回读。对象为 `EricXu-0805/nmu-ad-language-training` 的 `main`。操作前 API 返回 `404 Branch not protected`，仓库管理员权限已确认。

配置源：`../../.github/branch-protection.json`。读取基线 `3dbf0538f906fefebdf91ff8772aab7556f2fc66` 的真实 check runs 后，按实际名称绑定 `app_id=15368`（GitHub Actions）：

- `backend (3.12)`
- `backend (3.14)`
- `frontend`
- `supply-chain`
- `image`

应用后另一次 GET 及结构断言确认：五项完全匹配，`strict=true`，`enforce_admins.enabled=true`，强推与删除均为 false。PR 必需，但审批人数为 0，避免单人项目被不存在的第二审批者锁死；已打开讨论解决要求。

原始写入回包与独立回读保存在父项目 `审核报告_20260906/evidence/branch-protection-applied.json`、`branch-protection-readback.json`，不含密钥。未进行会故意破坏 main 的试推。

此记录证明托管规则在回读时已生效，不证明当前修复候选的 CI 已通过，不证明已经部署，也不能保证后续管理员没有修改规则。

接口依据：[GitHub 官方分支保护文档](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection)。GitHub 的 `checks` 与旧 `contexts` 使用互斥声明，本配置只使用绑定来源的 `checks`。
