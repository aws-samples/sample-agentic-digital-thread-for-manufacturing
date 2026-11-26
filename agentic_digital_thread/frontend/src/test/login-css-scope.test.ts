// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* main-app.tsx imports login.css next to the app's own styles, so a bare
 * selector in login.css styles the whole app. login.css hid `.error`
 * (display: none until `.visible`), and the onboarding wizard renders every
 * step failure as `<div class="ob-msg error">` with its Retry button: each
 * one was invisible, and a failed step looked like a wizard that had frozen.
 * The login page's error rules must stay on the login page. */
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

const STYLES_DIR = path.resolve(__dirname, '../styles');
const loginCss = readFileSync(path.join(STYLES_DIR, 'login.css'), 'utf8');
const mainApp = readFileSync(path.resolve(__dirname, '../main-app.tsx'), 'utf8');

/** Every selector (comma-split) in a stylesheet: comments and at-rule
 * preludes (@media, @keyframes) removed, keyframe steps skipped. */
function selectors(css: string): string[] {
  const body = css.replace(/\/\*[\s\S]*?\*\//g, '').replace(/@[^{;]+\{/g, '{');
  const out: string[] = [];
  for (const match of body.matchAll(/([^{}@;]+)\{/g)) {
    for (const part of match[1].split(',')) {
      const sel = part.trim();
      if (sel && !/^(from|to|\d+%)$/.test(sel)) out.push(sel);
    }
  }
  return out;
}

describe('login.css error rules stay on the login page', () => {
  it('is imported by the app shell (why the scope matters)', () => {
    expect(mainApp).toContain("import './styles/login.css'");
  });

  it('scopes every .error selector under .login-page', () => {
    const errorSelectors = selectors(loginCss).filter((s) => /\.error\b/.test(s));
    expect(errorSelectors.length).toBeGreaterThan(0);
    for (const sel of errorSelectors) {
      expect(sel, sel).toMatch(/^\.login-page\s/);
    }
  });

  it('scopes every class selector under .login-page (only bare label/input stay global)', () => {
    for (const sel of selectors(loginCss)) {
      if (sel === '.login-page' || sel.startsWith('.login-page ')) continue;
      // The app's own form controls take their look from these two today.
      expect(['label', 'input', 'input:focus'], sel).toContain(sel);
    }
  });

  it('still hides the login error until it is visible', () => {
    expect(loginCss).toMatch(/\.login-page \.error\s*\{[^}]*display:\s*none/);
    expect(loginCss).toMatch(/\.login-page \.error\.visible\s*\{[^}]*display:\s*block/);
  });
});
