import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  cloudAuthorizationReadDelayMs,
  cloudProcessingAllowDisabledReason,
  cloudProcessingAuthorizedForAutopilot,
  cloudProcessingChoiceIssue,
  cloudProcessingDisclosure,
  nextCloudAuthorization,
  type CloudAuthorizationRead,
} from "./cloudProcessingPolicy.ts";
import {
  assessDuplicateIntake,
  capturePatientIntakeSubmission,
  duplicateCloudChangeMayBeApplied,
  duplicatePatientSnapshotChanged,
} from "./patientIntakeDuplicate.ts";

test("建档提交快照不受网络等待期间的草稿编辑影响", () => {
  const draft = {
    patient_id: "  P-SLOW-409  ",
    is_simulation_subject: false,
    recording_allowed: true,
    cloud_processing_allowed: false,
    governance_revision: 0,
  };
  const submitted = capturePatientIntakeSubmission(draft);

  draft.patient_id = "P-OTHER";
  draft.recording_allowed = false;
  draft.cloud_processing_allowed = true;

  assert.deepEqual(submitted, {
    patient_id: "P-SLOW-409",
    is_simulation_subject: false,
    recording_allowed: true,
    cloud_processing_allowed: false,
    governance_revision: 0,
  });
  assert.notEqual(submitted, draft);
});

test("409 核对固定使用 submitted 快照，busy fieldset 不包住重复档案确认区", () => {
  const source = readFileSync(new URL("./PatientIntakeScreen.tsx", import.meta.url), "utf8");

  // The actual async submit/409 behavior (including validated numeric fields)
  // is exercised in frontendSafetyRegression.test.mjs; do not bind this check
  // to the draft expression used to construct the immutable submission.
  assert.match(source, /await resolveExisting\(submitted, then\);/);
  assert.doesNotMatch(source, /resolveExisting\(\{\s*\.\.\.p/);
  assert.match(source, /<fieldset[\s\S]*disabled=\{busy\}[\s\S]*aria-busy=\{busy\}/);

  const fieldsetEnd = source.indexOf("</fieldset>");
  const duplicateReviewStart = source.indexOf("{duplicateReview && (");
  assert.ok(fieldsetEnd >= 0 && duplicateReviewStart > fieldsetEnd,
    "重复档案确认区必须位于 busy fieldset 之外");
  assert.match(source, /aria-labelledby="duplicate-review-title" aria-busy=\{busy\}/);
  assert.match(source, /<Button disabled=\{busy\}[\s\S]*关闭并修改本次表单/);
  assert.match(source, /<Button disabled=\{busy\}[\s\S]*沿用既有档案/);
  assert.match(source, /<Button variant="danger" disabled=\{busy\}/);
});

test("允许云处理时服务器 policy 缺失必须 fail closed", () => {
  assert.match(cloudProcessingChoiceIssue(true, null) ?? "", /不能记录允许/);
  assert.equal(cloudProcessingChoiceIssue(false, null), null);
  assert.equal(cloudProcessingChoiceIssue(null, null), null);
});

test("未接入时的文案是人话:不出现英文 policy,并给出可执行的下一步", () => {
  const unconfigured = { configured: false, provider_id: null, notice_version: null, data_categories: [] };
  for (const text of [
    cloudProcessingDisclosure(null),
    cloudProcessingDisclosure(unconfigured),
    cloudProcessingChoiceIssue(true, null) ?? "",
    cloudProcessingAllowDisabledReason(unconfigured, null) ?? "",
  ]) {
    assert.doesNotMatch(text, /policy/i, text);
  }
  assert.match(cloudProcessingAllowDisabledReason(unconfigured, null) ?? "", /暂不选择/);
});

test("『是』按钮禁用原因:未接入/读取中/读取失败给原因,配置齐全为 null", () => {
  const configured = {
    configured: true,
    provider_id: "aliyun-dashscope",
    notice_version: "notice-2026-01",
    data_categories: [],
  };
  assert.equal(cloudProcessingAllowDisabledReason(configured, null), null);
  assert.match(cloudProcessingAllowDisabledReason(null, null) ?? "", /正在读取/);
  assert.match(cloudProcessingAllowDisabledReason(null, "网络错误") ?? "", /无法授权/);
  assert.match(
    cloudProcessingAllowDisabledReason(
      { configured: false, provider_id: null, notice_version: null, data_categories: [] },
      null,
    ) ?? "", /无法授权/);
});

test("建档屏云区块:『是』按预检禁用、第三态叫暂不选择、说明随配置切换", () => {
  const source = readFileSync(new URL("./PatientIntakeScreen.tsx", import.meta.url), "utf8");
  assert.match(source, /nullLabel="暂不选择"/);
  assert.match(source, /yesDisabled=\{cloudProcessingAllowDisabledReason\(cloudPolicy, cloudPolicyError\) !== null\}/);
  assert.match(source, /yesDisabledReason=\{cloudProcessingAllowDisabledReason\(cloudPolicy, cloudPolicyError\)\}/);
  assert.match(source, /云端 AI 服务尚未接入/);
  // 未接入不再顶着红色警示框吓人;配置齐全才用 warn 提示要外发数据。
  assert.match(source, /tone=\{cloudPolicy\?\.configured \? "warn" : "info"\}/);
});

test("文案明确声纹、回答文本、provider 与告知版本", () => {
  const policy = {
    configured: true,
    provider_id: "aliyun-dashscope",
    notice_version: "notice-2026-01",
    data_categories: [],
  };
  assert.equal(cloudProcessingChoiceIssue(true, policy), null);
  const text = cloudProcessingDisclosure(policy);
  for (const expected of ["声纹", "回答转写文本", "aliyun-dashscope", "notice-2026-01"]) {
    assert.match(text, new RegExp(expected));
  }
});

test("重复编号从既有拒绝或撤销改为允许时绝不具备自动写入资格", () => {
  const submitted = {
    patient_id: "P-REVOKED",
    is_simulation_subject: false,
    cloud_processing_allowed: true,
  };
  for (const revokedAt of [null, "2026-07-18T08:00:00Z"]) {
    const existing = {
      patient_id: "P-REVOKED",
      is_simulation_subject: false,
      cloud_processing_allowed: false,
      cloud_processing_revoked_at: revokedAt,
    };
    const assessment = assessDuplicateIntake(submitted, existing);

    assert.equal(assessment.cloudChangeRequested, true);
    assert.equal(assessment.cloudChangeDirection, "grant");
    assert.equal(assessment.existingCloudRevocationRecorded, revokedAt != null);
    assert.equal(assessment.canRequestCloudChange, true);
    assert.equal(duplicateCloudChangeMayBeApplied(assessment, {
      duplicateReviewed: false,
      dedicatedAuthorizationConfirmed: false,
    }), false);
    assert.equal(duplicateCloudChangeMayBeApplied(assessment, {
      duplicateReviewed: true,
      dedicatedAuthorizationConfirmed: false,
    }), false);
    assert.equal(duplicateCloudChangeMayBeApplied(assessment, {
      duplicateReviewed: true,
      dedicatedAuthorizationConfirmed: true,
    }), true);
  }
});

test("重复编号存在其他档案冲突时即使二次确认也不能修改云授权", () => {
  const assessment = assessDuplicateIntake({
    patient_id: "P-CONFLICT",
    is_simulation_subject: false,
    recording_allowed: true,
    cloud_processing_allowed: true,
  }, {
    patient_id: "P-CONFLICT",
    is_simulation_subject: false,
    recording_allowed: false,
    cloud_processing_allowed: false,
  });

  assert.deepEqual(assessment.blockingConflictLabels, ["录音授权"]);
  assert.equal(assessment.canReuseWithoutChanges, false);
  assert.equal(assessment.canRequestCloudChange, false);
  assert.equal(duplicateCloudChangeMayBeApplied(assessment, {
    duplicateReviewed: true,
    dedicatedAuthorizationConfirmed: true,
  }), false);
});

test("二次确认前授权或撤回事实发生并发变化必须重新核对", () => {
  const reviewed = {
    patient_id: "P-RACE",
    is_simulation_subject: false,
    cloud_processing_allowed: false,
    cloud_processing_revoked_at: "2026-07-18T08:00:00Z",
  };
  assert.equal(duplicatePatientSnapshotChanged(reviewed, { ...reviewed }), false);
  assert.equal(duplicatePatientSnapshotChanged(reviewed, {
    ...reviewed,
    withdrawal_status: "已撤回",
  }), true);
  assert.equal(duplicatePatientSnapshotChanged(reviewed, {
    ...reviewed,
    cloud_processing_allowed: true,
    cloud_processing_revoked_at: null,
  }), true);
});

test("自动带练的云处理授权判定与服务端门禁同一条规则(autopilot_cloud_processing_required)", () => {
  const policy = {
    configured: true,
    provider_id: "aliyun-dashscope",
    notice_version: "notice-2026-01",
    data_categories: [],
  };
  const granted = {
    cloud_processing_allowed: true,
    cloud_processing_provider_id: "aliyun-dashscope",
    cloud_processing_notice_version: "notice-2026-01",
    cloud_processing_consented_at: "2026-11-20T08:00:00Z",
    cloud_processing_revoked_at: null,
  };
  assert.equal(cloudProcessingAuthorizedForAutopilot(granted, policy), true);
  // 2026-11-27 上午 8 次 409 的那位受试者:cloud_processing_allowed 为 null(从没选过)。
  assert.equal(cloudProcessingAuthorizedForAutopilot({ cloud_processing_allowed: null }, policy), false);
  assert.equal(cloudProcessingAuthorizedForAutopilot({}, policy), false);
  for (const spoiled of [
    { cloud_processing_allowed: false },
    { cloud_processing_provider_id: "other-provider" },
    { cloud_processing_notice_version: "notice-2025-12" },
    { cloud_processing_consented_at: null },
    { cloud_processing_revoked_at: "2026-11-21T08:00:00Z" },
  ]) {
    assert.equal(cloudProcessingAuthorizedForAutopilot({ ...granted, ...spoiled }, policy), false,
      JSON.stringify(spoiled));
  }
  // 部署没接入云端服务时谁都不算已授权。
  assert.equal(cloudProcessingAuthorizedForAutopilot(granted, {
    configured: false, provider_id: null, notice_version: null, data_categories: [],
  }), false);
});

const READ_FAILED: CloudAuthorizationRead = { ok: false, error: "Bad Gateway" };

test("云授权读失败不推翻已读到的结论,从没读到过就保持待核对;只有成功读数才改结论", () => {
  assert.equal(nextCloudAuthorization(true, READ_FAILED), true);
  assert.equal(nextCloudAuthorization(false, READ_FAILED), false);
  assert.equal(nextCloudAuthorization(null, READ_FAILED), null);
  assert.equal(nextCloudAuthorization(null, { ok: true, authorized: true }), true);
  assert.equal(nextCloudAuthorization(null, { ok: true, authorized: false }), false);
  // 授权在别处补完或被撤回,下一次成功读数照实翻转。
  assert.equal(nextCloudAuthorization(false, { ok: true, authorized: true }), true);
  assert.equal(nextCloudAuthorization(true, { ok: true, authorized: false }), false);
});

test("云授权重读间隔:已有结论 30 s 一读(读失败也一样);从没读到过按 3 s 起翻倍重试,封顶 30 s", () => {
  for (const known of [true, false]) {
    for (const failures of [0, 1, 2, 5, 20]) {
      assert.equal(cloudAuthorizationReadDelayMs(known, failures), 30_000, `${known}/${failures}`);
    }
  }
  assert.deepEqual([1, 2, 3, 4, 5, 6, 20].map((failures) => cloudAuthorizationReadDelayMs(null, failures)),
    [3_000, 6_000, 12_000, 24_000, 30_000, 30_000, 30_000]);
});

// 与组件读循环同一个推进顺序:读完 → 结论按纯函数推进 → 连续失败计数 → 按间隔排下一读。
function simulateCloudReads(results: readonly (boolean | "fail")[]) {
  let known: boolean | null = null;
  let failures = 0;
  let at = 0;
  const reads: { at: number; known: boolean | null }[] = [];
  for (const result of results) {
    const read: CloudAuthorizationRead = result === "fail" ? READ_FAILED : { ok: true, authorized: result };
    known = nextCloudAuthorization(known, read);
    failures = read.ok ? 0 : failures + 1;
    reads.push({ at, known });
    at += cloudAuthorizationReadDelayMs(known, failures);
  }
  return reads;
}

// 复核 F2:首读撞上一次 502 时,旧代码要等满 30 s 才重读,已授权受试者的启动被白白挡 30 s。
test("云授权首读失败一次:3 s 后重读,已授权受试者最多被挡 3 s 而不是 30 s", () => {
  assert.deepEqual(simulateCloudReads(["fail", true]), [
    { at: 0, known: null },
    { at: 3_000, known: true },
  ]);
  assert.deepEqual(simulateCloudReads(["fail", "fail", "fail", true]).map((read) => read.at),
    [0, 3_000, 9_000, 21_000]);
  // 已有结论之后的读失败不改结论,也不加快重读。
  assert.deepEqual(simulateCloudReads([true, "fail", "fail", false]), [
    { at: 0, known: true },
    { at: 30_000, known: true },
    { at: 60_000, known: true },
    { at: 90_000, known: false },
  ]);
});
