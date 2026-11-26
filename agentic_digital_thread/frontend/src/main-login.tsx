// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* login.html entry. */

/* Global styles — static/login.html loads /css/fonts.css + /css/login.css. */
import './styles/fonts.css';
import './styles/login.css';

import React from 'react';
import { createRoot } from 'react-dom/client';

import { BrandingProvider } from './contexts/BrandingContext';
import LoginPage from './pages/login/LoginPage';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrandingProvider updateTitle={false} source="direct">
      <LoginPage />
    </BrandingProvider>
  </React.StrictMode>,
);
