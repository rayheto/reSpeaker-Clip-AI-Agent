/**
 * Wire types for the reSpeaker Clip service (`/api/clip/*`).
 *
 * These mirror the payloads produced by `backend/clip/runtime.py`
 * (`status_payload`), `backend/routes/clip.py` and the SSE event stream.
 */

/** Warm-pause RTC streaming phase reported by the device runtime. */
export type RtcPhase =
  | 'disconnected'
  | 'arming'
  | 'paused'
  | 'capturing'
  | 'finalizing'
  | 'stopped';

export const RTC_PHASES: readonly RtcPhase[] = [
  'disconnected',
  'arming',
  'paused',
  'capturing',
  'finalizing',
  'stopped',
];

/** Human-readable status lines, matching what the reference web UI shows. */
export const RTC_PHASE_LABELS: Record<RtcPhase, string> = {
  disconnected: 'Clip disconnected',
  arming: 'Armed — starting',
  paused: 'Armed (warm pause) — no BLE audio while paused',
  capturing: 'Listening',
  finalizing: 'Finalizing',
  stopped: 'RTC stopped — start a recording to restart',
};

/** Device-side status, as reported by the Clip firmware over BLE. */
export interface ClipDeviceStatus {
  state?: string;
  recording?: boolean;
  session?: string | null;
  duration_seconds?: number;
  battery_percent?: number;
  charging?: boolean;
  temperature_c?: number | null;
  mode?: string;
  bitrate?: number;
  free_space_mb?: number;
  device_name?: string;
  [key: string]: unknown;
}

/** `GET /api/clip/status` (and the `connection` event's `status` field). */
export interface ClipStatus {
  connected: boolean;
  /** Present on the HTTP payload; absent on the raw device status. */
  available?: boolean;
  device_id?: string | null;
  recording: boolean;
  session?: string | null;
  transfer_active?: boolean;
  last_error?: string | null;
  input_mode?: 'browser' | 'clip' | 'both' | string;
  record_mode?: 'normal' | 'enhanced' | string;
  /** False when the service runs as a device gateway (no agent, no STT). */
  agent_enabled?: boolean;
  rtc_phase?: RtcPhase;
  rtc_session?: string | null;
  rtc_utterance_id?: number | null;
  rtc_partial_transcript?: string;
  rtc_processing?: boolean;
  rtc_error?: string | null;
  [key: string]: unknown;
}

export interface ConnectionEvent {
  connected: boolean;
  error?: string | null;
  status?: ClipDeviceStatus | null;
}

export interface RecordingEvent {
  action: 'started' | 'stopped';
  session?: string;
  trigger?: 'web' | 'physical' | string;
}

export interface WorkflowEvent {
  status: 'stopped' | 'downloading' | 'processing' | 'completed' | 'failed';
  session?: string;
  error?: string;
}

export interface ResultEvent {
  session?: string;
  conversation_id?: string;
  transcript?: string;
  response?: string;
  trigger?: string;
  /** Present for RTC utterances; `null`/absent for SD session results. */
  utterance_id?: number | null;
}

export interface RtcStateEvent {
  phase: RtcPhase;
  session?: string | null;
  utterance_id?: number | null;
  trigger?: 'web' | 'device' | string;
  error?: string | null;
}

/** Firmware RTC status notification (`event: rtc_device`). */
export interface RtcDeviceEvent {
  status: string;
  session?: string | null;
}

export interface TranscriptEvent {
  utterance_id: number;
  text: string;
  /** `false` for a rolling partial, `true` for the authoritative result. */
  final: boolean;
  /** Set on a final event when the utterance was too short / empty. */
  skipped?: boolean;
}

export interface ThinkingEvent {
  /** Tool the agent is about to call, or `''` when generation just started. */
  tool?: string;
}

export interface TokenEvent {
  text: string;
}

/**
 * Device-gateway mode only: the Ogg snapshot of one finalized utterance.
 * Emitted instead of `transcript`/`result` when the service runs without the
 * agent (`--no-agent`), so the consumer does its own ASR.
 */
export interface UtteranceAudioEvent {
  utterance_id: number;
  session?: string | null;
  /** Fetchable Ogg URL; absent when the utterance was skipped or failed. */
  url?: string;
  bytes?: number;
  content_type?: string;
  trigger?: string;
  /** Set when no audio was produced at all (e.g. `too short`). */
  skipped?: string;
  error?: string;
}

/** Device-gateway mode only: the Ogg of a downloaded SD session. */
export interface SessionAudioEvent {
  session: string;
  url: string;
  bytes?: number;
  content_type?: string;
  trigger?: string;
  error?: string;
}

/** Named events carried by `GET /api/clip/events`. */
export interface ClipEventMap {
  connection: ConnectionEvent;
  recording: RecordingEvent;
  workflow: WorkflowEvent;
  result: ResultEvent;
  rtc_state: RtcStateEvent;
  rtc_device: RtcDeviceEvent;
  transcript: TranscriptEvent;
  thinking: ThinkingEvent;
  token: TokenEvent;
  utterance_audio: UtteranceAudioEvent;
  session_audio: SessionAudioEvent;
}

export type ClipEventName = keyof ClipEventMap;

export type ClipEvent = {
  [K in ClipEventName]: { event: K; data: ClipEventMap[K]; id?: string };
}[ClipEventName];

export interface StartRecordingResult {
  conversation_id?: string;
  session?: string;
  [key: string]: unknown;
}

export interface StopRecordingResult {
  accepted?: boolean;
  session?: string;
  [key: string]: unknown;
}

export interface RtcControlResult {
  accepted?: boolean;
  phase?: RtcPhase;
  utterance_id?: number | null;
  session?: string | null;
  [key: string]: unknown;
}

export interface IngestResult {
  accepted?: boolean;
  session?: string;
  status?: string;
  reason?: string;
  [key: string]: unknown;
}