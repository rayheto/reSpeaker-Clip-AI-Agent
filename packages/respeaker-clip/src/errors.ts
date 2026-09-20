import type { ClipEventName } from './types.js';

/**
 * Failure codes. They mirror the service's error mapping:
 * 400 bad input, 409 state conflict, 502 command/transfer failure,
 * 503 unavailable / reconnecting.
 */
export type ClipErrorCode =
  | 'bad_input'
  | 'conflict'
  | 'command_failed'
  | 'unavailable'
  | 'http'
  | 'network'
  | 'timeout'
  | 'aborted';

const CODE_BY_STATUS: Record<number, ClipErrorCode> = {
  400: 'bad_input',
  409: 'conflict',
  502: 'command_failed',
  503: 'unavailable',
};

export interface ClipApiErrorOptions {
  code: ClipErrorCode;
  status?: number;
  /** Parsed JSON body from the service, when it sent one. */
  body?: unknown;
  cause?: unknown;
}

export class ClipApiError extends Error {
  readonly code: ClipErrorCode;
  /** HTTP status, or 0 for transport failures (network/timeout/abort). */
  readonly status: number;
  readonly body?: unknown;
  /** Set when the error came from a named SSE event handler. */
  readonly event?: ClipEventName;

  constructor(message: string, options: ClipApiErrorOptions) {
    super(message, options.cause !== undefined ? { cause: options.cause } : undefined);
    this.name = 'ClipApiError';
    this.code = options.code;
    this.status = options.status ?? 0;
    this.body = options.body;
  }

  /** True when the service answered 503: the Clip link is down or reconnecting. */
  get isOffline(): boolean {
    return this.code === 'unavailable';
  }

  /** True when the request never got an HTTP answer (or was cancelled). */
  get isTransport(): boolean {
    return this.code === 'network' || this.code === 'timeout' || this.code === 'aborted';
  }

  static fromResponse(status: number, body: unknown, fallbackMessage: string): ClipApiError {
    const message =
      body && typeof body === 'object' && typeof (body as { error?: unknown }).error === 'string'
        ? String((body as { error: unknown }).error)
        : fallbackMessage;
    return new ClipApiError(message, {
      code: CODE_BY_STATUS[status] ?? 'http',
      status,
      body,
    });
  }
}

/**
 * True when `error` is a 503 from the service, i.e. the Clip link is down.
 * Convenience for callers that treat "offline" as a normal state.
 */
export function isOfflineError(error: unknown): boolean {
  return error instanceof ClipApiError && error.isOffline;
}