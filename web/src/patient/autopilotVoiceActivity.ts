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
 *
 * 复核 2026-09-21 抓出的截断路径:一声咳嗽、一个「嗯」、工作人员一句「你说说看」
 * 都够 250 ms,3 s 后就把麦收了,老人的回答根本不在录音里。所以「说完」还要求开口后
 * 累计有声时长够一个词;有声不足一句话的,尾静默窗口拉长——宁可多等,不可少录。
 */

/** 连续高于开口阈值这么久才算「开口」(累计,每帧最多记 VAD_ONSET_FRAME_CAP_MS)。 */
export const VAD_SPEECH_ONSET_MS = 250;

/**
 * 开口累计每帧最多记这么多毫秒:主线程卡顿 ≥250 ms 之后补来的一帧,读数只代表
 * analyser 窗口那 43 ms,不能让一声敲击靠「帧长」就凑够开口。64 ms 采样下 4 帧才够。
 */
export const VAD_ONSET_FRAME_CAP_MS = 130;

/** 开口之后连续低于收口阈值这么久判「说完」(有声时长达到 VAD_FULL_UTTERANCE_MS 时)。 */
export const VAD_TRAILING_SILENCE_MS = 3_000;

/**
 * 开口后累计有声时长(高于收口阈值的帧)不足这个数,不判「说完」:咳嗽(300–500 ms)、
 * 填充词「嗯」、桌上放杯子都不够。单音节目标词(「锚」「书」)也可能不够——那就照旧
 * 等按钮或作答窗口,与今天一样,不会更差。
 */
export const VAD_MIN_VOICED_MS = 450;

/**
 * 有声时长达到这个数才用 3 s 的尾静默;不足的(一个短词、半句话)用更长的窗口,
 * 给老人找词的停顿留余地。
 */
export const VAD_FULL_UTTERANCE_MS = 900;
export const VAD_SHORT_UTTERANCE_TRAILING_SILENCE_MS = 4_500;

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
 * 标定出的噪声底封顶:标定期混进语音时分位数也可能是语音级,不封顶的话开口阈值跟着
 * 上天,这一次回答 VAD 全程听不见。0.04 ≈ −28 dBFS:比正常语音(AGC 下 0.05–0.3)低,
 * 比一般房间的底(0.002–0.03)高;比这更吵的房间开口阈值封在 0.12,照样能判大声说话。
 */
export const VAD_NOISE_FLOOR_CAP_RMS = 0.04;

/** 标定完成前先用这个底判开口,老人第一帧就在说话也能记进有声时长。 */
export const VAD_PROVISIONAL_FLOOR_RMS = VAD_ABSOLUTE_MIN_RMS / 3;

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
  /** 开口之后累计的有声毫秒(高于收口阈值的帧)。 */
  readonly voicedMs: number;
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
  let calibrationMaxRms = 0;
  let floor: number | null = null;
  let loudMs = 0;
  let quietMs = 0;
  let voicedMs = 0;

  const effectiveFloor = (): number => floor ?? VAD_PROVISIONAL_FLOOR_RMS;
  const onsetOf = (base: number): number => Math.max(base * VAD_ONSET_FLOOR_RATIO, VAD_ABSOLUTE_MIN_RMS);
  const releaseOf = (base: number): number =>
    Math.max(base * VAD_RELEASE_FLOOR_RATIO, VAD_ABSOLUTE_MIN_RMS * VAD_RELEASE_MIN_RATIO);
  const onsetThreshold = (): number | null => floor === null ? null : onsetOf(floor);
  const releaseThreshold = (): number | null => floor === null ? null : releaseOf(floor);
  const trailingWindowMs = (): number => voicedMs >= VAD_FULL_UTTERANCE_MS
    ? VAD_TRAILING_SILENCE_MS : VAD_SHORT_UTTERANCE_TRAILING_SILENCE_MS;

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
    get voicedMs() { return voicedMs; },
    push(rms: number, atMs: number): VoiceActivityState {
      if (state === "stopped") return state;
      // 坏读数(NaN/负数)整帧丢掉:既不当静默也不当语音,时间也不推进。
      if (!Number.isFinite(rms) || rms < 0 || !Number.isFinite(atMs)) return state;
      const deltaMs = lastAtMs === null
        ? 0 : Math.min(Math.max(0, atMs - lastAtMs), VAD_MAX_FRAME_GAP_MS);
      lastAtMs = atMs;
      if (firstAtMs === null) firstAtMs = atMs;

      if (floor === null) {
        // 标定期每一帧都进噪声样本;同时按临时底照常累计开口/有声——老人在提问声里
        // 就开口时,第一帧就是话。标定一完成就用真底复核:临时判出的「开口」若其实
        // 低于真开口阈值(吵的房间的稳态噪声),撤回成 idle。
        calibration.push(rms);
        calibrationMaxRms = Math.max(calibrationMaxRms, rms);
        if (atMs - firstAtMs >= VAD_CALIBRATION_MS) {
          floor = Math.min(lowQuantile(calibration, VAD_CALIBRATION_QUANTILE), VAD_NOISE_FLOOR_CAP_RMS);
          if (state !== "idle" && calibrationMaxRms < onsetOf(floor)) {
            state = "idle";
            loudMs = 0;
            voicedMs = 0;
            quietMs = 0;
          }
        }
      }

      const base = effectiveFloor();
      const onset = onsetOf(base);
      const release = releaseOf(base);
      if (state === "idle") {
        if (rms >= onset) {
          loudMs += Math.min(deltaMs, VAD_ONSET_FRAME_CAP_MS);
          voicedMs += deltaMs;
          if (loudMs >= VAD_SPEECH_ONSET_MS) {
            state = "speaking";
            quietMs = 0;
          }
        } else {
          loudMs = 0;
          voicedMs = 0;
          if (floor !== null) trackFloor(rms, deltaMs);
        }
        return state;
      }

      // speaking / trailing_silence:低于收口阈值累计静默,高于就回到 speaking 并累计有声。
      if (rms < release) {
        quietMs += deltaMs;
        if (floor !== null) trackFloor(rms, deltaMs);
        state = voicedMs >= VAD_MIN_VOICED_MS && quietMs >= trailingWindowMs()
          ? "stopped" : "trailing_silence";
      } else {
        quietMs = 0;
        voicedMs += deltaMs;
        state = "speaking";
      }
      return state;
    },
  };
}
