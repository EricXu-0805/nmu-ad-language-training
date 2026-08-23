import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ApiError } from "../apiResponse.ts";
import {
  buildProfilePatch,
  consentTypeLockedReason,
  draftFromPatient,
  EDITABLE_PROFILE_FIELDS,
  patchIsEmpty,
  renameIssue,
  saveErrorText,
  type ProfileDraft,
} from "./patientEdit.ts";
import type { Patient } from "../types.ts";

const PATIENT: Patient = {
  patient_id: "P-EDIT",
  is_simulation_subject: false,
  governance_revision: 0,
  dementia_severity: "轻度",
  mandarin_eligible: true,
  consent_status: "已同意",
  consent_type: "本人同意",
  consent_person: "本人",
  recording_allowed: true,
};

test("补丁只带真正改动的字段;没动的键不进 PATCH", () => {
  const original = draftFromPatient(PATIENT);
  const edited: ProfileDraft = { ...original, dementia_severity: "中度", recording_allowed: null };
  const patch = buildProfilePatch(original, edited, "", "P-EDIT");
  assert.deepEqual(patch, { dementia_severity: "中度", recording_allowed: null });
  assert.equal(patchIsEmpty(buildProfilePatch(original, { ...original }, "", "P-EDIT")), true);
});

test("治理字段永远不在可编辑清单里", () => {
  for (const forbidden of [
    "withdrawal_status", "governance_revision", "is_simulation_subject",
    "cloud_processing_allowed", "cloud_processing_provider_id", "patient_id",
  ]) {
    assert.ok(!(EDITABLE_PROFILE_FIELDS as readonly string[]).includes(forbidden), forbidden);
  }
});

test("编号更正:等于原编号或留空不发;不同编号才进补丁", () => {
  const original = draftFromPatient(PATIENT);
  assert.equal(buildProfilePatch(original, { ...original }, "  P-EDIT  ", "P-EDIT").new_patient_id, undefined);
  assert.equal(buildProfilePatch(original, { ...original }, "", "P-EDIT").new_patient_id, undefined);
  assert.equal(buildProfilePatch(original, { ...original }, "NMU-001", "P-EDIT").new_patient_id, "NMU-001");
});

test("改名门禁在前端就说人话:格式错/已有场次都当场可见", () => {
  assert.equal(renameIssue("", 0), null);
  assert.equal(renameIssue("NMU-001", 0), null);
  assert.match(renameIssue("测试1", 0) ?? "", /字母、数字/);
  assert.match(renameIssue("NMU-001", 3) ?? "", /已有训练数据/);
});

test("保存错误逐类翻译成人话,409/422 透传服务器可读原因", () => {
  assert.equal(saveErrorText(new ApiError(409, "研究编号 NMU-001 已被使用，请换一个编号")),
    "研究编号 NMU-001 已被使用，请换一个编号");
  assert.match(saveErrorText(new ApiError(403, "x")), /权限/);
  assert.match(saveErrorText(new ApiError(401, "x")), /重新登录/);
});

test("知情同意方式:空值可补录;非空值锁定并给可见原因(与后端 409 同则)", () => {
  const emptyConsent = draftFromPatient({ ...PATIENT, consent_type: undefined });
  assert.equal(consentTypeLockedReason(emptyConsent), null);
  const locked = consentTypeLockedReason(draftFromPatient(PATIENT));
  assert.match(locked ?? "", /本人同意/);
  assert.match(locked ?? "", /不能在这里改写/);
  // 抽屉里下拉按同一规则禁用,原因走可见 hint 而不是 title。
  const drawer = readFileSync(new URL("./PatientEditDrawer.tsx", import.meta.url), "utf8");
  assert.match(drawer, /disabled=\{consentTypeLockedReason\(original \?\? draft\) !== null\}/);
  assert.match(drawer, /hint=\{consentTypeLockedReason\(original \?\? draft\)/);
});

test("登记表行内有编辑入口,编辑抽屉挂在列表层", () => {
  const screen = readFileSync(new URL("./SubjectRegistryScreen.tsx", import.meta.url), "utf8");
  assert.match(screen, /编辑档案/);
  assert.match(screen, /<PatientEditDrawer patientId=\{editFor\.patient_id\}/);
  // 已撤回行没有编辑入口:撤回档案由治理流程管理。
  assert.match(screen, /\{!r\.withdrawal_status && canManagePlans && \(\s*<Button onClick=\{\(\) => setEditFor\(r\)\}>编辑档案<\/Button>/);
  // 配对码列:只在服务器给出值时显示,标题解释一次输入长期有效。
  assert.match(screen, /r\.pairing_code && \(/);
});
