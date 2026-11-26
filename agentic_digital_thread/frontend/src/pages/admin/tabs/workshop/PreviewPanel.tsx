// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <PreviewPanel/> + <LineList/> — the dry-run display shared by the Place
 * and Reset controls: an optional "expired" status, the summary lines, the
 * effect lines under a heading, and the warnings. Test ids hang off
 * `testIdPrefix` (`workshop-preview` / `workshop-reset-preview`). Every
 * line is a literal text node — zero HTML sinks. */
import type { CSSProperties } from 'react';

import { useI18n } from '../../../../lib/i18n';

export function LineList({ lines, testId, style }: { lines: string[]; testId?: string; style?: CSSProperties }) {
  return (
    <ul data-testid={testId} style={style}>
      {lines.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  );
}

export interface PreviewPanelProps {
  testIdPrefix: string;
  expired: boolean;
  summary: string[];
  effectHeading: string;
  effect: string[];
  warnings: string[];
}

export default function PreviewPanel({ testIdPrefix, expired, summary, effectHeading, effect, warnings }: PreviewPanelProps) {
  const { t } = useI18n();
  return (
    <div className="workshop-preview" data-testid={testIdPrefix} data-expired={expired ? 'true' : 'false'}>
      {expired && (
        <div role="status" className="workshop-preview-expired" data-testid={`${testIdPrefix}-expired`}>
          {t('workshop.previewExpired')}
        </div>
      )}
      <LineList lines={summary} testId={`${testIdPrefix}-summary`} />
      <div>{effectHeading}</div>
      <LineList lines={effect} testId={`${testIdPrefix}-effect`} />
      {warnings.length > 0 && (
        <div className="workshop-warnings" data-testid={`${testIdPrefix}-warnings`}>
          <div>{t('workshop.warnings')}</div>
          <LineList lines={warnings} />
        </div>
      )}
    </div>
  );
}
