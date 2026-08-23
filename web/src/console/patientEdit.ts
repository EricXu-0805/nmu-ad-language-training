// 档案编辑抽屉的纯逻辑层:算差量补丁、判改名可行性、把服务器错误翻成人话。
import { ApiError } from "../apiResponse.ts";
import type { Patient, PatientProfilePatch } from "../types";
import { PATIENT_ID_VALID_PATTERN } from "./registryPresentation.ts";

// 与后端 PatientProfileUpdate 一致的可编辑面;治理字段(撤回/云授权/模拟身份/
// 治理版本)不在此清单,各走专属流程。
export const EDITABLE_PROFILE_FIELDS = [
  "dementia_severity",
  "mandarin_eligible",
  "consent_status",
  "consent_type",
  "consent_person",
  "proxy_consent",
  "assent_obtained",
  "recording_allowed",
  "secondary_use_allowed",
] as const;

export type EditableProfileField = (typeof EDITABLE_PROFILE_FIELDS)[number];

export type ProfileDraft = Pick<Patient, EditableProfileField>;

export function draftFromPatient(patient: Patient): ProfileDraft {
  const draft = {} as Record<EditableProfileField, unknown>;
  for (const field of EDITABLE_PROFILE_FIELDS) {
    draft[field] = patient[field] ?? null;
  }
  return draft as ProfileDraft;
}

// 只发真正改动的字段:没动的键不进 PATCH,服务器审计里的字段名清单才可信。
export function buildProfilePatch(
  original: ProfileDraft,
  edited: ProfileDraft,
  newPatientId: string | null,
  currentPatientId: string,
): PatientProfilePatch {
  const patch: PatientProfilePatch = {};
  for (const field of EDITABLE_PROFILE_FIELDS) {
    const before = original[field] ?? null;
    const after = edited[field] ?? null;
    if (before !== after) {
      (patch as Record<string, unknown>)[field] = after;
    }
  }
  const trimmedId = (newPatientId ?? "").trim();
  if (trimmedId && trimmedId !== currentPatientId) {
    patch.new_patient_id = trimmedId;
  }
  return patch;
}

export function patchIsEmpty(patch: PatientProfilePatch): boolean {
  return Object.keys(patch).length === 0;
}

// 知情同意方式:补录空值允许(纠正遗漏),改写非空值拒绝(那是治理动作)。
// 与后端 PATCH /patients 的同名规则一致;返回非 null 时下拉应禁用并显示原因。
export function consentTypeLockedReason(original: ProfileDraft): string | null {
  if (!original.consent_type) return null;
  return `已登记为「${original.consent_type}」——登记过的同意方式不能在这里改写，如确需更正请联系研究负责人`;
}

export function renameIssue(
  newPatientId: string,
  sessionCount: number,
): string | null {
  const trimmed = newPatientId.trim();
  if (!trimmed) return null;
  if (!PATIENT_ID_VALID_PATTERN.test(trimmed)) {
    return "编号只能用字母、数字、点、横线，例如 NMU-001——不要用中文或姓名";
  }
  if (sessionCount > 0) {
    return "已有训练数据，编号不能再改；如确属登记错误，请新建档案";
  }
  return null;
}

export function saveErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return error.detail;
    if (error.status === 422) return error.detail;
    if (error.status === 401) return "登录已过期，请重新登录后再保存";
    if (error.status === 403) return "当前账号没有编辑档案的权限";
    return error.detail;
  }
  return error instanceof Error ? error.message : String(error);
}
