// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { KeyboardEvent } from 'react';

import '../../styles/async-states.css';

interface AsyncVisualProps {
  compact?: boolean;
  className?: string;
}

function classNames(base: string, compact: boolean, className?: string): string {
  return [base, compact ? 'async-state--compact' : '', className || ''].filter(Boolean).join(' ');
}

export interface SkeletonProps extends AsyncVisualProps {
  /** Number of placeholder rows. Values below one render one block. */
  lines?: number;
}

/** Decorative placeholder. Its shape is hidden from assistive technology. */
export function Skeleton({ lines = 1, compact = false, className }: SkeletonProps) {
  const lineCount = Math.max(1, Math.floor(lines));
  return (
    <div
      className={classNames('async-skeleton', compact, className)}
      aria-hidden="true"
      data-testid="async-skeleton"
    >
      {Array.from({ length: lineCount }, (_, index) => (
        <span key={index} className="async-skeleton__line" />
      ))}
    </div>
  );
}

export interface LoadingStateProps extends AsyncVisualProps {
  label: string;
}

export function LoadingState({ label, compact = false, className }: LoadingStateProps) {
  return (
    <div
      className={classNames('async-state async-state--loading', compact, className)}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <Skeleton lines={compact ? 1 : 3} compact={compact} />
      <span className="async-state__message">{label}</span>
    </div>
  );
}

/**
 * Background-refresh indicator that floats over content instead of joining
 * the layout — mounting/unmounting it never pushes content around. The
 * zero-height `.async-state-anchor` wrapper is the positioning context, so
 * callers need no CSS on their own containers. Place it directly above the
 * content it describes.
 */
export function RefreshingOverlay({ label, className }: LoadingStateProps) {
  return (
    <div className="async-state-anchor">
      <LoadingState
        label={label}
        compact
        className={[className, 'async-state--overlay'].filter(Boolean).join(' ')}
      />
    </div>
  );
}

export interface ErrorStateProps extends AsyncVisualProps {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
  /** Reserve assertive announcements for errors that require immediate attention. */
  urgent?: boolean;
}

export function ErrorState({
  message,
  onRetry,
  retryLabel = 'Retry',
  urgent = false,
  compact = false,
  className,
}: ErrorStateProps) {
  const activateRetry = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      event.currentTarget.click();
    }
  };

  return (
    <div
      className={classNames('async-state async-state--error', compact, className)}
      role={urgent ? 'alert' : 'status'}
      aria-live={urgent ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      <span className="async-state__message">{message}</span>
      {onRetry ? (
        <button
          type="button"
          className="async-state__action"
          onClick={onRetry}
          onKeyDown={activateRetry}
        >
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

export interface EmptyStateProps extends AsyncVisualProps {
  message: string;
  reason?: 'no-items' | 'not-configured';
  actionLabel?: string;
  onAction?: () => void;
}

export function EmptyState({
  message,
  reason,
  actionLabel,
  onAction,
  compact = false,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={classNames('async-state async-state--empty', compact, className)}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-reason={reason}
    >
      <span className="async-state__message">{message}</span>
      {actionLabel && onAction ? (
        <button type="button" className="async-state__action" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
}

export interface NoDataStateProps extends AsyncVisualProps {
  message: string;
}

/** Successful resolution with no samples: distinct from empty and unavailable. */
export function NoDataState({ message, compact = false, className }: NoDataStateProps) {
  return (
    <div
      className={classNames('async-state async-state--no-data', compact, className)}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-testid="async-no-data"
    >
      <span className="async-state__marker" aria-hidden="true" />
      <span className="async-state__message">{message}</span>
    </div>
  );
}
