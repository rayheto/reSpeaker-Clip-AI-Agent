/**
 * reSpeaker Clip — client SDK for the Clip voice service.
 *
 * ```ts
 * import { ClipClient, RtcSessionController } from 'respeaker-clip';
 *
 * const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });
 * const rtc = new RtcSessionController(clip);
 * const sub = clip.subscribe({
 *   onEvent: (event) => rtc.applyEvent(event),
 *   onTranscript: (t) => console.log(t.final ? 'final:' : 'partial:', t.text),
 *   onResult: (r) => console.log('assistant:', r.response),
 * });
 *
 * await rtc.resume();
 * await rtc.pause();
 * ```
 *
 * This entry point is browser-safe. Node-only helpers (service bootstrap and
 * the CLI) live in `respeaker-clip/service`.
 */

export { ClipClient, makeEvent } from './client.js';
export type {
  ClipClientOptions,
  ClipSubscription,
  FetchLike,
  SubscribeHandlers,
  SubscribeOptions,
  SubscriptionState,
} from './client.js';

export { ClipApiError, isOfflineError } from './errors.js';
export type { ClipApiErrorOptions, ClipErrorCode } from './errors.js';

export { SseParser, parseSse } from './sse.js';
export type { SseMessage } from './sse.js';

export { RtcSessionController, describeRtcPhase } from './rtc.js';
export type { RtcControllerOptions, RtcSnapshot, Utterance } from './rtc.js';

export { RTC_PHASES, RTC_PHASE_LABELS } from './types.js';
export type {
  ClipDeviceStatus,
  ClipEvent,
  ClipEventMap,
  ClipEventName,
  ClipStatus,
  ConnectionEvent,
  IngestResult,
  RecordingEvent,
  ResultEvent,
  RtcControlResult,
  RtcDeviceEvent,
  RtcPhase,
  RtcStateEvent,
  SessionAudioEvent,
  StartRecordingResult,
  StopRecordingResult,
  ThinkingEvent,
  TokenEvent,
  TranscriptEvent,
  UtteranceAudioEvent,
  WorkflowEvent,
} from './types.js';