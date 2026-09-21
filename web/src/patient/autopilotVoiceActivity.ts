/**
 * 「说完了没」的纯判定:把一串 RMS 帧(每 50–100 ms 一帧)变成
 * idle → speaking → trailing_silence → stopped 的状态机。
 *
 * 养老院 2026-09-16/17 实测(149 段录音):老人 2–3 s 就说完,录音却中位 7.7 s、
 * p90 15.7 s——117 段等工作人员按「我说好了」,32 段撞 14 s 作答窗口。协议冻结
 * 说明写的是「作答窗口(v1):无 VAD…真机应加 VAD」。这个模块就是那个 VAD。
 *
 * 只做判定,不碰任何浏览器对象:不 import、不读 globals、不起定时器。时间由调用
 * 方随每帧带进来(单调毫秒),所以帧率变了,「说了 250 ms 才算开口」「静了 3 s 才算
 * 说完」这两个秒数不变。
 *
 * 阈值是自适应的:噪声底取头 400 ms 的低分位,之后由非语音帧慢慢跟踪;开口阈值
 * 与收口阈值分开(迟滞),吵一点的房间阈值自然抬高。纯静音永远不会判「说完」——
 * 没开过口就没有「说完」,那两条路(按钮、作答窗口到点)仍是唯一的收麦方式。
 */

/** 连续高于开口阈值这么久才算「开口」;短于此的敲击、咳嗽不算话。 */
export const VAD_SPEECH_ONSET_MS = 250;

/** 开口之后连续低于收口阈值这么久判「说完」。 */
export const VAD_TRAILING_SILENCE_MS = 3_000;

/** 头这段时间只标定噪声底,不判语音。 */
export const VAD_CALIBRATION_MS = 400;

/**
 * 开口阈值的绝对下限(时域采样 RMS,满刻度 1.0;0.01 ≈ −40 dBFS)。
 * 极安静的房间里噪声底趋近 0,不设下限的话 floor×3 也趋近 0,呼吸声就会算开口。
 */
export const VAD_ABSOLUTE_MIN_RMS = 0.01;

/** 开口阈值 = max(噪声底 × 3, 绝对下限)。 */
export const VAD_ONSET_FLOOR_RATIO = 3;

/** 收口阈值 = max(噪声底 × 2, 绝对下限 × 0.7)。比开口低,说话的尾音不会被当成静默。 */
export const VAD_RELEASE_FLOOR_RATIO = 2;
export const VAD_RELEASE_MIN_RATIO = 0.7;

/** 标定期取这一分位当噪声底:头 400 ms 里混进半句话也不会把底抬到语音级。 */
export const VAD_CALIBRATION_QUANTILE = 0.25;

/**
 * 标定之后噪声底按非语音帧做非对称 EMA:往下跟得快(老人一开录就在说话时,
 * 标定出来的底是语音级,第一次停顿就要把底放下来),往上爬得慢(电视声渐起)。
 */
export const VAD_NOISE_FLOOR_FALL_TAU_MS = 300;
export const VAD_NOISE_FLOOR_RISE_TAU_MS = 3_000;

/**
 * 一帧最多记这么多毫秒。定时器被卡住几秒之后补来的一帧,中间发生过什么没人
 * 知道,不能把那几秒整段记成静默。
 */
export const VAD_MAX_FRAME_GAP_MS = 500;

export type VoiceActivityState = "idle" | "speaking" | "trailing_silence" | "stopped";

export interface VoiceActivityDetector {
  readonly state: VoiceActivityState;
  /** 标定完成之前为 null。 */
  readonly noiseFloor: number | null;
  readonly onsetThreshold: number | null;
  readonly releaseThreshold: number | null;
  /**
   * 喂一帧。`atMs` 是这帧采样时刻的单调毫秒读数;帧代表上一帧到这一帧之间那段
   * 时间。返回这帧之后的状态;`stopped` 一旦出现就定型,再喂什么都不变。
   */
  push(rms: number, atMs: number): VoiceActivityState;
}

function lowQuantile(values: number[], quantile: number): number {
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(sorted.length - 1, Math.floor(sorted.length * quantile));
  return sorted[index];
}

export function createVoiceActivityDetector(): VoiceActivityDetector {
  let state: VoiceActivityState = "idle";
  let firstAtMs: number | null = null;
  let lastAtMs: number | null = null;
  const calibration: number[] = [];
  let floor: number | null = null;
  let loudMs = 0;
  let quietMs = 0;

  const onsetThreshold = (): number | null => floor === null
    ? null : Math.max(floor * VAD_ONSET_FLOOR_RATIO, VAD_ABSOLUTE_MIN_RMS);
  const releaseThreshold = (): number | null => floor === null
    ? null : Math.max(floor * VAD_RELEASE_FLOOR_RATIO, VAD_ABSOLUTE_MIN_RMS * VAD_RELEASE_MIN_RATIO);

  const trackFloor = (rms: number, deltaMs: number): void => {
    if (floor === null || deltaMs <= 0) return;
    const tau = rms < floor ? VAD_NOISE_FLOOR_FALL_TAU_MS : VAD_NOISE_FLOOR_RISE_TAU_MS;
    const alpha = Math.min(1, deltaMs / tau);
    floor += alpha * (rms - floor);
  };

  return {
    get state() { return state; },
    get noiseFloor() { return floor; },
    get onsetThreshold() { return onsetThreshold(); },
    get releaseThreshold() { return releaseThreshold(); },
    push(rms: number, atMs: number): VoiceActivityState {
      if (state === "stopped") return state;
      // 坏读数(NaN/负数)整帧丢掉:既不当静默也不当语音,时间也不推进。
      if (!Number.isFinite(rms) || rms < 0 || !Number.isFinite(atMs)) return state;
      const deltaMs = lastAtMs === null
        ? 0 : Math.min(Math.max(0, atMs - lastAtMs), VAD_MAX_FRAME_GAP_MS);
      lastAtMs = atMs;
      if (firstAtMs === null) firstAtMs = atMs;

      if (floor === null) {
        calibration.push(rms);
        if (atMs - firstAtMs >= VAD_CALIBRATION_MS) {
          floor = lowQuantile(calibration, VAD_CALIBRATION_QUANTILE);
        }
        return state;
      }

      const onset = onsetThreshold() as number;
      const release = releaseThreshold() as number;
      if (state === "idle") {
        if (rms >= onset) {
          loudMs += deltaMs;
          if (loudMs >= VAD_SPEECH_ONSET_MS) {
            state = "speaking";
            quietMs = 0;
          }
        } else {
          loudMs = 0;
          trackFloor(rms, deltaMs);
        }
        return state;
      }

      // speaking / trailing_silence:低于收口阈值累计静默,高于就回到 speaking。
      if (rms < release) {
        quietMs += deltaMs;
        trackFloor(rms, deltaMs);
        state = quietMs >= VAD_TRAILING_SILENCE_MS ? "stopped" : "trailing_silence";
      } else {
        quietMs = 0;
        state = "speaking";
      }
      return state;
    },
  };
}
