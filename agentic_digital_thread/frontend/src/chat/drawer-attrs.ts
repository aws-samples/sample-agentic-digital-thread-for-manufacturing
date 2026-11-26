// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Attributes for a collapsed chat/memory drawer. A collapsed drawer is
 * zero-width and transparent, but without `inert` its controls stay in the
 * Tab order and a focused one keeps focus after the drawer closes. React 18
 * has no `inert` prop type and needs the empty-string form to emit the
 * attribute (same form as shared/ui/ModalSurface's closed state). */

export type CollapsedDrawerAttrs = { inert?: '' };

export function collapsedDrawerAttrs(collapsed: boolean): CollapsedDrawerAttrs {
  return collapsed ? { inert: '' } : {};
}
