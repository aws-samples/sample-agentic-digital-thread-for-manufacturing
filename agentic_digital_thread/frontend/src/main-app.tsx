// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* index.html entry — app shell. */
import './session-bootstrap';

/* Global styles — cascade order is contractual
 * (base→dashboard→onboard→thread→memory→login in <head>, admin.css as the
 * bottom-of-body <link>). fonts.css first: the web fonts from the pinned npm
 * packages, bundled into dist/assets/. */
import './styles/fonts.css';
import './styles/base.css';
import './styles/dashboard.css';
import './styles/onboard.css';
import './styles/thread.css';
import './styles/memory.css';
import './styles/login.css';
import './styles/admin.css';
import './styles/enhancements.css';

import React from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createRoot } from 'react-dom/client';

import { createQueryClient } from './api/query-client';
import { registerSystemsQueryClient } from './api/systems-query';
import { AuthProvider } from './contexts/AuthContext';
import { BrandingProvider } from './contexts/BrandingContext';
import AppShell from './pages/app/AppShell';

const queryClient = createQueryClient();
registerSystemsQueryClient(queryClient);

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrandingProvider>
        <AuthProvider>
          <AppShell />
        </AuthProvider>
      </BrandingProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
