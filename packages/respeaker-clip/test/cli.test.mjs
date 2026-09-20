import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

import { main } from '../dist/esm/cli.js';
import { VERSION } from '../dist/esm/version.js';

const repoRoot = fileURLToPath(new URL('../../..', import.meta.url));

function collect() {
  const out = [];
  const err = [];
  return {
    io: { out: (message) => out.push(message), err: (message) => err.push(message) },
    out,
    err,
  };
}

test('--version prints the package version', async () => {
  const { io, out } = collect();
  assert.equal(await main(['--version'], io), 0);
  assert.deepEqual(out, [VERSION]);
});

test('no arguments prints usage and fails', async () => {
  const { io, err } = collect();
  assert.equal(await main([], io), 2);
  assert.match(err.join('\n'), /Usage:/);
});

test('--help prints usage and succeeds', async () => {
  const { io, out } = collect();
  assert.equal(await main(['--help'], io), 0);
  assert.match(out.join('\n'), /respeaker-clip serve/);
});

test('an unknown command is rejected', async () => {
  const { io, err } = collect();
  assert.equal(await main(['sing'], io), 2);
  assert.match(err.join('\n'), /unknown command: sing/);
});

test('serve --dry-run prints the resolved plan', async () => {
  const { io, out } = collect();
  assert.equal(await main(['serve', '--dry-run'], io), 0);
  const text = out.join('\n');
  assert.match(text, /mode:    package/);
  assert.match(text, /install: .*pip install respeaker-clip-service/);
  assert.match(text, /run:     .*backend\.service_cli --host 0\.0\.0\.0 --port 5000/);
});

test('serve --dry-run --json is machine readable', async () => {
  const { io, out } = collect();
  assert.equal(await main(['serve', '--dry-run', '--json', '--port', '7000'], io), 0);

  const plan = JSON.parse(out.join('\n'));
  assert.equal(plan.mode, 'package');
  assert.deepEqual(plan.run.args.slice(-2), ['--port', '7000']);
});

test('serve --source uses the checkout instead of pip', async () => {
  const { io, out, err } = collect();
  assert.equal(await main(['serve', '--dry-run', '--source', repoRoot], io), 0);

  const text = out.join('\n');
  assert.match(text, /mode:    source/);
  assert.match(text, /install: .*pip install -r requirements\.txt/);
  assert.deepEqual(err, []);
});

test('serve forwards extra arguments after --', async () => {
  const { io, out } = collect();
  assert.equal(
    await main(
      ['serve', '--dry-run', '--json', '--input-mode', 'both', '--', '--extra-flag', 'value'],
      io,
    ),
    0,
  );

  const plan = JSON.parse(out.join('\n'));
  assert.deepEqual(plan.run.args.slice(-4), ['--input-mode', 'both', '--extra-flag', 'value']);
});

test('serve rejects an invalid port', async () => {
  const { io, err } = collect();
  assert.equal(await main(['serve', '--port', 'abc'], io), 1);
  assert.match(err.join('\n'), /invalid --port/);
});

test('serve rejects an invalid input mode', async () => {
  const { io, err } = collect();
  assert.equal(await main(['serve', '--input-mode', 'mic'], io), 1);
  assert.match(err.join('\n'), /invalid --input-mode/);
});

test('serve rejects unknown flags', async () => {
  const { io, err } = collect();
  assert.equal(await main(['serve', '--nope'], io), 2);
  assert.match(err.join('\n'), /nope/);
});

test('doctor renders check lines', async () => {
  const { io, out } = collect();
  const code = await main(['doctor'], io);
  const text = out.join('\n');

  assert.match(text, /python\s+.*Python 3\./);
  assert.ok(code === 0 || code === 1);
});