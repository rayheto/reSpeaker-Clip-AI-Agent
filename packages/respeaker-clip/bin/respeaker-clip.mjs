#!/usr/bin/env node
// Thin launcher: keeps the executable free of build-time logic and lets the
// CLI itself stay a normal, testable ESM module.
import { main } from '../dist/esm/cli.js';

try {
  process.exitCode = await main(process.argv.slice(2));
} catch (error) {
  console.error(`respeaker-clip failed: ${error?.message ?? String(error)}`);
  process.exitCode = 1;
}