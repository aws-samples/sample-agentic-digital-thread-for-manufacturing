// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* admin.html entry. */
import './session-bootstrap';

/* Global styles — contractual order (base + admin in <head>,
 * thread.css appended for the thread tab). fonts.css first: the web fonts
 * from the pinned npm packages, bundled into dist/assets/. */
import './styles/fonts.css';
import './styles/base.css';
import './styles/admin.css';
// The Context tab reuses <BusinessContextCard/> from the onboarding flow by
// design — its .onboard-btn/.reset-btn classes resolve from onboard.css.
import './styles/onboard.css';
import './styles/thread.css';

import React from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createRoot } from 'react-dom/client';

import { createQueryClient } from './api/query-client';
import { AuthProvider } from './contexts/AuthContext';
import { BrandingProvider } from './contexts/BrandingContext';
import AdminPage from './pages/admin/AdminPage';
import { ConfirmHost } from './shared/ui/ConfirmHost';
import { ToastRegion } from './shared/ui/ToastRegion';

const queryClient = createQueryClient();

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrandingProvider updateTitle={false}>
        <AuthProvider>
          <AdminPage />
          <ToastRegion />
          <ConfirmHost />
        </AuthProvider>
      </BrandingProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
