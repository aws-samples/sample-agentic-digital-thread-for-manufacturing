// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Source tripwire: no native browser dialog in src/ (outside src/test/).
 * Prompts go through askConfirm() (shared/ui/confirm-store.ts) and messages
 * through the toast store (shared/ui/toast-store.ts push()).
 *
 * The scan uses the TypeScript TYPE CHECKER over the project's own tsconfig,
 * not syntax. A reference counts when it RESOLVES to the DOM library's
 * `confirm` / `alert` (lib.dom.d.ts: the global functions and the Window
 * members), so the ordinary spellings that reach the browser dialog count
 * and a local `confirm` (a parameter, a callback, an import, an object key) never
 * does. It flags:
 *
 *   - a bare or aliased reference: `confirm(…)`, `confirm.call(…)`,
 *     `(0, confirm)(…)`, `const c = confirm`, `setTimeout(alert)`, `{ confirm }`;
 *   - a member of ANY value whose type carries the dialog: `window.confirm`,
 *     `window!.alert`, `const w = window; w.confirm`, `self?.['alert']`,
 *     `function f(w: Window) { w.confirm() }`, `const { confirm: c } = window`;
 *   - the same through a type cast: `(window as any).confirm`,
 *     `(<any>globalThis)['alert']`, `(window as unknown as X).confirm`, and a
 *     variable initialised with such a cast (`const w = window as any`);
 *   - a DYNAMIC computed member of such a value (`window['con' + 'firm']`,
 *     `globalThis[k]`): it cannot be proven safe and src/ has no use for one;
 *   - a RETYPING: a dialog-carrying value handed to a slot whose declared
 *     type has its own `confirm`/`alert` (`const w: Cap = window`, a
 *     `{ w: window }` field, `return window` from a `(): Cap`, an argument,
 *     `({ confirm: c } = window)`): past that point the member no longer
 *     resolves to lib.dom, so the hand-off itself is reported.
 *
 * The scanned set is every script found by WALKING src/ (minus src/test/),
 * not tsconfig's `include`, and config diagnostics fail the test.
 *
 * Limit, by design: a value whose static type is `any`/`unknown` from the
 * start (a parameter typed `any`, `JSON.parse(…)`), a cast to `any` held in a
 * field or returned from a function, `Reflect.get`, and `eval` carry nothing
 * to resolve. The strict tsconfig and code review cover those; this is a
 * regression tripwire, not a sandbox.
 *
 * ALLOWED is an exact file -> count map with the reason. */
import { readdirSync } from 'node:fs';
import path from 'node:path';

import ts from 'typescript';
import { beforeAll, describe, expect, it } from 'vitest';

const ROOT = process.cwd();
const SRC = path.join(ROOT, 'src') + path.sep;
const TEST_DIR = path.join(ROOT, 'src', 'test') + path.sep;
const FIXTURE_DIR = path.join(ROOT, '__native_dialog_fixtures__');
const NAMES = new Set(['confirm', 'alert']);
const DOM_LIB = /[\\/]lib\.dom\.d\.ts$/;

const ALLOWED: Record<string, { count: number; reason: string; mustContain: RegExp }> = {
  'shared/ui/confirm-store.ts': {
    // `typeof window.confirm !== 'function'` guard + the window.confirm call.
    count: 2,
    reason: 'documented window.confirm fallback when no <ConfirmHost/> is mounted',
    mustContain: /window\.confirm\(`\$\{title\}/,
  },
};

/** [code, expected hits]. Each runs as its own module in the same program. */
const FIXTURES: ReadonlyArray<readonly [string, number]> = [
  // Direct and global-object forms.
  ["if (!confirm('Delete?')) throw new Error();", 1],
  ["alert('Saved');", 1],
  ["window.confirm('x'); window.alert('y');", 2],
  ["globalThis.confirm('x')", 1],
  ["window['confirm']('x')", 1],
  ['window?.confirm("x")', 1],
  ["globalThis['alert']('x')", 1],
  ["self?.alert('x')", 1],
  ["self?.['alert']('x')", 1],
  ['(window).confirm(`x`)', 1],
  ["confirm?.('x')", 1],
  // Aliases and indirect calls.
  ['const ask = window.confirm; ask("x");', 1],
  ["const c = confirm; c('x');", 1],
  ["confirm.call(window, 'x')", 1],
  ["(0, confirm)('x')", 1],
  ['setTimeout(alert, 0)', 1],
  ['const o = { confirm };', 1],
  ["const w = window; w.confirm('x');", 1],
  ["function f(w: Window) { w.confirm('x'); }", 1],
  ["const { confirm: c } = window; c('x');", 1],
  ['const { alert } = globalThis;', 1],
  // Casts and cast-initialised aliases.
  ["(window as any).confirm('x')", 1],
  ["window!.alert('x')", 1],
  ["(<any>globalThis)['alert']('x')", 1],
  ["(window satisfies Window).confirm('x')", 1],
  ["(window as unknown as { confirm: (m: string) => boolean }).confirm('x')", 1],
  ["const w = window as any; w.confirm('x');", 1],
  ["const w = window as unknown as Record<string, unknown>; w['alert'];", 1],
  ["const { confirm: c } = window as any; c('x');", 1],
  // Retyping a dialog-carrying value into a type with its own member.
  ["type Cap = { confirm(m?: string): boolean }; const w: Cap = window; w.confirm('x');", 1],
  ["type Cap = { confirm(m?: string): boolean }; const b: { w: Cap } = { w: window }; b.w.confirm('x');", 1],
  ["function dialogs(): { alert(m?: unknown): void } { return window; } dialogs().alert('x');", 1],
  ["let c = () => false; ({ confirm: c } = window); c();", 1],
  ["function use(x: { confirm(m?: string): boolean }) { return x.confirm('x'); } use(globalThis);", 1],
  ["const w = window; const narrowed: Pick<Window, 'confirm'> = w; narrowed.confirm('x');", 1],
  // ...through a cast, non-null or satisfies around the handed-off value.
  ["function use(x: { confirm(m?: string): boolean }) { return x.confirm('x'); } use(window as Window);", 1],
  ["function use(x: { confirm(m?: string): boolean }) { return x.confirm('x'); } use(window!);", 1],
  ["function use(x: { confirm(m?: string): boolean }) { return x.confirm('x'); } use(window satisfies Window);", 1],
  // Two hits: the hand-off, and `c.alert` (c was initialised with a cast).
  ["type Cap = { alert(m?: unknown): void }; const c: Cap = (window) as Cap; c.alert('x');", 2],
  ['declare const f: boolean; const c: { confirm(): boolean } = (f ? window : globalThis) as Window;', 2],
  // Handing the global on under its own type keeps lib.dom: no retyping.
  ["function keep(x: Window) { return x; } keep(window as Window); const w2: Window = window!;", 0],
  // Dynamic keys on a dialog-carrying value.
  ["window['con' + 'firm']('x')", 1],
  ['const k = "alert"; globalThis[k as "alert"]("x")', 1],
  ['declare const k: string; (window as any)[k]', 1],
  // Not native dialogs: locals, keys, members of other types, imports, types.
  ["const confirm = (v: boolean) => v; confirm(true); setTimeout(confirm, 0);", 0],
  ["function g(confirm: () => void) { confirm(); }", 0],
  ['declare const props: { confirm: () => void }; const { confirm } = props; confirm();', 0],
  ["declare const obj: { confirm: number }; const o = { confirm: 1 }; obj.confirm = 2; obj['confirm'];", 0],
  ['interface I { confirm(): void; alert: string } type T = I["confirm"];', 0],
  ["import { confirm } from './x'; confirm();", 0],
  ["declare const dialog: { confirm(): void }; dialog.confirm(); const arr = [1]; arr[0];", 0],
  ["const g2 = globalThis as Record<string, unknown>; g2.handleDemo;", 0],
  ["// confirm('x')\n/* alert('y') */\nconst a = 1;", 0],
  ["const s = \"confirm('x') and window.alert('y')\";", 0],
  ["const url = 'https://example.com'; confirm('z');", 1],
];

const fixturePath = (i: number) => path.join(FIXTURE_DIR, `f${i}.ts`);
const VIRTUAL = new Map<string, string>([
  [path.join(FIXTURE_DIR, 'x.ts'), 'export function confirm(): boolean { return true; }\n'],
  ...FIXTURES.map(([code], i) => [fixturePath(i), `${code}\nexport {};\n`] as const),
]);

function isDomDialog(checker: ts.TypeChecker, symbol: ts.Symbol | undefined): boolean {
  let s = symbol;
  if (s && s.flags & ts.SymbolFlags.Alias) s = checker.getAliasedSymbol(s);
  return !!s && NAMES.has(s.name) && (s.declarations ?? []).some((d) => DOM_LIB.test(d.getSourceFile().fileName));
}

/** Strip parentheses, `as`, `<T>`, `!` and `satisfies`. */
function unwrap(e: ts.Expression): ts.Expression {
  for (;;) {
    if (
      ts.isParenthesizedExpression(e) ||
      ts.isAsExpression(e) ||
      ts.isTypeAssertionExpression(e) ||
      ts.isNonNullExpression(e) ||
      ts.isSatisfiesExpression(e)
    ) {
      e = e.expression;
    } else {
      return e;
    }
  }
}

/** The expression that occupies a typed slot when `e` is a value handed to
 * one, else null. Climbs what does not change WHERE the value goes: parens,
 * casts (`as`, `<T>`, `!`, `satisfies`), a `?:` branch and the right side of
 * `??`/`||`/`&&`. A slot is a variable initialiser (not a destructuring one:
 * the binding rule judges those), a property value, a return value or arrow
 * body, an assignment's right side, a call argument, or an array element. */
function flowSlot(e: ts.Expression): ts.Expression | null {
  let node: ts.Expression = e;
  for (;;) {
    const p: ts.Node = node.parent;
    const climbs =
      ((ts.isParenthesizedExpression(p) ||
        ts.isAsExpression(p) ||
        ts.isTypeAssertionExpression(p) ||
        ts.isNonNullExpression(p) ||
        ts.isSatisfiesExpression(p)) &&
        p.expression === node) ||
      (ts.isConditionalExpression(p) && p.condition !== node) ||
      (ts.isBinaryExpression(p) &&
        p.right === node &&
        [ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.AmpersandAmpersandToken].includes(
          p.operatorToken.kind,
        ));
    if (!climbs) break;
    node = p as ts.Expression;
  }
  const p = node.parent;
  const inSlot =
    (ts.isVariableDeclaration(p) && p.initializer === node && ts.isIdentifier(p.name)) ||
    (ts.isPropertyAssignment(p) && p.initializer === node) ||
    ts.isReturnStatement(p) ||
    (ts.isArrowFunction(p) && p.body === node) ||
    (ts.isBinaryExpression(p) && p.right === node && p.operatorToken.kind === ts.SyntaxKind.EqualsToken) ||
    ((ts.isCallExpression(p) || ts.isNewExpression(p)) && (p.arguments?.some((a) => a === node) ?? false)) ||
    ts.isArrayLiteralExpression(p);
  return inSlot ? node : null;
}

/** Scans one source file of a type-checked program. */
function makeScanner(checker: ts.TypeChecker) {
  /** The dialog member `name` of the value `e` evaluates to, looking through
   * casts and through a variable that was initialised with a cast. */
  const memberIsDialog = (e: ts.Expression, name: string): boolean => {
    let operand = unwrap(e);
    if (ts.isIdentifier(operand)) {
      const decl = checker.getSymbolAtLocation(operand)?.valueDeclaration;
      if (decl && ts.isVariableDeclaration(decl) && decl.initializer && unwrap(decl.initializer) !== decl.initializer) {
        operand = unwrap(decl.initializer);
      }
    }
    const type = checker.getNonNullableType(checker.getTypeAtLocation(operand));
    return isDomDialog(checker, checker.getPropertyOfType(type, name));
  };
  const carriesDialog = (e: ts.Expression) => [...NAMES].some((n) => memberIsDialog(e, n));

  /** A dialog-carrying value flowing into a slot whose declared type has its
   * OWN `confirm`/`alert` (`const w: Cap = window`, `{ w: window }` for a
   * `{ w: Cap }`, `return window` from a `(): Cap`, `({ confirm: c } =
   * window)`, `use(window)` for a `(x: Cap)` parameter): after this point
   * the member resolves to that type, not lib.dom, so the RETYPING is where
   * the native dialog is laundered and where it is reported. Only values in
   * a flow position are judged, and the (rare) slot check runs first. */
  const retypesDialog = (e: ts.Expression): boolean => {
    // The slot's type is read at the OUTERMOST expression in it, so
    // `use(window as Window)` is judged against `use`'s parameter type.
    const occupant = flowSlot(e);
    if (!occupant) return false;
    const slot = checker.getContextualType(occupant);
    if (!slot) return false;
    const target = checker.getNonNullableType(slot);
    const ownMember = [...NAMES].some((n) => {
      const member = checker.getPropertyOfType(target, n);
      return !!member && !isDomDialog(checker, member);
    });
    return ownMember && carriesDialog(e);
  };

  return (sf: ts.SourceFile): string[] => {
    const hits: string[] = [];
    const report = (n: ts.Node) => hits.push(n.getText(sf).replace(/\s+/g, ' ').slice(0, 80));
    const visit = (node: ts.Node): void => {
      if (ts.isPropertyAccessExpression(node) && NAMES.has(node.name.text)) {
        if (isDomDialog(checker, checker.getSymbolAtLocation(node.name)) || memberIsDialog(node.expression, node.name.text)) {
          report(node);
        }
      } else if (ts.isElementAccessExpression(node)) {
        const key = node.argumentExpression;
        if (ts.isStringLiteralLike(key)) {
          if (NAMES.has(key.text) && memberIsDialog(node.expression, key.text)) report(node);
        } else if (!ts.isNumericLiteral(key) && carriesDialog(node.expression)) {
          report(node);
        }
      } else if (ts.isBindingElement(node) && ts.isObjectBindingPattern(node.parent)) {
        const key = node.propertyName ?? node.name;
        const decl = node.parent.parent;
        const source =
          ts.isVariableDeclaration(decl) && decl.initializer ? decl.initializer : (node.parent as unknown as ts.Expression);
        if (ts.isIdentifier(key) && NAMES.has(key.text) && memberIsDialog(source, key.text)) report(node);
      } else if (
        ts.isIdentifier(node) &&
        NAMES.has(node.text) &&
        !ts.isBindingElement(node.parent) &&
        // `x.confirm`'s name was judged with its member access above.
        !(ts.isPropertyAccessExpression(node.parent) && node.parent.name === node)
      ) {
        const p = node.parent;
        const symbol =
          ts.isShorthandPropertyAssignment(p) && p.name === node
            ? checker.getShorthandAssignmentValueSymbol(p)
            : checker.getSymbolAtLocation(node);
        if (isDomDialog(checker, symbol)) report(ts.isCallExpression(p) && p.expression === node ? p : node);
      }
      // A value (an identifier, member or call result: `window`, `w`,
      // `ctx.win`, `getWindow()`) retyped into a slot with its own dialog.
      if (
        (ts.isIdentifier(node) || ts.isPropertyAccessExpression(node) || ts.isCallExpression(node)) &&
        retypesDialog(node)
      ) {
        report(node);
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
    return hits;
  };
}

const rel = (file: string) => path.relative(SRC, path.resolve(file)).split(path.sep).join('/');

/** Every script under src/ (not src/test/), found by walking the directory,
 * NOT from tsconfig: a narrowed `include` must not quietly shrink the scan. */
function walkSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (full + path.sep !== TEST_DIR) out.push(...walkSources(full));
    } else if (/\.(ts|tsx|js|jsx|mjs|cjs)$/.test(entry.name)) {
      out.push(path.resolve(full));
    }
  }
  return out;
}

describe('no native confirm()/alert() in src/', () => {
  let program: ts.Program;
  let scan: (sf: ts.SourceFile) => string[];
  let srcFiles: ts.SourceFile[];
  let walked: string[];
  let configDiagnostics: readonly ts.Diagnostic[];
  /** Real-tree scan results, computed in beforeAll (its budget, not a test's:
   * the checker resolves lazily, so the first full scan pays for checking). */
  let realHits: Map<string, string[]>;

  beforeAll(() => {
    const configPath = path.join(ROOT, 'tsconfig.json');
    const config = ts.readConfigFile(configPath, ts.sys.readFile);
    const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, ROOT);
    configDiagnostics = [...(config.error ? [config.error] : []), ...parsed.errors];
    walked = walkSources(SRC);
    const host = ts.createCompilerHost(parsed.options, true);
    const { getSourceFile, fileExists, readFile } = host;
    host.fileExists = (f) => VIRTUAL.has(path.resolve(f)) || fileExists.call(host, f);
    host.readFile = (f) => VIRTUAL.get(path.resolve(f)) ?? readFile.call(host, f);
    host.getSourceFile = (f, lang, ...rest) => {
      const text = VIRTUAL.get(path.resolve(f));
      return text !== undefined
        ? ts.createSourceFile(f, text, lang, true, ts.ScriptKind.TS)
        : getSourceFile.call(host, f, lang, ...rest);
    };
    // Roots: the walked tree (independent of `include`), plus whatever the
    // config adds (ambient .d.ts, vite.config.ts), plus the fixtures. The
    // config still supplies the compiler options (lib, jsx, strict).
    const roots = [...new Set([...walked, ...parsed.fileNames.map((f) => path.resolve(f))])];
    program = ts.createProgram([...roots, ...VIRTUAL.keys()], { ...parsed.options, allowJs: true }, host);
    scan = makeScanner(program.getTypeChecker());
    srcFiles = program
      .getSourceFiles()
      .filter((sf) => path.resolve(sf.fileName).startsWith(SRC) && !path.resolve(sf.fileName).startsWith(TEST_DIR));
    realHits = new Map(srcFiles.map((sf) => [rel(sf.fileName), scan(sf)]));
  }, 120_000);

  it('type-checks EVERY script under src/ against the DOM library', () => {
    expect(configDiagnostics.map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'))).toEqual([]);
    expect(walked.length).toBeGreaterThan(100);
    // Exact coverage: every walked script is in the program and scanned.
    const scanned = new Set(srcFiles.map((sf) => path.resolve(sf.fileName)));
    expect(walked.filter((f) => !scanned.has(f))).toEqual([]);
    expect(srcFiles.some((sf) => rel(sf.fileName) === 'shared/ui/confirm-store.ts')).toBe(true);
    // Without lib.dom nothing could resolve to the native dialog: fail closed.
    expect(program.getSourceFiles().some((sf) => DOM_LIB.test(sf.fileName))).toBe(true);
  });

  it('has no native dialog reference outside the allowlist', () => {
    const offenders = [...realHits]
      .filter(([r, hits]) => hits.length > 0 && !(r in ALLOWED))
      .map(([r, hits]) => `${r}: ${hits.join(', ')}`);
    expect(offenders, 'use askConfirm() or a toast instead').toEqual([]);
  });

  it.each(Object.entries(ALLOWED))('%s keeps exactly its allowlisted hits', (r, entry) => {
    const sf = srcFiles.find((f) => rel(f.fileName) === r);
    expect(sf, entry.reason).toBeDefined();
    expect(sf!.getFullText(), entry.reason).toMatch(entry.mustContain);
    expect(realHits.get(r), entry.reason).toHaveLength(entry.count);
  });

  it.each(FIXTURES.map(([code, expected], i) => [code, expected, i] as const))(
    '%s → %i hit(s)',
    (_code, expected, i) => {
      const sf = program.getSourceFile(fixturePath(i));
      expect(sf).toBeDefined();
      expect(scan(sf!)).toHaveLength(expected);
    },
  );
});
