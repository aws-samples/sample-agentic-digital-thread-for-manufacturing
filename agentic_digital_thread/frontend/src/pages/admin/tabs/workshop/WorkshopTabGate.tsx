// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Self-gating Workshop tab host (ADR-0091 D4): AdminPage inserts these two
 * components as JSX. Each observes the SHARED workshop story query (enabled
 * for admins only; the GET is steward-gated) and renders NOTHING unless it
 * holds a clock: a 403/404 (not a live workshop), loading with no data, and
 * the frozen story (`clock: null`) all stay invisible and silent. A transient
 * refetch failure keeps the last good clock (clock.clockGate). The section
 * mounts <WorkshopTab/> on first activation and keeps it mounted (the other
 * admin tabs' idiom); AdminPage falls back to its default tab whenever the
 * gate settles 'hidden' on #tab-workshop (admin-logic.workshopTabFallback). */
import { useEffect, useState } from 'react';

import { useWorkshopStory } from '../../../../features/workshop/useWorkshopStory';
import { useI18n } from '../../../../lib/i18n';
import WorkshopTab from '../WorkshopTab';

export const WORKSHOP_TAB_ID = 'tab-workshop' as const;

/** true when the shared story query holds a live clock for this admin. */
function useWorkshopLive(): boolean {
  return useWorkshopStory().gate === 'live';
}

export function WorkshopTabButton({ active, onActivate }: { active: boolean; onActivate: () => void }) {
  const { t } = useI18n();
  const live = useWorkshopLive();
  if (!live) return null;
  return (
    <button
      type="button"
      className={active ? 'active' : undefined}
      data-tab={WORKSHOP_TAB_ID}
      data-testid="workshop-tab-button"
      onClick={onActivate}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="12" cy="12" r="10" />
        <polyline points="12 6 12 12 16 14" />
      </svg>
      {t('admin.tabWorkshop')}
    </button>
  );
}

export function WorkshopTabSection({ active }: { active: boolean }) {
  const live = useWorkshopLive();
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (active && live) setMounted(true);
  }, [active, live]);
  if (!live) return null;
  return (
    <section id={WORKSHOP_TAB_ID} className={'tab-panel' + (active ? ' active' : '')}>
      {(mounted || active) && <WorkshopTab />}
    </section>
  );
}
