// ★第1周画像:v1 仅老人端本地存储(localStorage),【绝不 POST 到后端】——最强隔离。
// 判分链在类型层根本触达不到画像(LockPanel/TurnScoringVM 只由 plan.display + 判分字段构成)。
// 后端本就无 Week1Profile 写路由,物理上判分侧取不到。
export interface LocalWeek1Profile {
  patientId: string;
  preferred_appellation?: string;
  zodiac?: string;
  interests?: string;
  daily_activities?: string;
  familiar_places?: string;
  familiar_people?: string;
  common_objects?: string;
  willing_to_continue?: boolean;
}

const key = (pid: string) => `nmu:profile:${pid}`;

export function loadLocalProfile(patientId: string): LocalWeek1Profile {
  try {
    const raw = localStorage.getItem(key(patientId));
    if (raw) return JSON.parse(raw) as LocalWeek1Profile;
  } catch { /* 忽略损坏 */ }
  return { patientId };
}

export function saveLocalProfile(p: LocalWeek1Profile): void {
  localStorage.setItem(key(p.patientId), JSON.stringify(p));
}
