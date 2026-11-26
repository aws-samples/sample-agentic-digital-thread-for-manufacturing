// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* AuthContext — the /api/auth/me result (user display + admin gate). */
import { QueryClientContext } from '@tanstack/react-query';
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';

import { requestJson } from '../api/client';
import { useAuthMeQuery } from '../api/reference-queries';
import type { AuthMe } from '../api/types/auth';

interface AuthState {
  user: AuthMe | null;
  /** True until /api/auth/me resolves (or fails). */
  loading: boolean;
}

const AuthContext = createContext<AuthState>({ user: null, loading: true });

/** Short display name — email before @, or first 8 chars of long ids
 * (verbatim logic carried over from the retired legacy UI). */
export function shortDisplayName(u: AuthMe): string {
  const name = u.email || u.username || u.user_id || '';
  if (name.includes('@')) return name.split('@')[0];
  if (name.length > 12) return name.slice(0, 8) + '…';
  return name;
}

function QueryAuthProvider({ children }: { children: ReactNode }) {
  const query = useAuthMeQuery();
  const state: AuthState = {
    user: query.data ?? null,
    loading: query.isPending,
  };
  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

function DirectAuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ user: null, loading: true });
  useEffect(() => {
    let cancelled = false;
    void requestJson<AuthMe>('/api/auth/me')
      .then((user) => {
        if (!cancelled) setState({ user, loading: false });
      })
      .catch(() => {
        if (!cancelled) setState({ user: null, loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  // App/admin always provide this context and therefore use Query. Retaining a
  // direct path keeps isolated/legacy mounts compatible without changing the
  // public AuthContext value contract.
  const queryClient = useContext(QueryClientContext);
  return queryClient
    ? <QueryAuthProvider>{children}</QueryAuthProvider>
    : <DirectAuthProvider>{children}</DirectAuthProvider>;
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
