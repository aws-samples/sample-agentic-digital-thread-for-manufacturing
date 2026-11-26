// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useRef, type FocusEvent, type KeyboardEvent, type MouseEvent } from 'react';

import { useStore } from '../../lib/store';
import '../../styles/toast.css';
import {
  dismiss,
  pauseToast,
  resumeToast,
  toastStore,
  type Toast,
} from './toast-store';

function ToastCard({ toast }: { toast: Toast }) {
  const interaction = useRef({ hovered: false, focused: false });

  const resumeIfIdle = () => {
    if (!interaction.current.hovered && !interaction.current.focused) {
      resumeToast(toast.id);
    }
  };

  const handleMouseEnter = (_event: MouseEvent<HTMLDivElement>) => {
    interaction.current.hovered = true;
    pauseToast(toast.id);
  };
  const handleMouseLeave = (_event: MouseEvent<HTMLDivElement>) => {
    interaction.current.hovered = false;
    resumeIfIdle();
  };
  const handleFocus = (_event: FocusEvent<HTMLDivElement>) => {
    interaction.current.focused = true;
    pauseToast(toast.id);
  };
  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) {
      return;
    }
    interaction.current.focused = false;
    resumeIfIdle();
  };
  const handleDismissKey = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      event.stopPropagation();
      dismiss(toast.id);
    }
  };

  // Activatable toast (onActivate): the card behaves as a button — click /
  // Enter / Space runs the action and dismisses. The dismiss × stays an
  // independent control (stopPropagation keeps it from activating).
  const activate = () => {
    toast.onActivate?.();
    dismiss(toast.id);
  };
  const handleActivateKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate();
    }
  };
  const interactiveProps = toast.onActivate
    ? {
        role: 'button' as const,
        tabIndex: 0,
        onClick: activate,
        onKeyDown: handleActivateKey,
      }
    : {};

  return (
    <div
      className={`toast toast-${toast.kind}${toast.onActivate ? ' toast-activatable' : ''}`}
      data-toast-id={toast.id}
      data-toast-kind={toast.kind}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      onFocusCapture={handleFocus}
      onBlurCapture={handleBlur}
      // Reviewed (react-props-spreading): interactiveProps is a local constant
      // of typed a11y/interaction handlers, not caller-supplied attributes.
      // nosemgrep
      {...interactiveProps /* nosemgrep */}
    >
      <span className="toast-message">{toast.message}</span>
      <button
        type="button"
        className="toast-dismiss"
        aria-label={`Dismiss ${toast.kind} notification`}
        onClick={(event) => {
          event.stopPropagation();
          dismiss(toast.id);
        }}
        onKeyDown={handleDismissKey}
      >
        ×
      </button>
    </div>
  );
}

/** App-level polite and urgent live regions for the shared toast queue. */
export function ToastRegion() {
  const toasts = useStore(toastStore, (state) => state.toasts);
  const urgent = toasts.filter((toast) => toast.kind === 'error' && toast.urgent);
  const polite = toasts.filter((toast) => !(toast.kind === 'error' && toast.urgent));

  return (
    <div className="toast-regions">
      <div className="toast-region" role="status" aria-live="polite" aria-atomic="false">
        {polite.map((toast) => (
          <ToastCard key={toast.id} toast={toast} />
        ))}
      </div>
      {urgent.length > 0 ? (
        <div className="toast-region toast-region-urgent" role="alert" aria-live="assertive">
          {urgent.map((toast) => (
            <ToastCard key={toast.id} toast={toast} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
