import type { ClipClient } from './client.js';
import type { ClipEvent } from './types.js';
import { RTC_PHASE_LABELS, type RtcPhase } from './types.js';

/**
 * One logical utterance: a `RESUME -> PAUSE` interval of the warm-paused RTC
 * stream. Partial transcripts are folded into it as they arrive; the final
 * authoritative transcript and the assistant reply land on the same object.
 */
export interface Utterance {
  id: number;
  /** Latest known transcript (rolling partial, then authoritative final). */
  text: string;
  final: boolean;
  /** Final event marked it too short/empty: no LLM pass was run. */
  skipped: boolean;
  /** Assistant reply, set when the result event arrives. */
  answer?: string;
  conversationId?: string;
  session?: string | null;
  /** `web` (button/API) or `device` (physical double-click). */
  trigger?: string;
  startedAt: number;
  finishedAt?: number;
}

export interface RtcSnapshot {
  phase: RtcPhase;
  /** RTC session id currently armed, when known. */
  session: string | null;
  /** Id of the utterance being captured/finalized, when known. */
  utteranceId: number | null;
  /** A finalize job (STT + agent) is in flight. */
  processing: boolean;
  /** The assistant reply is being streamed token by token. */
  streaming: boolean;
  /** Reply text streamed so far. */
  streamingText: string;
  /** Tool the agent is calling, or null. */
  tool: string | null;
  /** Last RTC error reported by the runtime. */
  error: string | null;
  /** Ready-to-render status line. */
  statusLine: string;
}

export interface RtcControllerOptions {
  /**
   * A new utterance started capturing. Use it to stop in-flight TTS so the
   * user's next words are not drowned out by the previous answer.
   */
  onUtteranceStart?: (utterance: Utterance) => void;
  /** An utterance was answered (or skipped) and the reply is complete. */
  onUtteranceResult?: (utterance: Utterance) => void;
  /** Called with every snapshot update. */
  onChange?: (snapshot: RtcSnapshot) => void;
}

/**
 * Folds the Clip event stream into a small, renderable RTC state machine.
 *
 * ```ts
 * const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });
 * const rtc = new RtcSessionController(clip, { onUtteranceStart: () => stopTts() });
 * const sub = clip.subscribe({ onEvent: (event) => rtc.applyEvent(event) });
 * await rtc.resume();   // start talking
 * await rtc.pause();    // finalize the utterance
 * ```
 */
export class RtcSessionController {
  #client: ClipClient;
  #options: RtcControllerOptions;
  #snapshot: RtcSnapshot;
  #listeners = new Set<(snapshot: RtcSnapshot) => void>();
  #utterances = new Map<number, Utterance>();
  #current: Utterance | null = null;
  #lastResult: { session?: string; conversationId?: string; response?: string } | null = null;

  constructor(client: ClipClient, options: RtcControllerOptions = {}) {
    this.#client = client;
    this.#options = options;
    this.#snapshot = {
      phase: 'disconnected',
      session: null,
      utteranceId: null,
      processing: false,
      streaming: false,
      streamingText: '',
      tool: null,
      error: null,
      statusLine: RTC_PHASE_LABELS.disconnected,
    };
  }

  get snapshot(): RtcSnapshot {
    return this.#snapshot;
  }

  /** Utterance being captured / finalized right now, if any. */
  get current(): Utterance | null {
    return this.#current;
  }

  /** Every utterance seen so far, oldest first. */
  get utterances(): Utterance[] {
    return [...this.#utterances.values()].sort((a, b) => a.id - b.id);
  }

  /** Result of a non-utterance (SD session) reply, if one arrived. */
  get lastResult(): { session?: string; conversationId?: string; response?: string } | null {
    return this.#lastResult;
  }

  /** True while the device link is up and an RTC session is armed. */
  get isArmed(): boolean {
    const { phase } = this.#snapshot;
    return phase === 'paused' || phase === 'capturing' || phase === 'arming' || phase === 'finalizing';
  }

  subscribe(listener: (snapshot: RtcSnapshot) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /** Start capturing the next utterance. */
  async resume(): Promise<void> {
    this.#applyState({ phase: 'capturing' });
    const result = await this.#client.streamResume();
    this.#applyState({
      phase: (result.phase as RtcPhase | undefined) ?? 'capturing',
      session: result.session ?? this.#snapshot.session,
      utteranceId: result.utterance_id ?? this.#snapshot.utteranceId,
    });
  }

  /** Warm-pause the stream: finalizes the current utterance. */
  async pause(): Promise<void> {
    this.#applyState({ phase: 'finalizing', processing: true });
    const result = await this.#client.streamPause();
    this.#applyState({
      phase: (result.phase as RtcPhase | undefined) ?? 'finalizing',
      session: result.session ?? this.#snapshot.session,
      utteranceId: result.utterance_id ?? this.#snapshot.utteranceId,
    });
  }

  /** Push the latest service status into the controller (e.g. after reconnect). */
  syncStatus(status: {
    connected?: boolean;
    rtc_phase?: RtcPhase;
    rtc_session?: string | null;
    rtc_utterance_id?: number | null;
    rtc_partial_transcript?: string;
    rtc_processing?: boolean;
    rtc_error?: string | null;
  }): void {
    if (status.connected === false) {
      this.#applyState({ phase: 'disconnected', session: null, processing: false });
      return;
    }
    const patch: Partial<RtcSnapshot> = {};
    if (status.rtc_phase) patch.phase = status.rtc_phase;
    if (status.rtc_session !== undefined) patch.session = status.rtc_session ?? null;
    if (status.rtc_utterance_id !== undefined) patch.utteranceId = status.rtc_utterance_id ?? null;
    if (status.rtc_processing !== undefined) patch.processing = status.rtc_processing;
    if (status.rtc_error !== undefined) patch.error = status.rtc_error ?? null;
    this.#applyState(patch);
    if (status.rtc_partial_transcript && this.#current) {
      this.#current.text = status.rtc_partial_transcript;
    }
  }

  /** Fold one SSE event from `GET /api/clip/events`. */
  applyEvent(event: ClipEvent): void {
    switch (event.event) {
      case 'connection': {
        const data = event.data;
        if (!data.connected) {
          this.#applyState({
            phase: 'disconnected',
            session: null,
            processing: false,
            streaming: false,
            error: data.error ?? this.#snapshot.error,
          });
        } else {
          this.#applyState({ error: data.error ?? null });
        }
        return;
      }
      case 'rtc_state': {
        const data = event.data;
        const nextId = data.utterance_id ?? null;
        // A capturing event for an utterance we have not seen yet opens a new
        // logical utterance; replays of the same id are ignored.
        const startsUtterance =
          data.phase === 'capturing' && nextId !== null && this.#current?.id !== nextId;
        if (startsUtterance && nextId !== null) {
          const utterance = this.#track(nextId, data.trigger, data.session ?? null);
          this.#current = utterance;
        }
        const resetStream = startsUtterance || (data.phase === 'capturing' && nextId === null);
        this.#applyState({
          phase: data.phase,
          utteranceId: nextId ?? this.#snapshot.utteranceId,
          session: data.session ?? this.#snapshot.session,
          processing: startsUtterance
            ? false
            : data.phase === 'finalizing'
              ? true
              : this.#snapshot.processing,
          error: startsUtterance
            ? (data.error ?? null)
            : (data.error ?? (data.phase === 'capturing' ? null : this.#snapshot.error)),
          streaming: data.phase === 'capturing' ? false : this.#snapshot.streaming,
          streamingText: resetStream ? '' : this.#snapshot.streamingText,
          tool: resetStream ? null : this.#snapshot.tool,
        });
        if (startsUtterance && this.#current) {
          this.#options.onUtteranceStart?.(this.#current);
        }
        return;
      }
      case 'transcript': {
        const data = event.data;
        const utterance = this.#track(data.utterance_id, 'device');
        if (data.final) {
          utterance.final = true;
          utterance.skipped = Boolean(data.skipped) || !data.text;
          // An empty/skipped final must not leave stale partial text behind.
          utterance.text = utterance.skipped ? '' : data.text;
          utterance.finishedAt = Date.now();
          return;
        }
        if (!data.text) return;
        utterance.text = data.text;
        this.#emit();
        return;
      }
      case 'thinking': {
        this.#applyState({
          processing: true,
          streaming: true,
          tool: event.data.tool ? event.data.tool : null,
        });
        return;
      }
      case 'token': {
        if (!event.data.text) return;
        this.#applyState({
          streaming: true,
          streamingText: this.#snapshot.streamingText + event.data.text,
        });
        return;
      }
      case 'result': {
        const data = event.data;
        const utteranceId = data.utterance_id;
        if (utteranceId !== undefined && utteranceId !== null) {
          const utterance = this.#utterances.get(utteranceId);
          if (utterance) {
            if (!utterance.final) utterance.final = true;
            if (data.transcript) utterance.text = data.transcript;
            utterance.answer = data.response;
            utterance.conversationId = data.conversation_id;
            utterance.session = data.session ?? utterance.session;
            utterance.finishedAt = Date.now();
          }
        }
        this.#lastResult = {
          session: data.session,
          conversationId: data.conversation_id,
          response: data.response,
        };
        this.#current = null;
        this.#applyState({
          processing: false,
          streaming: false,
          streamingText: '',
          tool: null,
          phase: this.#snapshot.phase === 'capturing' ? 'capturing' : 'paused',
        });
        const finished =
          utteranceId !== undefined && utteranceId !== null
            ? this.#utterances.get(utteranceId)
            : undefined;
        if (finished) this.#options.onUtteranceResult?.(finished);
        this.#emit();
        return;
      }
      default:
        // recording/workflow/rtc_device do not affect the RTC state machine.
        return;
    }
  }

  /** Drop all utterance history (e.g. after a reconnect or a conversation switch). */
  reset(): void {
    this.#utterances.clear();
    this.#current = null;
    this.#lastResult = null;
    this.#applyState({
      utteranceId: null,
      processing: false,
      streaming: false,
      streamingText: '',
      tool: null,
      error: null,
    });
  }

  /** Look up an utterance, registering it on first sight (e.g. after reconnect). */
  #track(id: number, trigger?: string, session?: string | null): Utterance {
    const existing = this.#utterances.get(id);
    if (existing) return existing;
    const utterance: Utterance = {
      id,
      text: '',
      final: false,
      skipped: false,
      trigger,
      session: session ?? this.#snapshot.session,
      startedAt: Date.now(),
    };
    this.#utterances.set(id, utterance);
    if (!this.#current) this.#current = utterance;
    return utterance;
  }

  #applyState(patch: Partial<RtcSnapshot>): void {
    this.#snapshot = { ...this.#snapshot, ...patch };
    this.#snapshot.statusLine = describeRtcPhase(this.#snapshot);
    this.#emit();
  }

  #emit(): void {
    const snapshot = this.#snapshot;
    for (const listener of this.#listeners) {
      try {
        listener(snapshot);
      } catch {
        /* listener errors must not break the fold */
      }
    }
    try {
      this.#options.onChange?.(snapshot);
    } catch {
      /* ignore */
    }
  }
}

/** Status line for a snapshot, mirroring the reference web UI wording. */
export function describeRtcPhase(snapshot: RtcSnapshot): string {
  if (snapshot.error && snapshot.phase !== 'capturing') {
    return `RTC error: ${snapshot.error}`;
  }
  switch (snapshot.phase) {
    case 'capturing':
      return snapshot.utteranceId !== null
        ? `Listening — utterance ${snapshot.utteranceId}`
        : RTC_PHASE_LABELS.capturing;
    case 'finalizing':
      return snapshot.utteranceId !== null
        ? `Finalizing utterance ${snapshot.utteranceId}`
        : RTC_PHASE_LABELS.finalizing;
    case 'paused':
      if (snapshot.processing) return RTC_PHASE_LABELS.finalizing;
      return RTC_PHASE_LABELS.paused;
    default:
      return RTC_PHASE_LABELS[snapshot.phase];
  }
}