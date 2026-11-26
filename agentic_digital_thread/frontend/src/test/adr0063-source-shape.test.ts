// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { readFileSync } from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(process.cwd(), 'src');
const PRODUCTION_FILES = [
  'views/architecture/SlCards.tsx',
  'views/architecture/proposal-trust.tsx',
  'views/architecture/proposal-families.tsx',
  'pages/admin/admin-queries.ts',
  'pages/admin/guidance-queries.ts',
  'pages/admin/tabs/vocabulary/GuidanceStabilityControls.tsx',
  'pages/admin/tabs/vocabulary/guidance-stability-ui.tsx',
  'pages/admin/tabs/vocabulary/ReconciliationPanel.tsx',
  'pages/admin/tabs/vocabulary/GuidanceLossDialog.tsx',
  'pages/admin/tabs/vocabulary/GuidanceProposalCard.tsx',
  'pages/admin/tabs/vocabulary/PlacementControl.tsx',
] as const;

function sourceOf(relativePath: string): ts.SourceFile {
  const fullPath = path.join(ROOT, relativePath);
  return ts.createSourceFile(
    fullPath,
    readFileSync(fullPath, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    relativePath.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function declarationNames(source: ts.SourceFile): string[] {
  const names: string[] = [];
  source.forEachChild((node) => {
    if (ts.isFunctionDeclaration(node) && node.name) names.push(node.name.text);
    if (ts.isVariableStatement(node)) {
      for (const item of node.declarationList.declarations) {
        if (ts.isIdentifier(item.name)) names.push(item.name.text);
      }
    }
  });
  return names;
}

function oversizedFunctions(source: ts.SourceFile): string[] {
  const offenders: string[] = [];
  function visit(node: ts.Node) {
    if (ts.isFunctionLike(node)) {
      const start = source.getLineAndCharacterOfPosition(node.getStart(source)).line;
      const end = source.getLineAndCharacterOfPosition(node.getEnd()).line;
      if (end - start + 1 > 50) offenders.push(`${source.fileName}:${start + 1}-${end + 1}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return offenders;
}

function lineCount(source: ts.SourceFile): number {
  return source.getLineAndCharacterOfPosition(source.getEnd()).line + 1;
}

describe('ADR-0063 frontend source shape', () => {
  it('moves trust/family and guidance query ownership into focused modules', () => {
    const cards = declarationNames(sourceOf('views/architecture/SlCards.tsx'));
    expect(cards).not.toContain('trustChipState');
    expect(cards).not.toContain('FamilyBatchHeader');

    const admin = declarationNames(sourceOf('pages/admin/admin-queries.ts'));
    expect(admin).not.toContain('guidancePreviewQueryOptions');
    expect(admin).not.toContain('contextPlacementJobQueryOptions');
  });

  it('shrinks prior large files and keeps every new file bounded', () => {
    expect(lineCount(sourceOf('views/architecture/SlCards.tsx'))).toBeLessThan(1175);
    expect(lineCount(sourceOf('pages/admin/admin-queries.ts'))).toBeLessThan(993);
    for (const relativePath of PRODUCTION_FILES.slice(1)) {
      expect(lineCount(sourceOf(relativePath)), relativePath).toBeLessThan(800);
    }
  });

  it('keeps every function in the touched production surface at 50 lines or fewer', () => {
    const offenders = PRODUCTION_FILES.slice(1).flatMap((relativePath) =>
      oversizedFunctions(sourceOf(relativePath)),
    );
    expect(offenders).toEqual([]);
  });
});
