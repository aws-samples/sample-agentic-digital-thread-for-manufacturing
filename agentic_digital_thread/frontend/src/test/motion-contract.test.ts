// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const STYLES_DIR = path.resolve(TEST_DIR, '../styles');
const BASE_CSS = path.resolve(STYLES_DIR, 'base.css');
const DASHBOARD_CSS = path.resolve(STYLES_DIR, 'dashboard.css');
const CARDS_SOURCE = path.resolve(TEST_DIR, '../views/dashboard/Cards.tsx');
const css = readFileSync(BASE_CSS, 'utf-8');
const allCss = readdirSync(STYLES_DIR)
  .filter((name) => name.endsWith('.css'))
  .map((name) => readFileSync(path.join(STYLES_DIR, name), 'utf-8'))
  .join('\n');
/* CSS files living OUTSIDE styles/ (component-adjacent). Included in the
   token cross-check and transition scans so they cannot drift silently. */
const EXTRA_CSS_FILES = [
  '../pages/app/user-menu.css',
  '../chat/session-menu.css',
  '../chat/prompt-library.css',
  '../chat/tool-activity.css',
  '../chat/answer.css',
  '../chat/evidence-card.css',
  '../chat/analysis-panel.css',
  '../shared/agents/session-drilldown.css',
] as const;
const wideCss = [
  allCss,
  ...EXTRA_CSS_FILES.map((rel) => readFileSync(path.resolve(TEST_DIR, rel), 'utf-8')),
].join('\n');

function blockStartingAt(source: string, start: number): string {
  const open = source.indexOf('{', start);
  if (open < 0) throw new Error('CSS block has no opening brace');

  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, index);
    }
  }
  throw new Error('CSS block has no closing brace');
}

describe('motion CSS contract', () => {
  it('defines the complete motion vocabulary in :root', () => {
    const root = blockStartingAt(css, css.indexOf(':root'));
    const tokens = {
      '--motion-fast': '120ms',
      '--motion-base': '200ms',
      '--motion-slow': '320ms',
      '--ease-standard': 'cubic-bezier(.2,0,0,1)',
      '--ease-enter': 'cubic-bezier(.16,1,.3,1)',
      '--ease-exit': 'cubic-bezier(.4,0,1,1)',
      '--motion-distance': '6px',
    } as const;

    for (const [name, value] of Object.entries(tokens)) {
      expect(root, `${name} should be defined in :root`).toContain(`${name}: ${value};`);
    }
  });

  it('defines every referenced motion and easing token', () => {
    const definitions = new Set(
      [...wideCss.matchAll(/(--(?:motion|ease)-[\w-]+)\s*:/g)].map((match) => match[1]),
    );
    const references = new Set(
      [...wideCss.matchAll(/var\((--(?:motion|ease)-[\w-]+)/g)].map((match) => match[1]),
    );
    const undefinedTokens = [...references].filter((token) => !definitions.has(token));

    expect(undefinedTokens).toEqual([]);
  });

  it.each([
    'view-enter',
    'msg-enter',
    'card-enter',
    'skeleton-shimmer',
    'visually-hidden',
  ])('defines the reusable .%s selector', (selector) => {
    expect(css).toMatch(new RegExp(`\\.${selector}(?:[\\s,{:]|::)`));
  });

  it('keeps transformed animation frames off the Architecture Cytoscape ancestor', () => {
    const selectorStart = css.indexOf('#architecture-view.view-enter');
    expect(selectorStart, 'Architecture needs a graph-safe view-enter override').toBeGreaterThanOrEqual(0);

    const architectureEnter = blockStartingAt(css, selectorStart);
    const animationName = architectureEnter.match(/animation-name\s*:\s*([\w-]+)/)?.[1];
    expect(animationName, 'Architecture should select an explicit graph-safe animation').toBeTruthy();

    const keyframesStart = css.indexOf(`@keyframes ${animationName}`);
    expect(keyframesStart, 'The graph-safe animation must be defined').toBeGreaterThanOrEqual(0);
    const keyframes = blockStartingAt(css, keyframesStart);
    expect(keyframes).not.toMatch(/\btransform\s*:/);
  });

  it('has one global reduced-motion policy covering motion and smooth scrolling', () => {
    const mediaPattern = /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)/g;
    const matches = [...css.matchAll(mediaPattern)];
    expect(matches).toHaveLength(1);

    const reducedMotion = blockStartingAt(css, matches[0].index);
    expect(reducedMotion).toMatch(/animation-duration\s*:\s*1ms\s*!important/);
    expect(reducedMotion).toMatch(/transition-duration\s*:\s*1ms\s*!important/);
    expect(reducedMotion).toMatch(/animation-iteration-count\s*:\s*1\s*!important/);
    expect(reducedMotion).toMatch(/scroll-behavior\s*:\s*auto\s*!important/);
    expect(reducedMotion).toMatch(/\.skeleton-shimmer::after[\s\S]*animation\s*:\s*none\s*!important/);
  });

  it('delays shimmer startup so fast loads do not flash', () => {
    const shimmer = blockStartingAt(css, css.indexOf('.skeleton-shimmer::after'));
    expect(shimmer).toMatch(/animation-delay\s*:\s*2[0-9]{2}ms/);
    expect(shimmer).toMatch(/opacity\s*:\s*0/);
  });

  it('uses explicit transition properties and avoids synchronous motion reflow', () => {
    expect(wideCss).not.toMatch(/transition\s*:\s*all\b/i);

    const sidebar = blockStartingAt(css, css.indexOf('.chat-sidebar {'));
    expect(sidebar).toContain('transform var(--motion-base)');
    expect(sidebar).toContain('opacity var(--motion-fast)');
    expect(sidebar).not.toMatch(/transition[^{;]*(?:width|min-width|padding)/);

    const dashboardCss = readFileSync(DASHBOARD_CSS, 'utf-8');
    const expandedTrace = blockStartingAt(
      dashboardCss,
      dashboardCss.indexOf('.trace-body.expanded'),
    );
    expect(expandedTrace).not.toMatch(/max-height\s*:\s*none/);
    expect(readFileSync(CARDS_SOURCE, 'utf-8')).not.toContain('offsetWidth');
  });
});

/* ── Motion-polish wave (modal/tab/toast/chart enters) ─────────────────── */

const readCss = (rel: string) => readFileSync(path.resolve(TEST_DIR, rel), 'utf-8');
const dashboardCss = readCss('../styles/dashboard.css');
const adminCss = readCss('../styles/admin.css');
const toastCss = readCss('../styles/toast.css');
const enhancementsCss = readCss('../styles/enhancements.css');
const onboardCss = readCss('../styles/onboard.css');
const loginCss = readCss('../styles/login.css');
const userMenuCss = readCss('../pages/app/user-menu.css');
const sessionMenuCss = readCss('../chat/session-menu.css');

/** All declared property names inside a @keyframes block (incl. from/to frames). */
function keyframeProperties(source: string, name: string): string[] {
  const start = source.indexOf(`@keyframes ${name}`);
  expect(start, `@keyframes ${name} must be defined`).toBeGreaterThanOrEqual(0);
  const block = blockStartingAt(source, start);
  return [...block.matchAll(/([a-zA-Z-]+)\s*:/g)].map((match) => match[1]);
}

describe('motion-polish enter contract', () => {
  it.each(['overlay-fade-motion', 'modal-pop-motion', 'toast-in-motion', 'panel-fade-motion'])(
    'defines compositor-only keyframes for %s in base.css',
    (name) => {
      const properties = keyframeProperties(css, name);
      expect(properties.length).toBeGreaterThan(0);
      for (const property of properties) {
        expect(['opacity', 'transform'], `${name} animates ${property}`).toContain(property);
      }
    },
  );

  it('keeps the architecture panel enter transform-free (Cytoscape safety)', () => {
    const pane = blockStartingAt(dashboardCss, dashboardCss.indexOf('.arch-graph-pane {'));
    const animationName = pane.match(/animation\s*:\s*([\w-]+)/)?.[1];
    expect(animationName).toBe('panel-fade-motion');
    expect(keyframeProperties(css, 'panel-fade-motion')).not.toContain('transform');
  });

  it.each([
    ['.detail-modal-overlay.open {', 'overlay-fade-motion', () => css],
    ['.detail-modal-overlay.open .detail-modal {', 'modal-pop-motion', () => css],
    ['.chat-modal-overlay.open {', 'overlay-fade-motion', () => dashboardCss],
    ['.chat-modal-overlay.open .chat-modal {', 'modal-pop-motion', () => dashboardCss],
    ['.data-modal-overlay .data-modal {', 'modal-pop-motion', () => dashboardCss],
    ['.tab-panel.active {', 'panel-fade-motion', () => adminCss],
    ['.vocab-modal-overlay {', 'overlay-fade-motion', () => adminCss],
    ['.vocab-concept-modal {', 'modal-pop-motion', () => adminCss],
    ['.toast {', 'toast-in-motion', () => toastCss],
    ['.chart-iframe-enter {', 'panel-fade-motion', () => enhancementsCss],
    ['.ob-modal-overlay .ob-modal {', 'modal-pop-motion', () => onboardCss],
    ['.user-dropdown {', 'item-enter-motion', () => userMenuCss],
    ['.session-menu {', 'item-enter-motion', () => sessionMenuCss],
  ])('gives %s the %s enter animation', (selector, animation, source) => {
    const cssText = source();
    const start = cssText.indexOf(selector);
    expect(start, `${selector} rule must exist`).toBeGreaterThanOrEqual(0);
    expect(blockStartingAt(cssText, start)).toContain(`animation: ${animation}`);
  });

  it('staggers dashboard card reveals in 40ms steps capped at the 8th card', () => {
    for (let child = 2; child <= 8; child += 1) {
      const rule = new RegExp(
        `\\.card-enter:nth-child\\(${child}\\)\\s*\\{\\s*animation-delay:\\s*${(child - 1) * 40}ms`,
      );
      expect(css).toMatch(rule);
    }
    expect(css).not.toMatch(/\.card-enter:nth-child\(9\)/);
  });

  it('covers the new enter states in the single reduced-motion block', () => {
    const media = blockStartingAt(css, css.search(/@media\s*\(\s*prefers-reduced-motion/));
    for (const selector of [
      '.detail-modal-overlay.open',
      '.chat-modal-overlay.open',
      '.data-modal-overlay',
      '.ob-modal-overlay',
      '.vocab-concept-modal',
      '.tab-panel.active',
      '.arch-graph-pane',
      '.toast',
      '.user-dropdown',
      '.session-menu',
      '.chart-iframe-enter',
    ]) {
      expect(media, `${selector} must be reduced-motion covered`).toContain(selector);
    }
    expect(media).toMatch(/animation-delay\s*:\s*0ms\s*!important/);
  });

  it('keeps the login entry motion self-contained (login.html loads only login.css)', () => {
    expect(loginCss).toMatch(/--motion-slow\s*:\s*320ms/);
    expect(loginCss).toMatch(/--ease-enter\s*:\s*cubic-bezier\(\.16,1,\.3,1\)/);
    expect(loginCss).toContain('@keyframes login-card-enter-motion');
    expect(loginCss).toMatch(/@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)/);
    const cardEnter = blockStartingAt(loginCss, loginCss.indexOf('.login-page .card {'));
    expect(cardEnter).toContain('animation: login-card-enter-motion');
  });
});
