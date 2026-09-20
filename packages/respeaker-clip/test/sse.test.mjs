import test from 'node:test';
import assert from 'node:assert/strict';

import { SseParser, parseSse } from '../dist/esm/sse.js';

test('parses a named event with id and data', () => {
  const messages = parseSse('id: 7\nevent: rtc_state\ndata: {"phase":"paused"}\n\n');
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0], {
    id: '7',
    event: 'rtc_state',
    data: '{"phase":"paused"}',
  });
});

test('joins multi-line data with newlines', () => {
  const [message] = parseSse('event: result\ndata: line one\ndata: line two\n\n');
  assert.equal(message.data, 'line one\nline two');
});

test('ignores comment keep-alive blocks', () => {
  assert.deepEqual(parseSse(': ping\n\n'), []);
  assert.deepEqual(parseSse(': keep-alive\n'), []);
});

test('handles CRLF line endings', () => {
  const messages = parseSse('id: 3\r\nevent: token\r\ndata: {"text":"hi"}\r\n\r\n');
  assert.equal(messages.length, 1);
  assert.equal(messages[0].event, 'token');
  assert.equal(messages[0].id, '3');
});

test('defaults the event name to "message"', () => {
  const [message] = parseSse('data: hello\n\n');
  assert.equal(message.event, 'message');
  assert.equal(message.data, 'hello');
});

test('strips exactly one space after the colon', () => {
  const [message] = parseSse('data:  two spaces\n\n');
  assert.equal(message.data, ' two spaces');
});

test('parses retry and drops ids containing NUL', () => {
  const nul = String.fromCharCode(0);
  const messages = parseSse(`retry: 2500\nid: bad${nul}id\ndata: x\n\n`);
  assert.equal(messages[0].retry, 2500);
  assert.equal(messages[0].id, undefined);
});

test('accepts a message split across chunks', () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push('event: transcript\nda'), []);
  const messages = parser.push('ta: {"utterance_id":1,"text":"hi","final":false}\n\n');
  assert.equal(messages.length, 1);
  assert.equal(messages[0].event, 'transcript');
  assert.equal(messages[0].data, '{"utterance_id":1,"text":"hi","final":false}');
});

test('retains the last event id across pushed messages', () => {
  const parser = new SseParser();
  parser.push('id: 41\nevent: token\ndata: {"text":"a"}\n\n');
  assert.equal(parser.lastEventId, '41');
  const [next] = parser.push('event: token\ndata: {"text":"b"}\n\n');
  assert.equal(next.id, '41');
});

test('reset clears buffered state', () => {
  const parser = new SseParser();
  parser.push('event: token\ndata: partial');
  parser.reset();
  assert.deepEqual(parser.push('\n\n'), []);
});

test('parses the documented Clip event stream', () => {
  const stream = [
    ': ping\n\n',
    'id: 1\nevent: connection\ndata: {"connected": true, "status": {"device_name": "Clip"}}\n\n',
    ': ping\n\n',
    'id: 2\nevent: rtc_state\ndata: {"phase": "capturing", "utterance_id": 4, "trigger": "device"}\n\n',
    'id: 3\nevent: transcript\ndata: {"utterance_id": 4, "text": "what is", "final": false}\n\n',
    'id: 4\nevent: transcript\ndata: {"utterance_id": 4, "text": "What is the weather?", "final": true}\n\n',
    'id: 5\nevent: thinking\ndata: {"tool": ""}\n\n',
    'id: 6\nevent: token\ndata: {"text": "It is "}\n\n',
    'id: 7\nevent: token\ndata: {"text": "sunny."}\n\n',
    'id: 8\nevent: result\ndata: {"utterance_id": 4, "transcript": "What is the weather?", "response": "It is sunny.", "trigger": "rtc"}\n\n',
  ].join('');

  const messages = parseSse(stream);
  assert.deepEqual(
    messages.map((message) => message.event),
    ['connection', 'rtc_state', 'transcript', 'transcript', 'thinking', 'token', 'token', 'result'],
  );
  assert.equal(JSON.parse(messages[0].data).status.device_name, 'Clip');
  assert.equal(JSON.parse(messages[7].data).utterance_id, 4);
  assert.equal(messages[7].id, '8');
});