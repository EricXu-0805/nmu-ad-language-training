import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { cloudProcessingChoiceIssue, cloudProcessingDisclosure } from "./cloudProcessingPolicy.ts";
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

  assert.match(source, /const submitted = capturePatientIntakeSubmission\(p\);/);
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
