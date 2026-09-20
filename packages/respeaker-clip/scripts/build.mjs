#!/usr/bin/env node
// Dual build: ESM + CJS into dist/, with a per-directory module marker so Node
// resolves each output tree with the right module system.
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const pkgDir = join(root, '..');
const tsc = join(pkgDir, 'node_modules', '.bin', 'tsc');

// The CLI reports its version from src/version.ts (a plain constant, so the
// output works as both ESM and CJS). Fail loudly if it drifts from the manifest.
const manifest = JSON.parse(readFileSync(join(pkgDir, 'package.json'), 'utf8'));
const versionSource = readFileSync(join(pkgDir, 'src', 'version.ts'), 'utf8');
const declared = /VERSION = '([^']+)'/.exec(versionSource)?.[1];
if (declared !== manifest.version) {
  throw new Error(
    `src/version.ts declares ${declared} but package.json declares ${manifest.version}`,
  );
}

rmSync(join(pkgDir, 'dist'), { recursive: true, force: true });

for (const project of ['tsconfig.esm.json', 'tsconfig.cjs.json']) {
  execFileSync(tsc, ['-p', join(pkgDir, project)], { stdio: 'inherit', cwd: pkgDir });
}

// The package root is "type": "module"; mark the CJS output otherwise.
mkdirSync(join(pkgDir, 'dist', 'cjs'), { recursive: true });
writeFileSync(
  join(pkgDir, 'dist', 'cjs', 'package.json'),
  JSON.stringify({ type: 'commonjs' }, null, 2) + '\n',
);
writeFileSync(
  join(pkgDir, 'dist', 'esm', 'package.json'),
  JSON.stringify({ type: 'module' }, null, 2) + '\n',
);
console.log('built dist/esm + dist/cjs');