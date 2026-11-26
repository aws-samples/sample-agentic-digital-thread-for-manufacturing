// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useId, type HTMLAttributes, type ReactNode } from 'react';

import { useDialogFocus, type DialogFocusTarget } from './useDialogFocus';

type InertDivAttributes = HTMLAttributes<HTMLDivElement> & { inert?: '' };

type DialogLabel =
  | { labelledBy: string; label?: never }
  | { label: string; labelledBy?: never };

export type ModalSurfaceProps = Omit<
  HTMLAttributes<HTMLDivElement>,
  'aria-label' | 'aria-labelledby' | 'aria-modal' | 'role'
> &
  DialogLabel & {
    open: boolean;
    onClose: () => void;
    initialFocus?: DialogFocusTarget;
    closeOnOverlayClick?: boolean;
    /** Preserve contractual DOM markup while closed; closed content is inert and hidden from assistive technology. */
    keepMounted?: boolean;
    children: ReactNode;
  };

let bodyScrollLockCount = 0;
let bodyOverflowBeforeLock = '';

function lockBodyScroll(): () => void {
  const body = document.body;
  if (bodyScrollLockCount === 0) {
    bodyOverflowBeforeLock = body.style.overflow;
    body.style.overflow = 'hidden';
  }
  bodyScrollLockCount += 1;

  return () => {
    bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
    if (bodyScrollLockCount === 0) {
      body.style.overflow = bodyOverflowBeforeLock;
      bodyOverflowBeforeLock = '';
    }
  };
}
const HIDDEN_LABEL_STYLE = {
  position: 'absolute',
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: 'hidden',
  clip: 'rect(0, 0, 0, 0)',
  whiteSpace: 'nowrap',
  border: 0,
} as const;

/** JSX-only dialog/overlay surface with shared focus and dismissal behavior. */
export function ModalSurface({
  open,
  onClose,
  initialFocus,
  closeOnOverlayClick = false,
  keepMounted = false,
  labelledBy,
  label,
  children,
  onClick,
  ...props
}: ModalSurfaceProps) {
  const generatedId = `modal-label-${useId().replace(/:/g, '')}`;
  const labelId = labelledBy ?? generatedId;
  const surfaceRef = useDialogFocus<HTMLDivElement>({ open, onClose, initialFocus });

  useEffect(() => {
    if (!open || typeof document === 'undefined') return;
    return lockBodyScroll();
  }, [open]);

  const accessibilityProps: InertDivAttributes = open
    ? {
        role: 'dialog',
        'aria-modal': 'true',
        'aria-labelledby': labelId,
        tabIndex: -1,
      }
    : { 'aria-hidden': true, inert: '' };

  if (!open && !keepMounted) return null;

  return (
    <div
      // Reviewed (react-props-spreading): typed shared-UI wrapper — props is
      // React.HTMLAttributes<HTMLDivElement> from the component signature, and
      // accessibilityProps is a local constant; no arbitrary attribute pass-through.
      // nosemgrep
      {...props /* nosemgrep */}
      {...accessibilityProps /* nosemgrep */}
      ref={surfaceRef}
      onClick={(event) => {
        onClick?.(event);
        if (
          open &&
          closeOnOverlayClick &&
          !event.defaultPrevented &&
          event.target === event.currentTarget
        ) {
          onClose();
        }
      }}
    >
      {label ? (
        <span id={generatedId} style={HIDDEN_LABEL_STYLE}>
          {label}
        </span>
      ) : null}
      {children}
    </div>
  );
}
