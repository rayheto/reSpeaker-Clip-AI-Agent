/**
 * Kept in sync with `package.json` by `scripts/build.mjs`, so the CLI can
 * report a version without reading the manifest at runtime (which would need
 * `import.meta` in ESM and `__dirname` in CJS).
 */
export const VERSION = '0.1.0';
export const PACKAGE_NAME = 'respeaker-clip';