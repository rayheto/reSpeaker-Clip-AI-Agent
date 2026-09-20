/**
 * Node-only helpers that bootstrap and run the Python Clip service.
 *
 * The Clip service itself is Python (BLE runtime + Flask `/api/clip` API).
 * This module lets a Node caller — and the `respeaker-clip` CLI — create a
 * dedicated virtualenv, install the service into it, and launch it without
 * cloning the repository by hand.
 *
 * Two modes:
 *  - `source`  — run from a local checkout (`--source`, `RESPEAKER_CLIP_SERVICE_ROOT`),
 *                installing `requirements.txt` into the venv.
 *  - `package` — install the published Python package (`respeaker-clip-service`,
 *                or any pip requirement spec via `--pip-spec`) into the venv.
 */

import { spawn as nodeSpawn, execFileSync, type ChildProcess } from 'node:child_process';
import { existsSync as fsExistsSync } from 'node:fs';
import { homedir as osHomedir } from 'node:os';
import { join, resolve, win32 as win32Path } from 'node:path';

export type ServiceMode = 'source' | 'package';
export type VoiceInputMode = 'browser' | 'clip' | 'both';

export interface ServiceOptions {
  /** Bind address. Default `0.0.0.0`. */
  host?: string;
  /** TCP port. Default `5000`. */
  port?: number;
  /** Which voice input the service serves. Default `clip`. */
  inputMode?: VoiceInputMode;
  /** Pin the Clip by BLE address. */
  bleAddress?: string;
  /** Scan for this BLE name instead of a fixed address. */
  bleName?: string;
  /** Run the HTTP API without the BLE runtime (browser voice only). */
  noClip?: boolean;
  /**
   * `.env` file the service should load. Defaults to `./.env` in the service
   * working directory; real environment variables always take precedence.
   */
  envFile?: string;
  /** Local checkout to run from. Defaults to `RESPEAKER_CLIP_SERVICE_ROOT`. */
  source?: string;
  /** Virtualenv directory. Defaults to `$RESPEAKER_CLIP_HOME/venv`. */
  venv?: string;
  /** pip requirement to install in package mode. Default `respeaker-clip-service`. */
  pipSpec?: string;
  /** Skip the pip install step and run whatever the venv already has. */
  skipInstall?: boolean;
  /** Extra arguments appended to the service command. */
  extraArgs?: string[];
  /** Environment for the child process. Defaults to `process.env`. */
  env?: NodeJS.ProcessEnv;
}

export interface ServiceCommand {
  command: string;
  args: string[];
  cwd: string;
}

export interface ServicePlan {
  mode: ServiceMode;
  /** Checkout root when running in source mode. */
  sourceDir: string | null;
  venvDir: string;
  /** Interpreter used to create the venv (host python). */
  hostPython: string;
  /** Interpreter inside the venv. */
  venvPython: string;
  /** Commands run to prepare the venv, in order. */
  installs: ServiceCommand[];
  run: ServiceCommand;
  warnings: string[];
}

export interface ServiceDeps {
  env: NodeJS.ProcessEnv;
  platform: NodeJS.Platform;
  exists: (path: string) => boolean;
  homedir: () => string;
  cwd: () => string;
  log: (message: string) => void;
  exec: (file: string, args: string[], options: { cwd?: string; env?: NodeJS.ProcessEnv }) => void;
  spawn: (file: string, args: string[], options: { cwd?: string; env?: NodeJS.ProcessEnv }) => ChildProcess;
  /** Run a probe command and return its stdout (used by `doctor`). */
  execCapture: (file: string, args: string[]) => string;
}

export const DEFAULT_PIP_SPEC = 'respeaker-clip-service';
export const DEFAULT_PORT = 5000;
export const DEFAULT_HOST = '0.0.0.0';
export const DEFAULT_VENV_DIRNAME = 'venv';
const MIN_PYTHON = [3, 10] as const;

/** Arguments handed to `python -m backend.service_cli`. */
export function serviceArgs(options: ServiceOptions = {}): string[] {
  const args = [
    '--host',
    options.host || DEFAULT_HOST,
    '--port',
    String(options.port ?? DEFAULT_PORT),
  ];
  if (options.inputMode) args.push('--input-mode', options.inputMode);
  if (options.bleAddress) args.push('--ble-address', options.bleAddress);
  if (options.bleName) args.push('--ble-name', options.bleName);
  if (options.noClip) args.push('--no-clip');
  if (options.envFile) args.push('--env-file', options.envFile);
  if (options.extraArgs?.length) args.push(...options.extraArgs);
  return args;
}

function pythonBinary(platform: NodeJS.Platform): string {
  return platform === 'win32' ? 'python' : 'python3';
}

/** Path of the interpreter inside a virtualenv. */
export function venvPythonPath(venvDir: string, platform: NodeJS.Platform = process.platform): string {
  // Join with the target platform's separator, so a plan resolved for Windows
  // (from a Linux CI, say) still renders a correct path.
  const joiner = platform === 'win32' ? win32Path.join : join;
  return platform === 'win32'
    ? joiner(venvDir, 'Scripts', 'python.exe')
    : joiner(venvDir, 'bin', 'python');
}

/** Managed-state directory: `$RESPEAKER_CLIP_HOME` or `~/.cache/respeaker-clip`. */
export function clipHome(options: ServiceOptions = {}, deps: ServiceDeps = defaultDeps()): string {
  const env = options.env ?? deps.env;
  const home = env.RESPEAKER_CLIP_HOME;
  if (home && home.trim()) return resolve(home);
  return join(deps.homedir(), '.cache', 'respeaker-clip');
}

export function defaultDeps(overrides: Partial<ServiceDeps> = {}): ServiceDeps {
  const base: ServiceDeps = {
    env: process.env,
    platform: process.platform,
    exists: (path) => fsExistsSync(path),
    homedir: () => osHomedir(),
    cwd: () => process.cwd(),
    log: (message) => console.error(message),
    exec: (file, args, options) => {
      execFileSync(file, args, { stdio: 'inherit', ...options });
    },
    spawn: (file, args, options) => nodeSpawn(file, args, { stdio: 'inherit', ...options }),
    execCapture: (file, args) =>
      execFileSync(file, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }),
  };
  return { ...base, ...overrides };
}

/**
 * Decide how the service will be installed and launched. Pure: callers pass
 * the filesystem/env accessors they want used, so this is unit-testable.
 */
export function resolveServicePlan(
  options: ServiceOptions = {},
  deps: ServiceDeps = defaultDeps(),
): ServicePlan {
  const env = options.env ?? deps.env;
  const warnings: string[] = [];
  const sourceDir = resolveOption(options.source ?? env.RESPEAKER_CLIP_SERVICE_ROOT, deps);

  const venvDir = resolveOption(options.venv, deps) ?? join(clipHome({ env }, deps), DEFAULT_VENV_DIRNAME);
  const hostPython = env.RESPEAKER_CLIP_PYTHON || pythonBinary(deps.platform);
  const venvPython = venvPythonPath(venvDir, deps.platform);

  const mode: ServiceMode = sourceDir ? 'source' : 'package';
  const installs: ServiceCommand[] = [];

  if (sourceDir) {
    if (!deps.exists(join(sourceDir, 'app.py')) || !deps.exists(join(sourceDir, 'backend'))) {
      warnings.push(
        `${sourceDir} does not look like a reSpeaker Clip AI Agent checkout (no app.py/backend)`,
      );
    }
    installs.push({
      command: venvPython,
      args: ['-m', 'pip', 'install', '-r', 'requirements.txt'],
      cwd: sourceDir,
    });
  } else {
    const spec = options.pipSpec || env.RESPEAKER_CLIP_PIP_SPEC || DEFAULT_PIP_SPEC;
    installs.push({
      command: venvPython,
      args: ['-m', 'pip', 'install', spec],
      cwd: deps.cwd(),
    });
  }

  const run: ServiceCommand = {
    command: venvPython,
    args: ['-m', 'backend.service_cli', ...serviceArgs(options)],
    cwd: sourceDir ?? deps.cwd(),
  };

  return {
    mode,
    sourceDir,
    venvDir,
    hostPython,
    venvPython,
    installs: options.skipInstall ? [] : installs,
    run,
    warnings,
  };
}

function resolveOption(value: string | undefined, deps: ServiceDeps): string | null {
  if (value === undefined) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  return resolve(deps.cwd(), trimmed);
}

/** One-line description of what the plan will do, for `--dry-run` and prompts. */
export function describePlan(plan: ServicePlan): string {
  const lines = [
    `mode:    ${plan.mode}${plan.sourceDir ? ` (${plan.sourceDir})` : ''}`,
    `venv:    ${plan.venvDir}`,
    `python:  ${plan.venvPython}`,
  ];
  for (const install of plan.installs) {
    lines.push(`install: ${install.command} ${install.args.join(' ')}`);
  }
  lines.push(`run:     ${plan.run.command} ${plan.run.args.join(' ')}`);
  return lines.join('\n');
}

/**
 * Create the venv (if missing) and run the plan's install commands.
 * Throws when the host interpreter or pip fails.
 */
export function ensureEnvironment(plan: ServicePlan, deps: ServiceDeps = defaultDeps()): void {
  if (!deps.exists(plan.venvPython)) {
    deps.log(`creating virtualenv ${plan.venvDir}`);
    deps.exec(plan.hostPython, ['-m', 'venv', plan.venvDir], { cwd: deps.cwd() });
  }
  for (const install of plan.installs) {
    deps.log(`installing: ${install.args.join(' ')}`);
    deps.exec(install.command, install.args, { cwd: install.cwd });
  }
}

/** Launch the service, forwarding signals; resolves with the exit code. */
export async function runService(
  plan: ServicePlan,
  options: { env?: NodeJS.ProcessEnv } = {},
  deps: ServiceDeps = defaultDeps(),
): Promise<number> {
  const env = { ...(options.env ?? deps.env) };
  const child = deps.spawn(plan.run.command, plan.run.args, { cwd: plan.run.cwd, env });

  return await new Promise<number>((resolvePromise, reject) => {
    const forward = (signal: NodeJS.Signals) => () => {
      if (!child.killed) child.kill(signal);
    };
    const onSigint = forward('SIGINT');
    const onSigterm = forward('SIGTERM');
    process.once('SIGINT', onSigint);
    process.once('SIGTERM', onSigterm);

    child.once('error', (error) => {
      process.removeListener('SIGINT', onSigint);
      process.removeListener('SIGTERM', onSigterm);
      reject(error);
    });
    child.once('exit', (code, signal) => {
      process.removeListener('SIGINT', onSigint);
      process.removeListener('SIGTERM', onSigterm);
      if (code !== null) resolvePromise(code);
      else resolvePromise(signal ? 130 : 0);
    });
  });
}

export interface DoctorCheck {
  name: string;
  ok: boolean;
  detail: string;
  /** False for informational checks that do not fail the report. */
  required?: boolean;
}

export interface DoctorReport {
  ok: boolean;
  plan: ServicePlan;
  checks: DoctorCheck[];
}

function pythonVersion(deps: ServiceDeps, python: string): string | null {
  try {
    return deps.execCapture(python, ['--version']).trim() || null;
  } catch {
    return null;
  }
}

function parsePythonVersion(text: string | null): [number, number] | null {
  const match = text?.match(/(\d+)\.(\d+)/);
  if (!match) return null;
  return [Number(match[1]), Number(match[2])];
}

/** Environment diagnostics: what is installed, what is missing. */
export function runDoctor(options: ServiceOptions = {}, deps: ServiceDeps = defaultDeps()): DoctorReport {
  const plan = resolveServicePlan(options, deps);
  const env = options.env ?? deps.env;
  const checks: DoctorCheck[] = [];

  const nodeMajor = Number(process.versions.node.split('.')[0] ?? '0');
  checks.push({
    name: 'node',
    ok: nodeMajor >= 18,
    required: true,
    detail: `v${process.versions.node}${nodeMajor >= 18 ? '' : ' (need >= 18.17)'}`,
  });

  const hostVersion = pythonVersion(deps, plan.hostPython);
  const hostParsed = parsePythonVersion(hostVersion);
  const pythonOk =
    hostParsed !== null &&
    (hostParsed[0] > MIN_PYTHON[0] || (hostParsed[0] === MIN_PYTHON[0] && hostParsed[1] >= MIN_PYTHON[1]));
  checks.push({
    name: 'python',
    ok: pythonOk,
    required: true,
    detail: hostVersion
      ? `${plan.hostPython} → ${hostVersion}${pythonOk ? '' : ' (need >= 3.10)'}`
      : `${plan.hostPython} not found on PATH`,
  });

  const venvReady = deps.exists(plan.venvPython);
  checks.push({
    name: 'virtualenv',
    ok: true,
    detail: venvReady ? `${plan.venvDir} (ready)` : `${plan.venvDir} (will be created)`,
  });

  checks.push({
    name: 'mode',
    ok: true,
    detail:
      plan.mode === 'source'
        ? `source checkout at ${plan.sourceDir}`
        : `pip package ${
            options.pipSpec || env.RESPEAKER_CLIP_PIP_SPEC || DEFAULT_PIP_SPEC
          } (use --source <dir> to run a local checkout)`,
  });

  if (deps.platform === 'linux') {
    const hasBluez = whichSync('bluetoothctl', deps);
    checks.push({
      name: 'bluez',
      ok: Boolean(hasBluez),
      required: false,
      detail: hasBluez
        ? 'bluetoothctl found — run `bluetoothctl power on` and pair the Clip'
        : 'bluetoothctl not found: BLE voice input needs BlueZ (the HTTP API still runs)',
    });
  }

  for (const warning of plan.warnings) {
    checks.push({ name: 'warning', ok: false, required: false, detail: warning });
  }

  return { ok: checks.every((check) => check.ok || check.required === false), plan, checks };
}

function whichSync(binary: string, deps: ServiceDeps): string | null {
  try {
    const path = deps.execCapture(deps.platform === 'win32' ? 'where' : 'which', [binary]);
    return path.trim().split(/\r?\n/)[0] ?? null;
  } catch {
    return null;
  }
}