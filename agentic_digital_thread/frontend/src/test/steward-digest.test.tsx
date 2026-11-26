// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — StewardDigest dashboard card (ADR-0052 B5):
 * (1) renders the digest counts (worth-review link, tier badges, top
 *     artifacts with evidence classes, expired/quarantine/staging stats);
 * (2) hides ENTIRELY on 403/error (admin-gated endpoint — non-admins never
 *     see a broken card);
 * (3) the worth-review link navigates: switchView('architecture') +
 *     requestArchTab('proposed') (the arch-tab-bridge lazy-mount seam);
 * (4) XSS-safety: server claim strings render as TEXT (zero innerHTML);
 * (5) the counters + cost line stay behind a COLLAPSED "Technical details"
 *     expander — the visibility contract the e2e suite must walk
 *     (tests/e2e/conftest.py::open_digest_technical);
 * (6) ADR-0063 D3 precision: plain-language accept rate + per-method
 *     breakdown, the too-few-decisions note in place of any rate below the
 *     minimum sample, nothing at all when the block is absent, and the block's
 *     PLACEMENT behind the same collapsed expander (a deliberate 2026-08-18
 *     choice, pinned so it cannot drift unnoticed). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../api/client';
import type { LearningDigest } from '../api/types/learning';
import { requestArchTab } from '../views/architecture/arch-tab-bridge';
import { StewardDigest } from '../views/dashboard/StewardDigest';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});
vi.mock('../views/architecture/arch-tab-bridge', () => ({
  requestArchTab: vi.fn(),
}));

const requestJsonMock = vi.mocked(requestJson);
const requestArchTabMock = vi.mocked(requestArchTab);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DIGEST: LearningDigest = {
  worth_review: [
    {
      mapping_id: 'm-1',
      claim: 'join_path: sys-a.t1.f1 ↔ sys-b.t2.f2',
      consequence: 3.4,
      provenance_tier: 2,
    },
    {
      mapping_id: 'm-2',
      claim: 'identity_rule: sys-c.t3.f3 ↔ sys-d.t4.f4',
      consequence: 1.2,
      provenance_tier: 0,
    },
  ],
  pending_total: 12,
  expired_unreviewed: 3,
  top_artifacts: [
    {
      artifact_id: 'jp-sysa-0123456789abcdef',
      kind: 'join_path',
      usage_30d: 4,
      evidence_classes: ['route-intersection'],
    },
  ],
  quarantine: { pending_quarantined: 4 },
  staging: { rows: 3, distinct_subjects: 2 },
};

let container: HTMLDivElement;
let root: Root;

/** How many CLOSED `<details>` ancestors stand between `el` and `root`.
 * jsdom does no layout, so this is the offline proxy for the browser's
 * visibility rule: a closed `<details>` renders its non-`<summary>` children
 * with a zero box, which Playwright reports as `hidden`. 0 = a browser would
 * paint it; >0 = an expander must be opened first. */
function closedDetailsAncestors(el: HTMLElement, root: HTMLElement): number {
  let n = 0;
  for (let node = el.parentElement; node && node !== root.parentElement; node = node.parentElement) {
    if (node instanceof HTMLDetailsElement && !node.open) n += 1;
  }
  return n;
}

beforeEach(() => {
  requestJsonMock.mockReset();
  requestArchTabMock.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  delete (window as { switchView?: unknown }).switchView;
  vi.restoreAllMocks();
});

async function render(): Promise<void> {
  await act(async () => {
    root.render(<StewardDigest />);
  });
}

describe('StewardDigest', () => {
  it('renders plain-language rows with the technical claim behind a per-row expander', async () => {
    requestJsonMock.mockResolvedValueOnce(DIGEST);

    await render();

    const card = container.querySelector('#steward-digest-card');
    expect(card).not.toBeNull();
    expect(card!.textContent).toContain('Recent learning');
    const link = card!.querySelector<HTMLButtonElement>('.steward-digest-review-link');
    expect(link?.textContent).toBe('12 pending proposals — review');
    // Rows lead with the plain-language tier VERB, never a status badge.
    const tiers = Array.from(card!.querySelectorAll('.steward-digest-tier')).map(
      (el) => el.textContent,
    );
    expect(tiers).toEqual(['Proposed:', 'Steward-defined:']);
    // identity_rule humanizes; the raw claim only lives in the row expander.
    expect(card!.textContent).toContain('Treat f3 in sys-c as the same as f4 in sys-d');
    const tech = card!.querySelector('.steward-digest-row-tech');
    expect(tech?.textContent).toContain('join_path: sys-a.t1.f1 ↔ sys-b.t2.f2');
    expect(tech?.textContent).toContain('3.4');
    // Artifacts humanize inside "Technical details" — never a bare id.
    const artifact = card!.querySelector('.steward-digest-artifact');
    expect(artifact?.textContent).toContain('Join path · sysa');
    expect(artifact?.textContent).not.toContain('jp-sysa-0123456789abcdef');
    expect(artifact?.getAttribute('title')).toBe('jp-sysa-0123456789abcdef');
    expect(artifact?.textContent).toContain('4 uses · route-intersection');
    // Footer stats survive inside the technical expander.
    expect(card!.querySelector('.steward-digest-stats')?.textContent).toBe(
      '3 expired unreviewed · 4 quarantined · 3 staged',
    );
    expect(requestJsonMock).toHaveBeenCalledWith('/api/admin/learning/digest');
  });

  it('hides the card entirely on 403 (non-admin) and on errors', async () => {
    requestJsonMock.mockRejectedValueOnce(
      new ApiError(403, '/api/admin/learning/digest', 'Forbidden'),
    );

    await render();

    expect(container.querySelector('#steward-digest-card')).toBeNull();
    expect(container.textContent).toBe('');
  });

  it('navigates to the Architecture review list on link click', async () => {
    requestJsonMock.mockResolvedValueOnce(DIGEST);
    const switchView = vi.fn();
    (window as { switchView?: (v: string) => void }).switchView = switchView;

    await render();
    const link = container.querySelector<HTMLButtonElement>('.steward-digest-review-link');
    await act(async () => {
      link!.click();
    });

    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(requestArchTabMock).toHaveBeenCalledWith('proposed');
  });

  it('renders hostile server strings as text — no element injection', async () => {
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      worth_review: [
        {
          mapping_id: 'm-x',
          claim: '<img src=x onerror="window.__pwned=1">',
          consequence: 9.9,
          provenance_tier: 1,
        },
      ],
    });

    await render();

    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="window.__pwned=1">');
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('renders the E7 cost trend line (turn-weighted avg + scope note) when cost_trend has days', async () => {
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      cost_trend: {
        days: [
          { date: '2026-07-24', turns: 2, avg_total_tokens: 4000 },
          { date: '2026-07-25', turns: 6, avg_total_tokens: 6000 },
        ],
        scope: 'orchestrator-model-only',
      },
    });

    await render();

    const line = container.querySelector<HTMLElement>('.steward-digest-cost-trend');
    expect(line).not.toBeNull();
    // Weighted: (2*4000 + 6*6000) / 8 = 5500 — never a per-day flat average.
    expect(line!.textContent).toBe(`Avg tokens/answer (7d): ${(5500).toLocaleString()} · orchestrator model only`);
    expect(line!.title).toBe(
      'Measured orchestrator model usage only — system-agent model calls are not included. No cost estimate is derived.',
    );
    // Existing footer stats are untouched by the trend line.
    expect(container.querySelector('.steward-digest-stats')?.textContent).toBe(
      '3 expired unreviewed · 4 quarantined · 3 staged',
    );
  });

  it('keeps the D3 precision block behind the SAME collapsed expander', async () => {
    // Placement pin. Reconstructed from history 2026-08-19 after I misread it
    // as a merge artifact: the 2026-08-18 change deliberately put PrecisionBlock inside
    // `details.steward-digest-technical`, honoring the 2026-08-06 steward
    // feedback that the card surface was "too technical - can't tell what I'm
    // supposed to care about". The surface stays three rows + one count line;
    // the miner's accept rate is ONE click away, not on the surface. If a
    // future change surfaces it deliberately, update this pin and the
    // docblock together - do not let the placement drift silently.
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      precision: {
        decisions: 223, accepted: 28, rate: 0.126,
        window_days: 30, min_decisions: 10,
        by_method: { hierarchy_position: { decisions: 16, accepted: 0, rate: 0.0 } },
      },
    });

    await render();

    const card = container.querySelector<HTMLElement>('#steward-digest-card')!;
    const block = card.querySelector<HTMLElement>('.steward-digest-precision')!;
    expect(block).toBeTruthy();
    // Rendered (so a steward CAN reach it) but behind a closed expander.
    expect(closedDetailsAncestors(block, card)).toBeGreaterThan(0);
  });

  it('keeps the counters and cost line behind a COLLAPSED expander (the e2e visibility contract)', async () => {
    // Regression pin for the 2026-08-10/18 e2e failures: the 2026-08-06 change moved
    // `.steward-digest-stats` and `.steward-digest-cost-trend` inside
    // `<details class="steward-digest-technical">`. A closed <details> gives
    // its non-<summary> children a zero box, so Playwright resolves them but
    // reports `hidden` — `expect(...).to_be_visible()` on the digest COUNTERS
    // is only legal after the expander is opened. jsdom has no layout, so the
    // browser-visibility proxy asserted here is "how many CLOSED <details>
    // ancestors stand between the node and the card": >0 means hidden.
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      cost_trend: {
        days: [{ date: '2026-07-25', turns: 4, avg_total_tokens: 5000 }],
        scope: 'orchestrator-model-only',
      },
    });

    await render();

    const card = container.querySelector<HTMLElement>('#steward-digest-card')!;
    const tech = card.querySelector<HTMLDetailsElement>('details.steward-digest-technical')!;
    // Collapsed on mount — the surface must stay plain-language.
    expect(tech.open).toBe(false);
    expect(tech.querySelector('summary')?.textContent).toBe('Technical details');

    const behindExpander = ['.steward-digest-stats', '.steward-digest-cost-trend'];
    for (const sel of behindExpander) {
      const node = card.querySelector<HTMLElement>(sel);
      expect(node, sel).not.toBeNull();
      expect(node!.closest('details.steward-digest-technical'), sel).toBe(tech);
      expect(closedDetailsAncestors(node!, card), sel).toBe(1);
    }

    // The plain-language SURFACE is never behind an expander — the proposals
    // e2e clicks the review link straight from the card.
    for (const sel of ['.steward-digest-heading', '.steward-digest-review-link']) {
      const node = card.querySelector<HTMLElement>(sel);
      expect(node, sel).not.toBeNull();
      expect(closedDetailsAncestors(node!, card), sel).toBe(0);
    }

    // Opening the ONE expander reveals both — no second collapsed layer.
    tech.open = true;
    for (const sel of behindExpander) {
      expect(closedDetailsAncestors(card.querySelector<HTMLElement>(sel)!, card), sel).toBe(0);
    }
  });

  it('renders NO trend line when cost_trend is absent or carries no usable days', async () => {
    // Absent key (the base DIGEST) — nothing rendered, no empty-state row.
    requestJsonMock.mockResolvedValueOnce(DIGEST);
    await render();
    expect(container.querySelector('.steward-digest-cost-trend')).toBeNull();
    expect(container.textContent).not.toContain('Avg tokens/answer');
    // The expander and the lifecycle counters still render — so the e2e's
    // "anchor on the rendered card, then assert the cost line's ABSENCE"
    // cannot pass vacuously on an unloaded digest.
    expect(container.querySelector('details.steward-digest-technical')).not.toBeNull();
    expect(container.querySelector('.steward-digest-stats')).not.toBeNull();

    // Empty days / zero-turn days — still nothing (never a fabricated zero).
    act(() => root.unmount());
    root = createRoot(container);
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      cost_trend: {
        days: [{ date: '2026-07-25', turns: 0, avg_total_tokens: 0 }],
        scope: 'orchestrator-model-only',
      },
    });
    await render();
    expect(container.querySelector('.steward-digest-cost-trend')).toBeNull();
  });

  it('leads a tier-1 pending row with an evidence verb, never Accepted', async () => {
    // Every worth_review row is PENDING by construction; an acceptance badge
    // on one misreads as already-reviewed (observed live).
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      worth_review: [
        { mapping_id: 'm-t1', claim: 'crosswalk: a.x ↔ b.y', consequence: 0.6, provenance_tier: 1 },
      ],
    });
    await render();
    const tiers = Array.from(
      container.querySelectorAll('.steward-digest-tier'),
    ).map((el) => el.textContent);
    expect(tiers).toEqual(['Proposed (with evidence):']);
    expect(container.textContent).not.toContain('Accepted');
  });

  it('shows the stale-system count when present and hides it when absent', async () => {
    requestJsonMock.mockResolvedValueOnce({ ...DIGEST, stale_system_refs: 31 });
    await render();
    expect(container.textContent).toContain(
      '+31 for systems no longer onboarded',
    );
    act(() => root.unmount());
    container.remove();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    requestJsonMock.mockResolvedValueOnce(DIGEST); // key absent = unknown
    await render();
    expect(container.textContent).not.toContain('no longer onboarded');
  });

  /* ADR-0063 D3 — miner precision in plain language. Report-only: the block
   * describes the machine's proposal quality, never a gate. */
  it('states the D3 accept rate in plain language with a per-method breakdown', async () => {
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      precision: {
        decisions: 55,
        accepted: 7,
        rate: 0.127,
        window_days: 30,
        min_decisions: 10,
        by_method: {
          value_overlap: { decisions: 45, accepted: 7, rate: 0.156 },
          hierarchy_position: { decisions: 10, accepted: 0, rate: 0 },
        },
      },
    });

    await render();

    const block = container.querySelector<HTMLElement>('.steward-digest-precision');
    expect(block).not.toBeNull();
    expect(
      block!.querySelector('.steward-digest-precision-overall')?.textContent,
    ).toBe('Of your last 55 decisions on machine-proposed learnings, 13% were accepted');
    const methods = Array.from(
      block!.querySelectorAll('.steward-digest-precision-method'),
    ).map((el) => el.textContent);
    expect(methods).toEqual([
      'Value overlap: 16% of 45 accepted',
      'Hierarchy position: 0% of 10 accepted',
    ]);
    // Report-only copy: nothing about gating or auto-acceptance.
    expect(block!.title).toContain('never changes what gets filed or accepted');
  });

  it('states its own smallness instead of a rate below the minimum sample', async () => {
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      precision: {
        decisions: 3,
        accepted: 2,
        window_days: 30,
        min_decisions: 10,
        by_method: { value_overlap: { decisions: 3, accepted: 2 } },
      },
    });

    await render();

    const block = container.querySelector<HTMLElement>('.steward-digest-precision');
    expect(block!.querySelector('.steward-digest-precision-overall')?.textContent).toBe(
      'Machine-proposed learnings: too few decisions yet to judge (3 so far)',
    );
    expect(
      block!.querySelector('.steward-digest-precision-method')?.textContent,
    ).toBe('Value overlap: too few decisions yet to judge (3 so far)');
    // No fabricated percentage anywhere in the block.
    expect(block!.textContent).not.toContain('%');
  });

  it('reports zero decisions honestly (count only, no rate)', async () => {
    requestJsonMock.mockResolvedValueOnce({
      ...DIGEST,
      precision: {
        decisions: 0,
        accepted: 0,
        window_days: 30,
        min_decisions: 10,
        by_method: {},
      },
    });

    await render();

    const block = container.querySelector<HTMLElement>('.steward-digest-precision');
    expect(block!.textContent).toBe(
      'Machine-proposed learnings: too few decisions yet to judge (0 so far)',
    );
    expect(container.querySelectorAll('.steward-digest-precision-method').length).toBe(0);
  });

  it('renders nothing at all when the precision block is absent', async () => {
    requestJsonMock.mockResolvedValueOnce(DIGEST); // key absent = unavailable
    await render();
    expect(container.querySelector('.steward-digest-precision')).toBeNull();
    expect(container.textContent).not.toContain('machine-proposed learnings');
    // The rest of the technical expander is untouched.
    expect(container.querySelector('.steward-digest-stats')?.textContent).toBe(
      '3 expired unreviewed · 4 quarantined · 3 staged',
    );
  });
});

