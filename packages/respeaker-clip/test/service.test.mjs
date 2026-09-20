import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';

import {
  DEFAULT_PIP_SPEC,
  describePlan,
  ensureEnvironment,
  resolveServicePlan,
  runDoctor,
  runService,
  serviceArgs,
  venvPythonPath,
} from '../dist/esm/service.js';

function fakeChild() {
  const child = new EventEmitter();
  child.killed = false;
  child.kill = () => {
    child.killed = true;
  };
  return child;
}

function fakeDeps({
  env = {},
  platform = 'linux',
  exists = () => false,
  python = 'Python 3.12.3',
  bluetoothctl = '/usr/bin/bluetoothctl',
  child = fakeChild(),
} = {}) {
  const calls = { exec: [], spawn: [], logs: [] };
  const deps = {
    env,
    platform,
    exists,
    homedir: () => '/home/tester',
    cwd: () => '/work',
    log: (message) => calls.logs.push(message),
    exec: (file, args, options) => calls.exec.push({ file, args, options }),
    spawn: (file, args, options) => {
      calls.spawn.push({ file, args, options });
      return child;
    },
    execCapture: (file, args) => {
      if (file === 'python3' || file === 'python') {
        if (python === null) throw new Error('not found');
        return python;
      }
      if (file === 'which' || file === 'where') {
        if (args[0] === 'bluetoothctl' && bluetoothctl) return `${bluetoothctl}\n`;
        throw new Error('not found');
      }
      throw new Error(`unexpected probe: ${file}`);
    },
  };
  return { deps, calls };
}

test('serviceArgs maps options to service flags', () => {
  assert.deepEqual(serviceArgs(), ['--host', '0.0.0.0', '--port', '5000']);
  assert.deepEqual(
    serviceArgs({
      host: '127.0.0.1',
      port: 8080,
      inputMode: 'both',
      bleName: 'Clip',
      bleAddress: 'AA:BB:CC:DD:EE:FF',
      extraArgs: ['--log-level', 'DEBUG'],
    }),
    [
      '--host',
      '127.0.0.1',
      '--port',
      '8080',
      '--input-mode',
      'both',
      '--ble-address',
      'AA:BB:CC:DD:EE:FF',
      '--ble-name',
      'Clip',
      '--log-level',
      'DEBUG',
    ],
  );
  assert.deepEqual(serviceArgs({ noClip: true }), [
    '--host',
    '0.0.0.0',
    '--port',
    '5000',
    '--no-clip',
  ]);
});

test('venvPythonPath follows the platform layout', () => {
  assert.equal(venvPythonPath('/v', 'linux'), '/v/bin/python');
  assert.equal(venvPythonPath('C:\\v', 'win32'), 'C:\\v\\Scripts\\python.exe');
});

test('package mode installs the published service into a managed venv', () => {
  const { deps } = fakeDeps();
  const plan = resolveServicePlan({}, deps);

  assert.equal(plan.mode, 'package');
  assert.equal(plan.sourceDir, null);
  assert.equal(plan.venvDir, '/home/tester/.cache/respeaker-clip/venv');
  assert.equal(plan.venvPython, '/home/tester/.cache/respeaker-clip/venv/bin/python');
  assert.deepEqual(plan.installs, [
    {
      command: plan.venvPython,
      args: ['-m', 'pip', 'install', DEFAULT_PIP_SPEC],
      cwd: '/work',
    },
  ]);
  assert.deepEqual(plan.run, {
    command: plan.venvPython,
    args: ['-m', 'backend.service_cli', '--host', '0.0.0.0', '--port', '5000'],
    cwd: '/work',
  });
});

test('environment variables override the default locations and spec', () => {
  const { deps } = fakeDeps({
    env: {
      RESPEAKER_CLIP_HOME: '/opt/clip',
      RESPEAKER_CLIP_PIP_SPEC: 'respeaker-clip-service==0.2.0',
      RESPEAKER_CLIP_PYTHON: '/usr/bin/python3.12',
    },
  });
  const plan = resolveServicePlan({}, deps);

  assert.equal(plan.venvDir, '/opt/clip/venv');
  assert.equal(plan.hostPython, '/usr/bin/python3.12');
  assert.deepEqual(plan.installs[0].args, [
    '-m',
    'pip',
    'install',
    'respeaker-clip-service==0.2.0',
  ]);
});

test('source mode runs a checkout and installs its requirements', () => {
  const { deps } = fakeDeps({
    env: { RESPEAKER_CLIP_SERVICE_ROOT: '/src/agent' },
    exists: (path) => ['/src/agent/app.py', '/src/agent/backend'].includes(path),
  });
  const plan = resolveServicePlan({}, deps);

  assert.equal(plan.mode, 'source');
  assert.equal(plan.sourceDir, '/src/agent');
  assert.deepEqual(plan.installs, [
    {
      command: plan.venvPython,
      args: ['-m', 'pip', 'install', '-r', 'requirements.txt'],
      cwd: '/src/agent',
    },
  ]);
  assert.equal(plan.run.cwd, '/src/agent');
  assert.deepEqual(plan.warnings, []);
});

test('a --source that is not a checkout warns but stays usable', () => {
  const { deps } = fakeDeps();
  const plan = resolveServicePlan({ source: '/tmp/empty' }, deps);

  assert.equal(plan.mode, 'source');
  assert.equal(plan.warnings.length, 1);
  assert.match(plan.warnings[0], /does not look like a reSpeaker Clip AI Agent checkout/);
});

test('--skip-install keeps the venv but drops the pip step', () => {
  const { deps } = fakeDeps();
  const plan = resolveServicePlan({ skipInstall: true }, deps);
  assert.deepEqual(plan.installs, []);
});

test('describePlan summarises the plan', () => {
  const { deps } = fakeDeps();
  const text = describePlan(resolveServicePlan({ port: 7000, source: '/src' }, deps));
  assert.match(text, /mode:    source \(/);
  assert.match(text, /python:  .*\/venv\/bin\/python/);
  assert.match(text, /run:     .*backend\.service_cli --host 0\.0\.0\.0 --port 7000/);
});

test('ensureEnvironment creates the venv once and then installs', () => {
  const created = {};
  const { deps, calls } = fakeDeps({
    exists: (path) => Boolean(created[path]),
  });
  deps.exec = (file, args, options) => {
    calls.exec.push({ file, args, options });
    if (args[0] === '-m' && args[1] === 'venv') {
      // `python -m venv <dir>` produces the interpreter the plan looks for.
      created[venvPythonPath(args[2])] = true;
      return;
    }
    if (file === 'boom') throw new Error('pip failed');
  };

  const plan = resolveServicePlan({}, deps);
  ensureEnvironment(plan, deps);

  assert.equal(calls.exec.length, 2);
  assert.deepEqual(calls.exec[0], {
    file: 'python3',
    args: ['-m', 'venv', plan.venvDir],
    options: { cwd: '/work' },
  });
  assert.deepEqual(calls.exec[1].args, ['-m', 'pip', 'install', DEFAULT_PIP_SPEC]);

  // Second run: the venv already exists, so only the install runs.
  calls.exec.length = 0;
  ensureEnvironment(plan, deps);
  assert.equal(calls.exec.length, 1);
});

test('ensureEnvironment surfaces a failing install', () => {
  const { deps } = fakeDeps({ exists: () => true });
  deps.exec = () => {
    throw new Error('pip failed');
  };
  const plan = resolveServicePlan({}, deps);
  assert.throws(() => ensureEnvironment(plan, deps), /pip failed/);
});

test('runService resolves with the child exit code', async () => {
  const child = fakeChild();
  const { deps, calls } = fakeDeps({ child, env: { GROQ_API_KEY: 'k' } });
  const plan = resolveServicePlan({}, deps);

  const code = runService(plan, { env: { GROQ_API_KEY: 'k' } }, deps);
  child.emit('exit', 7, null);

  assert.equal(await code, 7);
  assert.equal(calls.spawn.length, 1);
  assert.equal(calls.spawn[0].file, plan.venvPython);
  assert.deepEqual(calls.spawn[0].options.env, { GROQ_API_KEY: 'k' });
});

test('doctor reports a healthy environment', () => {
  const { deps } = fakeDeps({ exists: () => true });
  const report = runDoctor({ source: '/src/agent' }, deps);

  assert.equal(report.ok, true);
  const names = report.checks.map((check) => check.name);
  assert.deepEqual(names, ['node', 'python', 'virtualenv', 'mode', 'bluez']);
  assert.match(report.checks.find((check) => check.name === 'python').detail, /3\.12/);
  assert.match(report.checks.find((check) => check.name === 'bluez').detail, /bluetoothctl found/);
});

test('doctor fails when python is missing or too old', () => {
  const missing = runDoctor({}, fakeDeps({ python: null }).deps);
  assert.equal(missing.ok, false);
  assert.match(missing.checks.find((c) => c.name === 'python').detail, /not found on PATH/);

  const old = runDoctor({}, fakeDeps({ python: 'Python 3.8.10' }).deps);
  assert.equal(old.ok, false);
  assert.match(old.checks.find((c) => c.name === 'python').detail, /need >= 3\.10/);
});

test('doctor treats a missing bluetoothctl as informational', () => {
  const { deps } = fakeDeps({ bluetoothctl: null });
  const report = runDoctor({}, deps);

  assert.equal(report.ok, true);
  const bluez = report.checks.find((check) => check.name === 'bluez');
  assert.equal(bluez.ok, false);
  assert.equal(bluez.required, false);
  assert.match(bluez.detail, /HTTP API still runs/);
});