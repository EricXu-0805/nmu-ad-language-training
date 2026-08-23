import type { EventLine, PhaseType, Session, SessionPlan } from "../types";

/**
 * Immutable frontend pin for the only simulation-only runnable demo profile.
 * Keep this literal aligned with app/autopilot_plan_profiles.py; changing a
 * profile means adding a new immutable entry, never moving this pin in place.
 */
export const WEEK2_SINGLE20_DEMO_PROFILE_VERSION = "week2-single20-demo-v1" as const;
export const WEEK2_SINGLE20_DEMO_PROFILE_DIGEST =
  "655e60c654405526a91dce02e3c06403951f3762a018e65c02ecf29541dfbdec" as const;

const WEEK2_SINGLE20_POSITION_COUNT = 20;

export function demoProfileVersionForVisitPlan(
  isSimulation: boolean | null,
  weekNo: number,
  phaseType: PhaseType,
  eventLine: EventLine,
): typeof WEEK2_SINGLE20_DEMO_PROFILE_VERSION | undefined {
  return isSimulation === true
    && weekNo === 2
    && phaseType === "正式训练"
    && eventLine === "正式训练"
    ? WEEK2_SINGLE20_DEMO_PROFILE_VERSION
    : undefined;
}

export function hasExactWeek2Single20Profile(session: Session): boolean {
  return session.autopilot_profile_version_id === WEEK2_SINGLE20_DEMO_PROFILE_VERSION
    && session.autopilot_profile_definition_digest === WEEK2_SINGLE20_DEMO_PROFILE_DIGEST
    && session.visit_plan_id !== null
    && session.visit_plan_id !== undefined
    && session.is_simulation === true
    && session.data_classification === "simulation"
    && session.week_no === 2
    && session.phase_type === "正式训练"
    && session.event_line === "正式训练";
}

function isExactWeek2Single20Plan(session: Session, plan: SessionPlan): boolean {
  if (plan.item_bank_version_id !== session.item_bank_version_id
      || plan.week_no !== 2
      || plan.event_line !== "正式训练"
      || plan.total_items !== WEEK2_SINGLE20_POSITION_COUNT
      || plan.total_turns !== WEEK2_SINGLE20_POSITION_COUNT
      || plan.items.length !== WEEK2_SINGLE20_POSITION_COUNT) {
    return false;
  }
  return plan.items.every((item) => {
    const turn = item.turns[0];
    return item.task_type === "单要素"
      && item.turns.length === 1
      && turn?.turn_seq === 1
      && turn.response_role === "命名";
  });
}

/**
 * Canonical sessions remain governed only by the server's whole-source flag.
 * The exact frozen demo profile instead proves readiness through its own
 * session-bound 20-position projection, so unrelated canonical gaps cannot
 * disable it. Unknown, half-bound, or drifted profile facts stay fail-closed.
 */
export function operationalAutopilotReadyForSession(
  session: Session,
  plan: SessionPlan,
): boolean {
  const pairedNull = session.autopilot_profile_version_id === null
    && session.autopilot_profile_definition_digest === null;
  if (pairedNull) {
    return plan.autopilot_profile_version_id === null
      && plan.completion_scope === "canonical_full_source"
      && plan.operational_autopilot_ready === true
      && plan.unsupported_position_count === 0;
  }
  return hasExactWeek2Single20Profile(session)
    && plan.autopilot_profile_version_id === WEEK2_SINGLE20_DEMO_PROFILE_VERSION
    && plan.completion_scope === "demo_plan_only"
    && plan.resolved_position_count === WEEK2_SINGLE20_POSITION_COUNT
    && plan.unsupported_position_count === 0
    && plan.operational_autopilot_ready === true
    && isExactWeek2Single20Plan(session, plan);
}
