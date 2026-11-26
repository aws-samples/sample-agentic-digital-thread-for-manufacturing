// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* BrandingContext — /api/branding (public route; consumed by all three entries).
 * App/admin use the shared Query cache. The login entry deliberately selects
 * the direct mode because it has no QueryClientProvider. */
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';

import { ApiError, requestJson } from '../api/client';
import { useBrandingQuery } from '../api/reference-queries';
import type { Branding } from '../api/types/core';

interface BrandingState {
  branding: Branding | null;
  /** True once the request settled (success OR failure). */
  loaded: boolean;
  /** Checked transport detail for consumers that choose to show degraded branding. */
  error: string | null;
}

interface BrandingProviderProps {
  children: ReactNode;
  /** index.html sets document.title from branding; login/admin keep their own titles. */
  updateTitle?: boolean;
  /** Login-only escape hatch for the entry that intentionally has no Query provider. */
  source?: 'query' | 'direct';
}

const BrandingContext = createContext<BrandingState>({ branding: null, loaded: false, error: null });

function errorDetail(error: unknown): string {
  return error instanceof ApiError
    ? error.detail
    : error instanceof Error
      ? error.message
      : String(error);
}

function updateDocumentTitle(
  updateTitle: boolean,
  branding: Branding | null,
  failed: boolean,
): void {
  if (!updateTitle) return;
  if (branding) document.title = branding.company + ' — ' + branding.tagline;
  else if (failed) document.title = 'Manufacturing Intelligence';
}

function QueryBrandingProvider({ children, updateTitle }: BrandingProviderProps) {
  const query = useBrandingQuery();
  const branding = query.data ?? null;
  const failed = query.isError && !branding;

  useEffect(() => {
    updateDocumentTitle(!!updateTitle, branding, failed);
  }, [branding, failed, updateTitle]);

  const state: BrandingState = {
    branding,
    loaded: !query.isPending,
    error: query.error ? errorDetail(query.error) : null,
  };
  return <BrandingContext.Provider value={state}>{children}</BrandingContext.Provider>;
}

function DirectBrandingProvider({ children, updateTitle }: BrandingProviderProps) {
  const [state, setState] = useState<BrandingState>({ branding: null, loaded: false, error: null });

  useEffect(() => {
    let cancelled = false;
    void requestJson<Branding>('/api/branding')
      .then((branding) => {
        if (cancelled) return;
        updateDocumentTitle(!!updateTitle, branding, false);
        setState({ branding, loaded: true, error: null });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        updateDocumentTitle(!!updateTitle, null, true);
        setState({ branding: null, loaded: true, error: errorDetail(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [updateTitle]);

  return <BrandingContext.Provider value={state}>{children}</BrandingContext.Provider>;
}

export function BrandingProvider({
  children,
  updateTitle = true,
  source = 'query',
}: BrandingProviderProps) {
  if (source === 'direct') {
    return <DirectBrandingProvider updateTitle={updateTitle}>{children}</DirectBrandingProvider>;
  }
  return <QueryBrandingProvider updateTitle={updateTitle}>{children}</QueryBrandingProvider>;
}

export function useBranding(): BrandingState {
  return useContext(BrandingContext);
}
