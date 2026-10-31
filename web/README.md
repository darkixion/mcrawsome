# Interface tooling

Run `npm ci`, then `npm run check`, `npm test` and `npm run build` in this folder.
See [e2e/README.md](e2e/README.md) for the browser tests.

TypeScript 7.0.2 is pinned as `@typescript/native` using an npm alias. `check` uses
it for Svelte diagnostics through `svelte-check --tsgo` and for the build and
browser-test configuration through its explicit `bin/tsc` path. The configuration
test also invokes that compiler. The explicit path avoids the shared `tsc` binary
name of the two TypeScript packages.

TypeScript 5.9.3 remains pinned as `typescript` for the JavaScript compiler API
used by Svelte's source conversion and `openapi-typescript` 7.13.0. Replacing that
package with TypeScript 7 makes `svelte-check` reject the version and makes
`openapi-typescript` fail at `ts.factory.createKeywordTypeNode`.

Upstream references checked for this migration:

- [Svelte's TypeScript 7 issue](https://github.com/sveltejs/language-tools/issues/3063)
  and [dual compiler instructions](https://github.com/sveltejs/language-tools/tree/master/packages/svelte-check#typescript-7-supports).
  The instructions suggest TypeScript 6, but 5.9.3 satisfies the installed
  `svelte-check` peer range and keeps the OpenAPI generator's `^5.x` peer valid.
- [svelte-check 4.7.6 release notes](https://github.com/sveltejs/language-tools/releases/tag/svelte-check%404.7.6).
- [openapi-typescript 7.13.0 release notes](https://github.com/openapi-ts/openapi-typescript/releases/tag/openapi-typescript%407.13.0)
  and [compiler compatibility work](https://github.com/openapi-ts/openapi-typescript/pull/2818).
- [Vite Svelte plugin 7.3.1 release notes](https://github.com/sveltejs/vite-plugin-svelte/releases/tag/%40sveltejs%2Fvite-plugin-svelte%407.3.1)
  and [Vitest 5.0.4 release notes](https://github.com/vitest-dev/vitest/releases/tag/v5.0.4).
  These tools run with the dual compiler installation; Vite remains at 8.3.4.

`--tsgo` writes generated checking files under `.svelte-check/`, which is ignored.
Svelte's CLI mode has the upstream incremental-mode limitation that Svelte files
outside the configured root are not fully checked. This project keeps its Svelte
files under `src/` within that root.
