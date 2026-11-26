// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* chat/trace-id-names.ts: the card's names plugin edits node values only.
 *
 * Run on a hand-built markdown tree: ids in text, inline code and code
 * values (and image alt text) are named; link and image urls, definitions, raw
 * HTML, the visible text of a link that shows its own destination, and a
 * text node another plugin gave an element (a local time) are left as they
 * are. One pass: a name holding another known id is shown as given. */
import { describe, expect, it } from 'vitest';

import { nameIds, readIdNames, remarkIdNames } from '../chat/trace-id-names';

const A = '5f2c8e1a-0b3d-4c6e-9a7f-1d2e3f4a5b6c';
const B = '7a9b0c1d-2e3f-4a5b-8c6d-9e0f1a2b3c4d';
const NAMES = readIdNames({ [A]: 'Test Rig 4', [B]: 'Inlet Pressure' })!;
const NA = 'Test Rig 4 (5f2c8e1a\u2026)';

interface Node {
  type: string;
  value?: string;
  url?: string;
  alt?: string;
  children?: Node[];
  data?: Record<string, unknown>;
}

function run(tree: Node): Node {
  remarkIdNames(NAMES)()(tree);
  return tree;
}

describe('remarkIdNames', () => {
  it('names ids in text, inline code and code values', () => {
    const tree = run({
      type: 'root',
      children: [
        {
          type: 'paragraph',
          children: [
            { type: 'text', value: `Asset ${A} ` },
            { type: 'inlineCode', value: A },
            { type: 'strong', children: [{ type: 'text', value: B }] },
          ],
        },
        { type: 'code', value: `${A},idle` },
      ],
    });
    const p = tree.children![0].children!;
    expect(p[0].value).toBe(`Asset ${NA} `);
    expect(p[1].value).toBe(NA);
    expect(p[2].children![0].value).toBe('Inlet Pressure (7a9b0c1d\u2026)');
    expect(tree.children![1].value).toBe(`${NA},idle`);
  });

  it('leaves urls, definitions, raw HTML, autolink text and local times; names alt text', () => {
    const url = `https://plant.example/assets/${A}`;
    const tree = run({
      type: 'root',
      children: [
        {
          type: 'paragraph',
          children: [
            { type: 'link', url, children: [{ type: 'text', value: `asset ${A}` }] },
            { type: 'link', url, children: [{ type: 'text', value: url }] },
            { type: 'link', url: `http://www.plant.example/${A}`, children: [{ type: 'text', value: `www.plant.example/${A}` }] },
            { type: 'image', url, alt: `rig ${A}` },
            { type: 'html', value: `<span>${A}</span>` },
            { type: 'text', value: A, data: { hName: 'time' } },
          ],
        },
        { type: 'definition', url, children: [] },
      ],
    });
    const p = tree.children![0].children!;
    expect(p[0].url).toBe(url);
    expect(p[0].children![0].value).toBe(`asset ${NA}`);
    expect(p[1].children![0].value).toBe(url);
    expect(p[2].children![0].value).toBe(`www.plant.example/${A}`);
    expect(p[3]).toEqual({ type: 'image', url, alt: `rig ${NA}` });
    expect(p[4].value).toBe(`<span>${A}</span>`);
    expect(p[5].value).toBe(A);
    expect(tree.children![1].url).toBe(url);
  });

  it('names an explicit link label that is only the end of its destination', () => {
    const url = `https://plant.example/assets/${A}`;
    const tree = run({
      type: 'root',
      children: [{ type: 'paragraph', children: [{ type: 'link', url, children: [{ type: 'text', value: A }] }] }],
    });
    const link = tree.children![0].children![0];
    expect(link.url).toBe(url);
    expect(link.children![0].value).toBe(NA);
  });

  it('leaves a link only when its text is its destination as GFM writes it', () => {
    const shown = (url: string, label: string) =>
      run({ type: 'root', children: [{ type: 'link', url, children: [{ type: 'text', value: label }] }] })
        .children![0].children![0].value;
    const site = `plant.example/${A}`;
    expect(shown(`https://${site}`, `https://${site}`)).toBe(`https://${site}`);
    expect(shown(`http://www.${site}`, `www.${site}`)).toBe(`www.${site}`);
    expect(shown(`mailto:${A}@plant.example`, `${A}@plant.example`)).toBe(`${A}@plant.example`);
    expect(shown(`https://${site}`, `${site}`)).toBe(`plant.example/${NA}`);
  });
});

describe('nameIds', () => {
  it('is one pass: a name holding another known id is shown as given', () => {
    const names = readIdNames({ [A]: `Rig ${B}`, [B]: 'Inlet Pressure' })!;
    expect(nameIds(`Asset ${A} reads ${B}.`, names)).toBe(
      `Asset Rig ${B} (5f2c8e1a\u2026) reads Inlet Pressure (7a9b0c1d\u2026).`,
    );
  });

  it('inserts a name holding replacement patterns as it is', () => {
    const names = readIdNames({ [A]: "$& $1 $' $`" })!;
    expect(nameIds(`x ${A} y`, names)).toBe("x $& $1 $' $` (5f2c8e1a\u2026) y");
  });
});

describe('readIdNames', () => {
  it('keeps only id-shaped keys with a usable string name, at most 50', () => {
    expect(readIdNames(null)).toBeNull();
    expect(readIdNames(['x'])).toBeNull();
    expect(readIdNames({ 'not-an-id': 'x', [A]: '', [B]: B })).toBeNull();
    const many = Object.fromEntries(
      Array.from({ length: 60 }, (_, i) => [`${String(i).padStart(8, '0')}-0b3d-4c6e-9a7f-1d2e3f4a5b6c`, `n${i}`]),
    );
    expect(readIdNames(many)!.size).toBe(50);
  });
});
