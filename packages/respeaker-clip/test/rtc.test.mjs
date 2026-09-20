import test from 'node:test';
import assert from 'node:assert/strict';

import { RtcSessionController, describeRtcPhase } from '../dist/esm/rtc.js';
import { makeEvent } from '../dist/esm/client.js';

function fakeClient({ resume, pause } = {}) {
  return {
    calls: [],
    async streamResume() {
      this.calls.push('resume');
      return resume ?? { accepted: true, phase: 'capturing', utterance_id: 5 };
    },
    async streamPause() {
      this.calls.push('pause');
      return pause ?? { accepted: true, phase: 'finalizing', utterance_id: 5 };
    },
  };
}

function controller(options = {}, clientOptions = {}) {
  const client = fakeClient(clientOptions);
  return { client, rtc: new RtcSessionController(client, options) };
}

test('a capturing state opens a new logical utterance', () => {
  const started = [];
  const { rtc } = controller({ onUtteranceStart: (utterance) => started.push(utterance) });

  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 7, trigger: 'device' }));

  assert.equal(rtc.snapshot.phase, 'capturing');
  assert.equal(rtc.snapshot.utteranceId, 7);
  assert.equal(started.length, 1);
  assert.equal(started[0].id, 7);
  assert.equal(started[0].trigger, 'device');
  assert.equal(rtc.current?.id, 7);
});

test('partial transcripts update, the final one is authoritative', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 1 }));

  rtc.applyEvent(makeEvent('transcript', { utterance_id: 1, text: 'what is', final: false }));
  assert.equal(rtc.current?.text, 'what is');
  assert.equal(rtc.current?.final, false);

  rtc.applyEvent(
    makeEvent('transcript', { utterance_id: 1, text: 'What is the weather?', final: true }),
  );
  assert.equal(rtc.current?.text, 'What is the weather?');
  assert.equal(rtc.current?.final, true);
  assert.equal(rtc.current?.skipped, false);
});

test('a skipped final clears the partial text', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 2 }));
  rtc.applyEvent(makeEvent('transcript', { utterance_id: 2, text: 'noise', final: false }));

  rtc.applyEvent(makeEvent('transcript', { utterance_id: 2, text: '', final: true, skipped: true }));

  assert.equal(rtc.current?.text, '');
  assert.equal(rtc.current?.skipped, true);
});

test('thinking and token events accumulate the streamed reply', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 3 }));
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'finalizing', utterance_id: 3 }));

  rtc.applyEvent(makeEvent('thinking', { tool: '' }));
  assert.equal(rtc.snapshot.processing, true);
  assert.equal(rtc.snapshot.streaming, true);

  rtc.applyEvent(makeEvent('thinking', { tool: 'web_search' }));
  assert.equal(rtc.snapshot.tool, 'web_search');

  rtc.applyEvent(makeEvent('token', { text: 'It is ' }));
  rtc.applyEvent(makeEvent('token', { text: 'sunny.' }));
  assert.equal(rtc.snapshot.streamingText, 'It is sunny.');
});

test('a result completes the utterance and releases the controller', () => {
  const results = [];
  const { rtc } = controller({ onUtteranceResult: (utterance) => results.push(utterance) });
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 4 }));
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'finalizing', utterance_id: 4 }));
  rtc.applyEvent(makeEvent('token', { text: 'partial answer' }));

  rtc.applyEvent(
    makeEvent('result', {
      utterance_id: 4,
      transcript: 'What is the weather?',
      response: 'It is sunny.',
      conversation_id: 'conv-1',
      trigger: 'rtc',
    }),
  );

  const utterance = rtc.utterances.find((item) => item.id === 4);
  assert.equal(utterance?.answer, 'It is sunny.');
  assert.equal(utterance?.text, 'What is the weather?');
  assert.equal(utterance?.conversationId, 'conv-1');
  assert.equal(utterance?.final, true);
  assert.equal(results.length, 1);
  assert.equal(rtc.snapshot.processing, false);
  assert.equal(rtc.snapshot.streaming, false);
  assert.equal(rtc.snapshot.streamingText, '');
  assert.equal(rtc.current, null);
  assert.equal(rtc.lastResult?.response, 'It is sunny.');
});

test('a new utterance resets the reply stream of the previous one', () => {
  const started = [];
  const { rtc } = controller({ onUtteranceStart: (utterance) => started.push(utterance.id) });
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 10 }));
  rtc.applyEvent(makeEvent('token', { text: 'answer for 10' }));

  // The next utterance may start while the previous LLM pass is still running.
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 11 }));

  assert.deepEqual(started, [10, 11]);
  assert.equal(rtc.snapshot.utteranceId, 11);
  assert.equal(rtc.snapshot.streamingText, '');
  assert.equal(rtc.current?.id, 11);
  // Replaying the same capturing event must not open a second utterance.
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 11 }));
  assert.deepEqual(started, [10, 11]);
});

test('reports every utterance seen so far', () => {
  const { rtc } = controller();
  for (const id of [3, 1, 2]) {
    rtc.applyEvent(makeEvent('transcript', { utterance_id: id, text: `text ${id}`, final: true }));
  }
  assert.deepEqual(
    rtc.utterances.map((utterance) => utterance.id),
    [1, 2, 3],
  );
});

test('a disconnect clears the armed state', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 1 }));

  rtc.applyEvent(makeEvent('connection', { connected: false, error: 'ble lost' }));

  assert.equal(rtc.snapshot.phase, 'disconnected');
  assert.equal(rtc.snapshot.session, null);
  assert.equal(rtc.snapshot.error, 'ble lost');
  assert.equal(rtc.isArmed, false);
});

test('resume and pause drive the service and follow its answer', async () => {
  const { rtc, client } = controller({}, {
    resume: { accepted: true, phase: 'capturing', utterance_id: 5, session: '20260920101234' },
    pause: { accepted: true, phase: 'finalizing', utterance_id: 5, session: '20260920101234' },
  });

  await rtc.resume();
  assert.deepEqual(client.calls, ['resume']);
  assert.equal(rtc.snapshot.phase, 'capturing');
  assert.equal(rtc.snapshot.utteranceId, 5);
  assert.equal(rtc.snapshot.session, '20260920101234');
  assert.equal(rtc.isArmed, true);

  await rtc.pause();
  assert.deepEqual(client.calls, ['resume', 'pause']);
  assert.equal(rtc.snapshot.phase, 'finalizing');
  assert.equal(rtc.snapshot.processing, true);
});

test('syncStatus rehydrates the controller after a reconnect', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 9 }));

  rtc.syncStatus({
    connected: true,
    rtc_phase: 'paused',
    rtc_session: '20260920101111',
    rtc_utterance_id: 12,
    rtc_processing: true,
  });

  assert.equal(rtc.snapshot.phase, 'paused');
  assert.equal(rtc.snapshot.session, '20260920101111');
  assert.equal(rtc.snapshot.utteranceId, 12);
  assert.equal(rtc.snapshot.processing, true);

  rtc.syncStatus({ connected: false });
  assert.equal(rtc.snapshot.phase, 'disconnected');
});

test('describeRtcPhase renders actionable status lines', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 4 }));
  assert.equal(describeRtcPhase(rtc.snapshot), 'Listening — utterance 4');

  rtc.applyEvent(makeEvent('rtc_state', { phase: 'paused' }));
  assert.match(describeRtcPhase(rtc.snapshot), /warm pause/);

  rtc.applyEvent(makeEvent('rtc_state', { phase: 'paused', error: 'arm failed' }));
  assert.equal(describeRtcPhase(rtc.snapshot), 'RTC error: arm failed');
});

test('device-gateway mode completes an utterance as audio', () => {
  const results = [];
  const { rtc } = controller({ onUtteranceResult: (utterance) => results.push(utterance) });
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 21 }));
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'finalizing', utterance_id: 21 }));

  rtc.applyEvent(
    makeEvent('utterance_audio', {
      utterance_id: 21,
      session: '20260920101234',
      url: '/api/clip/utterances/20260920101234/21/audio',
      bytes: 8192,
      content_type: 'audio/ogg',
      trigger: 'device',
    }),
  );

  const utterance = rtc.utterances.find((item) => item.id === 21);
  assert.equal(utterance?.audioUrl, '/api/clip/utterances/20260920101234/21/audio');
  assert.equal(utterance?.final, true);
  assert.equal(utterance?.skipped, false);
  assert.equal(utterance?.answer, undefined); // no LLM ran
  assert.equal(results.length, 1);
  assert.equal(rtc.snapshot.processing, false);
  assert.equal(rtc.snapshot.streaming, false);
  assert.equal(rtc.current, null);
});

test('a skipped device-gateway utterance yields no audio URL', () => {
  const { rtc } = controller();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 22 }));

  rtc.applyEvent(
    makeEvent('utterance_audio', {
      utterance_id: 22,
      session: '20260920101234',
      skipped: 'too short',
    }),
  );

  const utterance = rtc.utterances.find((item) => item.id === 22);
  assert.equal(utterance?.skipped, true);
  assert.equal(utterance?.audioUrl, undefined);
  assert.equal(rtc.snapshot.processing, false);
});

test('subscribers receive every snapshot', () => {
  const { rtc } = controller();
  const phases = [];
  const unsubscribe = rtc.subscribe((snapshot) => phases.push(snapshot.phase));

  rtc.applyEvent(makeEvent('rtc_state', { phase: 'capturing', utterance_id: 1 }));
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'paused' }));
  unsubscribe();
  rtc.applyEvent(makeEvent('rtc_state', { phase: 'stopped' }));

  assert.deepEqual(phases, ['capturing', 'paused']);
});