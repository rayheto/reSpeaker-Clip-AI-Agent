import { ClipApiError } from './errors.js';
import { SseParser } from './sse.js';
import type {
  ClipEvent,
  ClipEventMap,
  ClipEventName,
  ClipStatus,
  IngestResult,
  RecordingEvent,
  ResultEvent,
  RtcControlResult,
  RtcDeviceEvent,
  RtcStateEvent,
  StartRecordingResult,
  StopRecordingResult,
  ThinkingEvent,
  TokenEvent,
  TranscriptEvent,
  ConnectionEvent,
  SessionAudioEvent,
  UtteranceAudioEvent,
  WorkflowEvent,
} from './types.js';

export interface FetchLike {
  (input: string, init?: RequestInit): Promise<Response>;
}

export interface ClipClientOptions {
  /** Service origin, e.g. `http://localhost:5000`. Default `http://127.0.0.1:5000`. */
  baseUrl?: string;
  /** Extra headers sent with every request (auth, tenant, ...). */
  headers?: Record<string, string>;
  /** Custom fetch implementation (polyfill, proxy, test double). */
  fetch?: FetchLike;
  /** Per-request timeout in ms. Default 15000; `0` disables it. */
  timeoutMs?: number;
}

export interface SubscribeHandlers {
  onConnection?: (event: ConnectionEvent) => void;
  onRecording?: (event: RecordingEvent) => void;
  onWorkflow?: (event: WorkflowEvent) => void;
  onResult?: (event: ResultEvent) => void;
  onRtcState?: (event: RtcStateEvent) => void;
  onRtcDevice?: (event: RtcDeviceEvent) => void;
  onTranscript?: (event: TranscriptEvent) => void;
  onThinking?: (event: ThinkingEvent) => void;
  onToken?: (event: TokenEvent) => void;
  /** Device gateway: one utterance's Ogg is ready to fetch. */
  onUtteranceAudio?: (event: UtteranceAudioEvent) => void;
  /** Device gateway: a downloaded session's Ogg is ready to fetch. */
  onSessionAudio?: (event: SessionAudioEvent) => void;
  /** Every named event, after the specific handler. */
  onEvent?: (event: ClipEvent) => void;
  /** Transport-level failures; the subscription reconnects unless disabled. */
  onError?: (error: ClipApiError) => void;
  /** The stream is open and delivering events. */
  onOpen?: () => void;
}

export type SubscriptionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface SubscribeOptions {
  /** Replay from this `Last-Event-ID` (number or opaque string). */
  since?: string | number | null;
  /** Abort the subscription. */
  signal?: AbortSignal;
  /** Reconnect after a dropped stream. Default true. */
  reconnect?: boolean;
  /** First reconnect delay in ms. Default 1000. */
  minReconnectDelayMs?: number;
  /** Reconnect delay cap in ms. Default 30000. */
  maxReconnectDelayMs?: number;
  onStateChange?: (state: SubscriptionState, info: { error?: ClipApiError; attempt: number }) => void;
}

export interface ClipSubscription {
  readonly state: SubscriptionState;
  readonly lastEventId: string | null;
  /** Number of completed connects (1 after the first successful open). */
  readonly attempt: number;
  close(): void;
}

const DEFAULT_BASE_URL = 'http://127.0.0.1:5000';
const DEFAULT_TIMEOUT_MS = 15_000;
const SESSION_ID_RE = /^\d{14}$/;

interface Timing {
  signal: AbortSignal;
  timedOut: () => boolean;
  cleanup: () => void;
}

function combineSignals(signal: AbortSignal | undefined, timeoutMs: number): Timing {
  const controller = new AbortController();
  let timedOut = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const onAbort = () => controller.abort(signal?.reason);
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort(new Error('clip request timed out'));
    }, timeoutMs);
  }
  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    cleanup: () => {
      if (timer !== undefined) clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    },
  };
}

function defaultFetch(): FetchLike {
  const impl = globalThis.fetch as FetchLike | undefined;
  if (typeof impl !== 'function') {
    throw new ClipApiError(
      'global fetch is unavailable; pass a fetch implementation in ClipClientOptions',
      { code: 'network' },
    );
  }
  return (input, init) => impl(input, init);
}

/**
 * Typed client for the reSpeaker Clip service HTTP API.
 *
 * ```ts
 * const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });
 * const status = await clip.getStatus();
 * if (status.rtc_phase === 'paused') await clip.streamResume();
 * ```
 */
export class ClipClient {
  readonly baseUrl: string;
  readonly timeoutMs: number;

  #headers: Record<string, string>;
  #fetch: FetchLike;

  constructor(options: ClipClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.#headers = { ...(options.headers ?? {}) };
    this.#fetch = options.fetch ?? defaultFetch();
  }

  /** Absolute URL of `GET /api/clip/events`. */
  eventsUrl(): string {
    return `${this.baseUrl}/api/clip/events`;
  }

  /** Connection, recording and RTC streaming state. */
  getStatus(signal?: AbortSignal): Promise<ClipStatus> {
    return this.#request<ClipStatus>('GET', '/api/clip/status', undefined, signal);
  }

  /**
   * Start a legacy (SD) recording session. With an armed RTC session the
   * service answers 409 — use `streamResume`/`streamPause` instead.
   */
  startRecording(
    options: { mode?: 'normal' | 'enhanced'; conversationId?: string } = {},
    signal?: AbortSignal,
  ): Promise<StartRecordingResult> {
    return this.#request<StartRecordingResult>(
      'POST',
      '/api/clip/recordings/start',
      { mode: options.mode, conversation_id: options.conversationId },
      signal,
    );
  }

  /** Stop the running recording; the session then enters the ingest workflow. */
  stopRecording(signal?: AbortSignal): Promise<StopRecordingResult> {
    return this.#request<StopRecordingResult>('POST', '/api/clip/recordings/stop', {}, signal);
  }

  /** Resume the armed RTC session — starts the next logical utterance. */
  streamResume(signal?: AbortSignal): Promise<RtcControlResult> {
    return this.#request<RtcControlResult>('POST', '/api/clip/stream/resume', {}, signal);
  }

  /** Warm-pause the RTC session — finalizes the current utterance. */
  streamPause(signal?: AbortSignal): Promise<RtcControlResult> {
    return this.#request<RtcControlResult>('POST', '/api/clip/stream/pause', {}, signal);
  }

  /** Idempotently enqueue a stopped SD session for download + transcription. */
  ingest(
    sessionId: string,
    options: { trigger?: string; conversationId?: string } = {},
    signal?: AbortSignal,
  ): Promise<IngestResult> {
    if (!SESSION_ID_RE.test(sessionId)) {
      return Promise.reject(invalidSessionId(sessionId));
    }
    return this.#request<IngestResult>(
      'POST',
      `/api/clip/sessions/${sessionId}/ingest`,
      { trigger: options.trigger ?? 'manual', conversation_id: options.conversationId },
      signal,
    );
  }

  /** Attach subsequent transcripts to this conversation. */
  registerContext(
    conversationId: string,
    signal?: AbortSignal,
  ): Promise<{ accepted: boolean; conversation_id: string }> {
    return this.#request('POST', '/api/clip/context', { conversation_id: conversationId }, signal);
  }

  /**
   * URL of one utterance's Ogg. In device-gateway mode (`--no-agent`) the
   * `utterance_audio` event carries exactly this path; the bytes are the raw
   * audio for the consumer to transcribe.
   */
  utteranceAudioUrl(sessionId: string, utteranceId: number): string {
    return `${this.baseUrl}/api/clip/utterances/${sessionId}/${utteranceId}/audio`;
  }

  /** URL of a downloaded session's re-containerized Ogg. */
  sessionAudioUrl(sessionId: string): string {
    return `${this.baseUrl}/api/clip/sessions/${sessionId}/audio`;
  }

  /** Download one utterance's Ogg bytes (device-gateway mode). */
  utteranceAudio(
    sessionId: string,
    utteranceId: number,
    signal?: AbortSignal,
  ): Promise<ArrayBuffer> {
    if (!SESSION_ID_RE.test(sessionId)) {
      return Promise.reject(invalidSessionId(sessionId));
    }
    if (!Number.isInteger(utteranceId) || utteranceId < 0) {
      return Promise.reject(
        new ClipApiError(`invalid utterance id: ${utteranceId}`, { code: 'bad_input', status: 400 }),
      );
    }
    return this.#requestBytes(`/api/clip/utterances/${sessionId}/${utteranceId}/audio`, signal);
  }

  /** Download a downloaded session's Ogg bytes. */
  sessionAudio(sessionId: string, signal?: AbortSignal): Promise<ArrayBuffer> {
    if (!SESSION_ID_RE.test(sessionId)) {
      return Promise.reject(invalidSessionId(sessionId));
    }
    return this.#requestBytes(`/api/clip/sessions/${sessionId}/audio`, signal);
  }

  /**
   * Subscribe to `GET /api/clip/events`.
   *
   * Uses a streaming `fetch` body (no `EventSource` global needed), replays
   * missed events through `Last-Event-ID` and reconnects with jittered
   * exponential backoff until `close()` or the abort signal.
   */
  subscribe(handlers: SubscribeHandlers, options: SubscribeOptions = {}): ClipSubscription {
    const parser = new SseParser();
    const reconnect = options.reconnect ?? true;
    const minDelay = Math.max(1, options.minReconnectDelayMs ?? 1_000);
    const maxDelay = Math.max(minDelay, options.maxReconnectDelayMs ?? 30_000);
    const abort = new AbortController();
    const onExternalAbort = () => abort.abort(options.signal?.reason);
    if (options.signal) {
      if (options.signal.aborted) abort.abort(options.signal.reason);
      else options.signal.addEventListener('abort', onExternalAbort, { once: true });
    }

    let state: SubscriptionState = 'idle';
    let lastEventId: string | null =
      options.since === null || options.since === undefined ? null : String(options.since);
    let attempts = 0;
    let done = false;

    const subscription: ClipSubscription = {
      get state() {
        return state;
      },
      get lastEventId() {
        return lastEventId;
      },
      get attempt() {
        return attempts;
      },
      close() {
        if (done) return;
        done = true;
        options.signal?.removeEventListener('abort', onExternalAbort);
        setState('closed');
        abort.abort();
      },
    };

    function setState(next: SubscriptionState, error?: ClipApiError): void {
      state = next;
      try {
        options.onStateChange?.(next, { error, attempt: attempts });
      } catch {
        /* listener errors must not kill the stream */
      }
    }

    const dispatch = (name: string, data: string): void => {
      let parsed: unknown;
      try {
        parsed = data === '' ? {} : JSON.parse(data);
      } catch {
        return; // ignore malformed frames rather than dropping the stream
      }
      const event = { event: name, data: parsed } as ClipEvent;
      try {
        switch (name) {
          case 'connection':
            handlers.onConnection?.(event.data as ConnectionEvent);
            break;
          case 'recording':
            handlers.onRecording?.(event.data as RecordingEvent);
            break;
          case 'workflow':
            handlers.onWorkflow?.(event.data as WorkflowEvent);
            break;
          case 'result':
            handlers.onResult?.(event.data as ResultEvent);
            break;
          case 'rtc_state':
            handlers.onRtcState?.(event.data as RtcStateEvent);
            break;
          case 'rtc_device':
            handlers.onRtcDevice?.(event.data as RtcDeviceEvent);
            break;
          case 'transcript':
            handlers.onTranscript?.(event.data as TranscriptEvent);
            break;
          case 'thinking':
            handlers.onThinking?.(event.data as ThinkingEvent);
            break;
          case 'token':
            handlers.onToken?.(event.data as TokenEvent);
            break;
          case 'utterance_audio':
            handlers.onUtteranceAudio?.(event.data as UtteranceAudioEvent);
            break;
          case 'session_audio':
            handlers.onSessionAudio?.(event.data as SessionAudioEvent);
            break;
          default:
            break;
        }
        handlers.onEvent?.(event);
      } catch (error) {
        const wrapped = new ClipApiError(
          `handler for ${name} threw: ${(error as Error)?.message ?? String(error)}`,
          { code: 'http', cause: error },
        );
        try {
          handlers.onError?.(wrapped);
        } catch {
          /* ignore */
        }
      }
    };

    const read = async (): Promise<void> => {
      while (!done) {
        setState(attempts === 0 ? 'connecting' : 'reconnecting');
        const headers: Record<string, string> = {
          Accept: 'text/event-stream',
          ...this.#headers,
        };
        if (lastEventId) headers['Last-Event-ID'] = lastEventId;

        try {
          const response = await this.#fetch(this.eventsUrl(), {
            method: 'GET',
            headers,
            signal: abort.signal,
          });
          if (!response.ok) {
            const body = await safeJson(response);
            throw ClipApiError.fromResponse(
              response.status,
              body,
              `event stream refused with HTTP ${response.status}`,
            );
          }
          if (!response.body) {
            throw new ClipApiError('event stream returned no body', { code: 'network' });
          }

          attempts += 1;
          parser.reset();
          setState('open');
          try {
            handlers.onOpen?.();
          } catch {
            /* ignore */
          }

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          for (;;) {
            const { done: finished, value } = await reader.read();
            if (finished) break;
            const chunk = decoder.decode(value, { stream: true });
            for (const message of parser.push(chunk)) {
              if (parser.lastEventId !== undefined) lastEventId = parser.lastEventId;
              if (message.event === 'ping') continue;
              dispatch(message.event, message.data);
            }
            if (done) {
              await reader.cancel().catch(() => undefined);
              break;
            }
          }
          if (done) break;
          throw new ClipApiError('event stream ended', { code: 'network' });
        } catch (error) {
          if (done || abort.signal.aborted) break;
          const failure =
            error instanceof ClipApiError
              ? error
              : new ClipApiError(
                  `event stream failed: ${(error as Error)?.message ?? String(error)}`,
                  { code: 'network', cause: error },
                );
          try {
            handlers.onError?.(failure);
          } catch {
            /* ignore */
          }
          if (!reconnect) {
            setState('closed', failure);
            return;
          }
          const delay = Math.min(maxDelay, minDelay * 2 ** Math.min(attempts, 6));
          const jittered = Math.round(delay * (0.5 + Math.random() / 2));
          setState('reconnecting', failure);
          await sleep(jittered, abort.signal);
        }
      }
      setState('closed');
    };

    // The loop owns its own error handling; this guards against an unexpected
    // throw escaping it as an unhandled rejection.
    void read().catch((error) => {
      done = true;
      setState('closed');
      try {
        handlers.onError?.(
          error instanceof ClipApiError
            ? error
            : new ClipApiError(String((error as Error)?.message ?? error), {
                code: 'network',
                cause: error,
              }),
        );
      } catch {
        /* ignore */
      }
    });
    return subscription;
  }

  async #request<T>(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    const timing = combineSignals(signal, this.timeoutMs);
    const headers: Record<string, string> = { Accept: 'application/json', ...this.#headers };
    const init: RequestInit = { method, headers, signal: timing.signal };
    if (body !== undefined && method !== 'GET') {
      const payload = Object.fromEntries(
        Object.entries(body as Record<string, unknown>).filter(([, value]) => value !== undefined),
      );
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(payload);
    }

    const response = await this.#send(method, path, init, timing, signal);
    const parsed = await safeJson(response);
    if (!response.ok) {
      throw ClipApiError.fromResponse(
        response.status,
        parsed,
        `${method} ${path} failed with HTTP ${response.status}`,
      );
    }
    return (parsed ?? {}) as T;
  }

  /** GET raw bytes (audio exchange). */
  async #requestBytes(path: string, signal?: AbortSignal): Promise<ArrayBuffer> {
    const timing = combineSignals(signal, this.timeoutMs);
    const init: RequestInit = {
      method: 'GET',
      headers: { Accept: 'audio/ogg, application/octet-stream', ...this.#headers },
      signal: timing.signal,
    };

    const response = await this.#send('GET', path, init, timing, signal);
    if (!response.ok) {
      throw ClipApiError.fromResponse(
        response.status,
        await safeJson(response),
        `GET ${path} failed with HTTP ${response.status}`,
      );
    }
    return await response.arrayBuffer();
  }

  /** Perform one request, mapping transport failures to typed errors. */
  async #send(
    method: string,
    path: string,
    init: RequestInit,
    timing: Timing,
    signal?: AbortSignal,
  ): Promise<Response> {
    try {
      return await this.#fetch(`${this.baseUrl}${path}`, init);
    } catch (error) {
      if (timing.timedOut()) {
        throw new ClipApiError(`request to ${path} timed out after ${this.timeoutMs}ms`, {
          code: 'timeout',
          cause: error,
        });
      }
      if (signal?.aborted || timing.signal.aborted) {
        throw new ClipApiError(`request to ${path} was aborted`, { code: 'aborted', cause: error });
      }
      throw new ClipApiError(
        `cannot reach the Clip service at ${this.baseUrl}: ${(error as Error)?.message ?? String(error)}`,
        { code: 'network', cause: error },
      );
    } finally {
      timing.cleanup();
    }
  }
}

function invalidSessionId(sessionId: string): ClipApiError {
  return new ClipApiError(
    `invalid session_id: ${JSON.stringify(sessionId)} (expected 14 digits)`,
    { code: 'bad_input', status: 400 },
  );
}

async function safeJson(response: Response): Promise<unknown> {
  try {
    const text = await response.text();
    if (!text) return undefined;
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Typed helper: build an event object for tests or replaying captured frames. */
export function makeEvent<K extends ClipEventName>(
  event: K,
  data: ClipEventMap[K],
): ClipEvent {
  return { event, data } as ClipEvent;
}