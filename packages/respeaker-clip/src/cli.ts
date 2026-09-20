#!/usr/bin/env node
/**
 * `respeaker-clip` CLI.
 *
 *   respeaker-clip serve    bootstrap the Python Clip service and run it
 *   respeaker-clip status   print the status of a running service
 *   respeaker-clip doctor   report what is installed / missing
 *
 * Run `respeaker-clip <command> --help` for the options of each command.
 */

import { parseArgs } from 'node:util';
import { ClipClient } from './client.js';
import { ClipApiError } from './errors.js';
import {
  DEFAULT_PIP_SPEC,
  DEFAULT_PORT,
  describePlan,
  ensureEnvironment,
  resolveServicePlan,
  runDoctor,
  runService,
  type ServiceOptions,
} from './service.js';
import { PACKAGE_NAME, VERSION } from './version.js';

const USAGE = `${PACKAGE_NAME} ${VERSION} — reSpeaker Clip service runner and SDK CLI

Usage:
  ${PACKAGE_NAME} serve [options]        Install (if needed) and run the Clip service
  ${PACKAGE_NAME} status [options]       Print /api/clip/status from a running service
  ${PACKAGE_NAME} doctor [options]       Check node/python/venv/bluez prerequisites

serve options:
  --source <dir>          Run from a local checkout (default: $RESPEAKER_CLIP_SERVICE_ROOT)
  --venv <dir>            Virtualenv directory (default: $RESPEAKER_CLIP_HOME/venv)
  --pip-spec <spec>       pip requirement to install (default: ${DEFAULT_PIP_SPEC})
  --skip-install          Do not pip install; run what the venv already has
  --host <addr>           Bind address (default: 0.0.0.0)
  --port <port>           Port (default: ${DEFAULT_PORT})
  --input-mode <mode>     browser | clip | both (default: clip)
  --ble-address <addr>    Pin the Clip by BLE address
  --ble-name <name>       Scan for this BLE name instead
  --no-clip               Serve the HTTP API without the BLE runtime
  --no-agent              Device gateway: no agent stack, no API key, audio over HTTP
  --env-file <path>       .env to load (default: ./.env in the working directory)
  --dry-run               Print the resolved plan and exit
  --json                  Machine-readable output
  -- <args...>            Extra arguments forwarded to the service

status options:
  --base-url <url>        Service origin (default: http://127.0.0.1:${DEFAULT_PORT})
  --json                  Print the raw status payload

doctor options:
  --json                  Print the full report
`;

interface CliIo {
  out: (message: string) => void;
  err: (message: string) => void;
}

const defaultIo: CliIo = {
  out: (message) => console.log(message),
  err: (message) => console.error(message),
};

function parseCommandLine(argv: string[]) {
  return parseArgs({
    args: argv,
    allowPositionals: true,
    strict: true,
    options: {
      help: { type: 'boolean', short: 'h' },
      version: { type: 'boolean', short: 'v' },
      json: { type: 'boolean' },
      'dry-run': { type: 'boolean' },
      'base-url': { type: 'string' },
      host: { type: 'string' },
      port: { type: 'string' },
      'input-mode': { type: 'string' },
      'ble-address': { type: 'string' },
      'ble-name': { type: 'string' },
      'no-clip': { type: 'boolean' },
      'no-agent': { type: 'boolean' },
      'env-file': { type: 'string' },
      source: { type: 'string' },
      venv: { type: 'string' },
      'pip-spec': { type: 'string' },
      'skip-install': { type: 'boolean' },
    },
  });
}

type ParsedArgs = ReturnType<typeof parseCommandLine>;

function toServiceOptions(parsed: ParsedArgs): ServiceOptions {
  const { values, positionals } = parsed;
  const options: ServiceOptions = {};
  if (values.host) options.host = values.host;
  if (values.port) {
    const port = Number.parseInt(values.port, 10);
    if (!Number.isInteger(port) || port <= 0 || port > 65535) {
      throw new Error(`invalid --port: ${values.port}`);
    }
    options.port = port;
  }
  if (values['input-mode']) {
    const mode = values['input-mode'];
    if (mode !== 'browser' && mode !== 'clip' && mode !== 'both') {
      throw new Error(`invalid --input-mode: ${mode} (expected browser|clip|both)`);
    }
    options.inputMode = mode;
  }
  if (values['ble-address']) options.bleAddress = values['ble-address'];
  if (values['ble-name']) options.bleName = values['ble-name'];
  if (values['no-clip']) options.noClip = true;
  if (values['no-agent']) options.noAgent = true;
  if (values['env-file']) options.envFile = values['env-file'];
  if (values.source) options.source = values.source;
  if (values.venv) options.venv = values.venv;
  if (values['pip-spec']) options.pipSpec = values['pip-spec'];
  if (values['skip-install']) options.skipInstall = true;
  // Everything after `--` is forwarded to the service untouched.
  const extra = positionals.slice(1);
  if (extra.length) options.extraArgs = extra;
  return options;
}

function baseUrlFrom(parsed: ParsedArgs, env: NodeJS.ProcessEnv): string {
  const explicit = parsed.values['base-url'] || env.RESPEAKER_CLIP_BASE_URL;
  if (explicit) return explicit;
  return `http://127.0.0.1:${DEFAULT_PORT}`;
}

async function serve(parsed: ParsedArgs, io: CliIo): Promise<number> {
  const options = toServiceOptions(parsed);
  const env = process.env;
  const plan = resolveServicePlan(options);

  for (const warning of plan.warnings) io.err(`warning: ${warning}`);

  if (parsed.values['dry-run']) {
    if (parsed.values.json) {
      io.out(
        JSON.stringify(
          {
            mode: plan.mode,
            sourceDir: plan.sourceDir,
            venvDir: plan.venvDir,
            installs: plan.installs,
            run: plan.run,
            warnings: plan.warnings,
          },
          null,
          2,
        ),
      );
    } else {
      io.out(describePlan(plan));
    }
    return 0;
  }

  io.err(
    plan.mode === 'source'
      ? `starting Clip service from ${plan.sourceDir}`
      : `starting Clip service (pip: ${options.pipSpec || env.RESPEAKER_CLIP_PIP_SPEC || DEFAULT_PIP_SPEC})`,
  );
  ensureEnvironment(plan);
  if (parsed.values.json) io.out(JSON.stringify({ run: plan.run }, null, 2));
  return await runService(plan, { env });
}

async function status(parsed: ParsedArgs, io: CliIo): Promise<number> {
  const baseUrl = baseUrlFrom(parsed, process.env);
  const client = new ClipClient({ baseUrl });
  try {
    const payload = await client.getStatus();
    if (parsed.values.json) {
      io.out(JSON.stringify(payload, null, 2));
      return 0;
    }
    const lines = [
      `Clip service at ${baseUrl}`,
      `  connected     : ${payload.connected ? 'yes' : 'no'}`,
      `  device        : ${payload.device_id || '-'}${
        typeof payload.battery_percent === 'number' ? ` (battery ${payload.battery_percent}%)` : ''
      }`,
      `  recording     : ${payload.recording ? `yes (${payload.session || 'session'})` : 'no'}`,
      `  rtc phase     : ${payload.rtc_phase || '-'}`,
      `  rtc session   : ${payload.rtc_session || '-'}`,
      `  rtc utterance : ${payload.rtc_utterance_id ?? '-'}`,
      `  processing    : ${payload.rtc_processing ? 'yes' : 'no'}`,
      `  input mode    : ${payload.input_mode || '-'}`,
      `  agent mode    : ${payload.agent_enabled === false ? 'off (device gateway)' : 'on'}`,
      `  last error    : ${payload.last_error || payload.rtc_error || '-'}`,
    ];
    io.out(lines.join('\n'));
    return payload.connected ? 0 : 3;
  } catch (error) {
    if (error instanceof ClipApiError && error.isOffline) {
      io.err(`Clip service is reachable but the Clip is unavailable: ${error.message}`);
      return 3;
    }
    io.err(`cannot read status from ${baseUrl}: ${(error as Error)?.message ?? String(error)}`);
    return 1;
  }
}

function doctor(parsed: ParsedArgs, io: CliIo): number {
  const report = runDoctor(toServiceOptions(parsed));
  if (parsed.values.json) {
    io.out(JSON.stringify(report, null, 2));
    return report.ok ? 0 : 1;
  }
  io.out(describePlan(report.plan));
  io.out('');
  for (const check of report.checks) {
    const marker = check.ok ? '[ok]' : check.required === false ? '[--]' : '[!!]';
    io.out(`${marker} ${check.name.padEnd(12)} ${check.detail}`);
  }
  return report.ok ? 0 : 1;
}

export async function main(argv: string[] = process.argv.slice(2), io: CliIo = defaultIo): Promise<number> {
  let parsed: ParsedArgs;
  try {
    parsed = parseCommandLine(argv);
  } catch (error) {
    io.err(`${(error as Error).message}\n`);
    io.err(USAGE);
    return 2;
  }

  if (parsed.values.version) {
    io.out(VERSION);
    return 0;
  }

  const command = parsed.positionals[0];
  if (parsed.values.help) {
    io.out(USAGE);
    return 0;
  }
  if (!command) {
    io.err(USAGE);
    return 2;
  }

  try {
    switch (command) {
      case 'serve':
        return await serve(parsed, io);
      case 'status':
        return await status(parsed, io);
      case 'doctor':
        return doctor(parsed, io);
      default:
        io.err(`unknown command: ${command}\n`);
        io.err(USAGE);
        return 2;
    }
  } catch (error) {
    io.err(`${command} failed: ${(error as Error)?.message ?? String(error)}`);
    return 1;
  }
}