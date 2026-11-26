// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Vitest config (jsx-migration W1.1). Vitest prefers vitest.config.ts over
 * vite.config.ts; this merges the full vite config (plugins, test defaults)
 * and ONLY broadens the test include to also match .tsx specs — component
 * tests (e.g. src/test/md-component.test.tsx) need JSX. Everything else
 * (node default environment, per-file jsdom pragmas) is inherited. */
import { defineConfig, mergeConfig } from 'vitest/config';

import viteConfig from './vite.config';

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      // mergeConfig concatenates with vite.config.ts's
      // include: ['src/test/**/*.test.ts'].
      include: ['src/test/**/*.test.tsx'],
    },
  }),
);
