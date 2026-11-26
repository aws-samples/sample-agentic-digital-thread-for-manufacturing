// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <PhaseBar/> — the live story's loop as 4 proportional segments in server
 * loop order, with a marker at `loop_fraction`. role="img" + an aria-label
 * sentence carries the meaning; every segment also shows its text label and
 * the current one is outlined + bold, so phase is never colour-only. */
import type { PhaseTimeline } from '../../../../api/types/workshop';
import { useI18n } from '../../../../lib/i18n';
import { phaseLabel } from '../../../../features/workshop/clock';
import { phaseBarAriaLabel, phaseBarModel } from './workshop-logic';

export default function PhaseBar({ timeline }: { timeline: PhaseTimeline }) {
  const { t } = useI18n();
  const model = phaseBarModel(timeline);
  return (
    <div
      className="workshop-phase-bar"
      role="img"
      aria-label={phaseBarAriaLabel(timeline, t)}
      data-testid="workshop-phase-bar"
    >
      {model.segments.map((segment) => (
        <div
          key={segment.phase}
          className={`workshop-phase-segment workshop-phase-${segment.phase}${segment.current ? ' current' : ''}`}
          style={{ left: `${segment.start * 100}%`, width: `${segment.width * 100}%` }}
          data-testid={`workshop-phase-segment-${segment.phase}`}
          data-current={segment.current ? 'true' : 'false'}
          aria-hidden="true"
        >
          <span className="workshop-phase-label">{phaseLabel(segment.phase, t)}</span>
        </div>
      ))}
      <div
        className="workshop-phase-marker"
        style={{ left: `${model.marker * 100}%` }}
        data-testid="workshop-phase-marker"
        aria-hidden="true"
      />
    </div>
  );
}
