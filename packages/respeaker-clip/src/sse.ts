/**
 * Minimal Server-Sent Events parser.
 *
 * Written against the WHATWG event-stream rules (multi-line `data`, comment
 * keep-alives, `id:`/`retry:` fields, CR/LF/CRLF) so it can run on a plain
 * `fetch` body stream in Node and in the browser alike — no `EventSource`
 * dependency, which keeps the package zero-dependency.
 */

export interface SseMessage {
  /** Last `id:` seen before this message, when the stream carried one. */
  id?: string;
  /** Event name; `'message'` when the stream did not name one. */
  event: string;
  /** `data:` lines joined with `\n`, without the trailing newline. */
  data: string;
  /** Server-suggested reconnect delay in milliseconds. */
  retry?: number;
}

export class SseParser {
  #buffer = '';
  #dataLines: string[] = [];
  #eventName = '';
  #retry: number | undefined;
  #lastEventId: string | undefined;
  #hasFields = false;

  /** Last `id:` value seen, for `Last-Event-ID` on reconnect. */
  get lastEventId(): string | undefined {
    return this.#lastEventId;
  }

  /** Feed a decoded chunk; returns every message that is now complete. */
  push(chunk: string): SseMessage[] {
    this.#buffer += chunk;
    const out: SseMessage[] = [];
    let newline = this.#buffer.indexOf('\n');
    while (newline !== -1) {
      let line = this.#buffer.slice(0, newline);
      this.#buffer = this.#buffer.slice(newline + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      const message = this.#handleLine(line);
      if (message) out.push(message);
      newline = this.#buffer.indexOf('\n');
    }
    return out;
  }

  /** Reset all in-flight state (used when the connection is replaced). */
  reset(): void {
    this.#buffer = '';
    this.#dataLines = [];
    this.#eventName = '';
    this.#retry = undefined;
    this.#hasFields = false;
  }

  #handleLine(line: string): SseMessage | null {
    if (line === '') {
      // Dispatch on the blank line that terminates an event block. A block of
      // pure comments carries no fields and must not be dispatched.
      if (!this.#hasFields) return null;
      const message: SseMessage = {
        event: this.#eventName || 'message',
        data: this.#dataLines.join('\n'),
      };
      if (this.#lastEventId !== undefined) message.id = this.#lastEventId;
      if (this.#retry !== undefined) message.retry = this.#retry;
      this.#dataLines = [];
      this.#eventName = '';
      this.#hasFields = false;
      return message;
    }

    if (line.startsWith(':')) return null; // comment / keep-alive ping

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    switch (field) {
      case 'event':
        this.#eventName = value;
        this.#hasFields = true;
        break;
      case 'data':
        this.#dataLines.push(value);
        this.#hasFields = true;
        break;
      case 'id':
        // The spec ignores ids containing NUL.
        if (!value.includes(String.fromCharCode(0))) this.#lastEventId = value;
        this.#hasFields = true;
        break;
      case 'retry': {
        const parsed = Number.parseInt(value, 10);
        if (Number.isFinite(parsed) && parsed >= 0) this.#retry = parsed;
        this.#hasFields = true;
        break;
      }
      default:
        // Unknown fields are ignored, but they still make the block non-empty.
        this.#hasFields = true;
        break;
    }
    return null;
  }
}

/** Parse one buffered SSE payload (helper for tests and file-driven input). */
export function parseSse(text: string): SseMessage[] {
  return new SseParser().push(text);
}