import test from 'node:test';
import assert from 'node:assert/strict';

import { ClipClient } from '../dist/esm/client.js';
import { ClipApiError } from '../dist/esm/errors.js';

const encoder = new TextEncoder();

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  };
}

function sseResponse(text, { status = 200, chunks } = {}) {
  const parts = chunks ?? [text];
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
    body: new ReadableStream({
      start(controller) {
        for (const part of parts) controller.enqueue(encoder.encode(part));
        controller.close();
      },
    }),
  };
}

function stubFetch(handler) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init, calls.length);
  };
  fetch.calls = calls;
  return fetch;
}

test('getStatus builds the URL and returns the payload', async () => {
  const fetch = stubFetch(() => jsonResponse({ connected: true, rtc_phase: 'paused' }));
  const client = new ClipClient({ baseUrl: 'http://clip.local:5000/', fetch });

  const status = await client.getStatus();

  assert.equal(status.rtc_phase, 'paused');
  assert.equal(fetch.calls.length, 1);
  assert.equal(fetch.calls[0].url, 'http://clip.local:5000/api/clip/status');
  assert.equal(fetch.calls[0].init.method, 'GET');
});

test('startRecording posts only the fields that were provided', async () => {
  const fetch = stubFetch(() => jsonResponse({ conversation_id: 'c1' }));
  const client = new ClipClient({ fetch });

  await client.startRecording({ conversationId: 'c1' });
  const body = JSON.parse(fetch.calls[0].init.body);
  assert.deepEqual(body, { conversation_id: 'c1' });

  await client.startRecording({ mode: 'enhanced' });
  assert.deepEqual(JSON.parse(fetch.calls[1].init.body), { mode: 'enhanced' });
});

test('RTC resume and pause hit the stream endpoints', async () => {
  const fetch = stubFetch(() => jsonResponse({ accepted: true }));
  const client = new ClipClient({ fetch });

  await client.streamResume();
  await client.streamPause();

  assert.deepEqual(
    fetch.calls.map((call) => call.url),
    ['http://127.0.0.1:5000/api/clip/stream/resume', 'http://127.0.0.1:5000/api/clip/stream/pause'],
  );
});

test('ingest validates the session id without touching the network', async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  const client = new ClipClient({ fetch });

  await assert.rejects(() => client.ingest('abc'), (error) => {
    assert.ok(error instanceof ClipApiError);
    assert.equal(error.code, 'bad_input');
    return true;
  });
  assert.equal(fetch.calls.length, 0);

  await client.ingest('20260920101234', { trigger: 'physical', conversationId: 'c9' });
  assert.equal(fetch.calls[0].url, 'http://127.0.0.1:5000/api/clip/sessions/20260920101234/ingest');
  assert.deepEqual(JSON.parse(fetch.calls[0].init.body), {
    trigger: 'physical',
    conversation_id: 'c9',
  });
});

test('maps service error statuses to codes', async () => {
  const cases = [
    [400, 'bad_input', false],
    [409, 'conflict', false],
    [502, 'command_failed', false],
    [503, 'unavailable', true],
    [500, 'http', false],
  ];
  for (const [status, code, isOffline] of cases) {
    const fetch = stubFetch(() => jsonResponse({ error: 'boom' }, status));
    const client = new ClipClient({ fetch });
    await assert.rejects(() => client.stopRecording(), (error) => {
      assert.equal(error.code, code);
      assert.equal(error.status, status);
      assert.equal(error.message, 'boom');
      assert.equal(error.isOffline, isOffline);
      return true;
    });
  }
});

test('reports transport failures as network errors', async () => {
  const fetch = stubFetch(() => {
    throw new Error('ECONNREFUSED');
  });
  const client = new ClipClient({ fetch });

  await assert.rejects(() => client.getStatus(), (error) => {
    assert.equal(error.code, 'network');
    assert.match(error.message, /cannot reach the Clip service/);
    return true;
  });
});

test('times out slow requests', async () => {
  const fetch = () =>
    new Promise((_resolve, reject) => {
      // Never settles; the client's own timeout must abort it.
      setTimeout(() => reject(new Error('late')), 5_000);
    });
  const client = new ClipClient({ fetch, timeoutMs: 20 });

  await assert.rejects(() => client.getStatus(), (error) => {
    assert.equal(error.code, 'timeout');
    return true;
  });
});

test('honours a caller abort signal', async () => {
  const controller = new AbortController();
  const fetch = (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
  const client = new ClipClient({ fetch, timeoutMs: 0 });

  const pending = client.getStatus(controller.signal);
  controller.abort();

  await assert.rejects(() => pending, (error) => {
    assert.equal(error.code, 'aborted');
    return true;
  });
});

test('subscribe dispatches every named event', async () => {
  const stream = [
    'id: 1\nevent: connection\ndata: {"connected": true}\n\n',
    'id: 2\nevent: recording\ndata: {"action": "started", "session": "s1", "trigger": "web"}\n\n',
    'id: 3\nevent: workflow\ndata: {"status": "downloading", "session": "s1"}\n\n',
    'id: 4\nevent: rtc_state\ndata: {"phase": "capturing", "utterance_id": 2}\n\n',
    'id: 5\nevent: rtc_device\ndata: {"status": "STREAMING"}\n\n',
    'id: 6\nevent: transcript\ndata: {"utterance_id": 2, "text": "hi", "final": false}\n\n',
    'id: 7\nevent: thinking\ndata: {"tool": "web_search"}\n\n',
    'id: 8\nevent: token\ndata: {"text": "hel"}\n\n',
    'id: 9\nevent: result\ndata: {"utterance_id": 2, "response": "hello"}\n\n',
  ].join('');
  const fetch = stubFetch(() => sseResponse(stream));
  const client = new ClipClient({ fetch });

  const seen = [];
  const done = new Promise((resolve) => {
    const sub = client.subscribe(
      {
        onEvent: (event) => {
          seen.push(event.event);
          if (event.event === 'result') {
            sub.close();
            resolve();
          }
        },
      },
      { reconnect: false },
    );
  });

  await done;
  assert.deepEqual(seen, [
    'connection',
    'recording',
    'workflow',
    'rtc_state',
    'rtc_device',
    'transcript',
    'thinking',
    'token',
    'result',
  ]);
});

test('subscribe reconnects with Last-Event-ID', async () => {
  const first = 'id: 12\nevent: token\ndata: {"text":"a"}\n\n';
  const second = 'id: 13\nevent: result\ndata: {"response":"done"}\n\n';
  const fetch = stubFetch((_url, init, call) =>
    sseResponse(call === 1 ? first : second),
  );
  const client = new ClipClient({ fetch });

  const tokens = [];
  let opened = 0;
  let subscription;
  const finished = new Promise((resolve) => {
    subscription = client.subscribe(
      {
        onToken: (event) => tokens.push(event.text),
        onOpen: () => {
          opened += 1;
        },
        onResult: () => {
          subscription.close();
          resolve();
        },
      },
      { minReconnectDelayMs: 5, maxReconnectDelayMs: 10 },
    );
  });

  await finished;

  assert.equal(opened, 2);
  assert.deepEqual(tokens, ['a']);
  assert.equal(fetch.calls[0].init.headers['Last-Event-ID'], undefined);
  assert.equal(fetch.calls[1].init.headers['Last-Event-ID'], '12');
});

test('subscribe reports a refused stream without retrying when reconnect is off', async () => {
  const fetch = stubFetch(() => jsonResponse({ error: 'clip disabled' }, 503));
  const client = new ClipClient({ fetch });

  const errors = [];
  const states = [];
  await new Promise((resolve) => {
    client.subscribe(
      {
        onError: (error) => {
          errors.push(error);
          resolve();
        },
      },
      {
        reconnect: false,
        onStateChange: (state) => states.push(state),
      },
    );
  });

  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, 'unavailable');
  assert.ok(states.includes('closed'));
});

test('subscribe isolates handler exceptions', async () => {
  const fetch = stubFetch(() => sseResponse('event: token\ndata: {"text":"x"}\n\n'));
  const client = new ClipClient({ fetch });

  const errors = [];
  await new Promise((resolve) => {
    const sub = client.subscribe(
      {
        onToken: () => {
          throw new Error('handler exploded');
        },
        onError: (error) => {
          errors.push(error);
          sub.close();
          resolve();
        },
      },
      { reconnect: false },
    );
  });

  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /handler exploded/);
});