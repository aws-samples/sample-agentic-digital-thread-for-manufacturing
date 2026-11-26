// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The active-only briefing gate must be visible on its existing card. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('../lib/i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}));

import { AiBriefing } from '../views/dashboard/AiBriefing';
import { BRIEFING_PROMPT } from '../views/dashboard/briefing';
import { patchBriefing } from '../views/dashboard/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

it('shows scheduled while the dashboard lifecycle gate is active', () => {
  patchBriefing({ lifecycle: 'active' });
  act(() => root.render(<AiBriefing />));
  expect(container.querySelector('#briefing-lifecycle')?.textContent).toContain('scheduled');
});

it('shows paused while the dashboard lifecycle gate is inactive', () => {
  patchBriefing({ lifecycle: 'inactive' });
  act(() => root.render(<AiBriefing />));
  expect(container.querySelector('#briefing-lifecycle')?.textContent).toContain('paused');
});

it('adds no HTML-string sink', async () => {
  const source = await import('../views/dashboard/AiBriefing?raw');
  expect(source.default).not.toMatch(/dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML/);
});

it('renders a caveat note under the bullet cards, not as a card', () => {
  const bullet = { title: 'Order risk', detail: '22 open orders at risk', color: '#58a6ff' };
  patchBriefing({
    phase: 'bullets',
    bullets: [bullet, bullet, bullet, bullet, bullet],
    notes: ['Note: the order value total was flagged as unverified, so it is left out.'],
  });
  act(() => root.render(<AiBriefing />));
  const notes = container.querySelectorAll('.briefing-note');
  expect(notes).toHaveLength(1);
  expect(notes[0].textContent).toContain('flagged as unverified');
  expect(container.querySelectorAll('.briefing-bullet')).toHaveLength(5);
});

it('renders all the notes as ONE markdown block, so a note over several lines reads whole', () => {
  const bullet = { title: 'Order risk', detail: '22 open orders at risk', color: '#58a6ff' };
  patchBriefing({
    phase: 'bullets',
    bullets: [bullet, bullet, bullet, bullet, bullet],
    notes: ['*Note:* the total was capped at 500 rows,', 'so the order value is left out.', '- One more item.'],
  });
  act(() => root.render(<AiBriefing />));
  const notes = container.querySelectorAll('.briefing-note');
  expect(notes).toHaveLength(1);
  expect(notes[0].querySelector('em')?.textContent).toBe('Note:');
  expect(notes[0].textContent).toContain('capped at 500 rows,');
  expect(notes[0].textContent).toContain('so the order value is left out.');
  expect(notes[0].querySelectorAll('li')).toHaveLength(1);
  expect(container.querySelectorAll('.briefing-bullet')).toHaveLength(5);
});

it('a child item in the note block renders nested under its parent', () => {
  const bullet = { title: 'Order risk', detail: '22 open orders at risk', color: '#58a6ff' };
  patchBriefing({
    phase: 'bullets',
    bullets: [bullet, bullet, bullet, bullet, bullet],
    notes: ['- f six', '  - its detail'],
  });
  act(() => root.render(<AiBriefing />));
  const note = container.querySelector('.briefing-note');
  expect(note?.querySelector('li li')?.textContent).toBe('its detail');
});

it('renders no note block when there are no notes', () => {
  const bullet = { title: 'Order risk', detail: '22 open orders at risk', color: '#58a6ff' };
  patchBriefing({ phase: 'bullets', bullets: [bullet, bullet, bullet], notes: [] });
  act(() => root.render(<AiBriefing />));
  expect(container.querySelectorAll('.briefing-note')).toHaveLength(0);
});

it('the title tooltip shows the prompt the briefing actually sends', () => {
  patchBriefing({ phase: 'init', bullets: [], notes: [] });
  act(() => root.render(<AiBriefing />));
  const tip = container.querySelector('.briefing-title-tooltip')?.getAttribute('data-tooltip');
  expect(tip).toBe(`Agent prompt: ${BRIEFING_PROMPT}`);
});
