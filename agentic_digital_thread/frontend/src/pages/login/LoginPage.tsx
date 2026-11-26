// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* LoginPage — port of static/login.html (finalized in Task 3.1).
 *
 * Form ids are e2e-load-bearing (tests/e2e conftest login fixture):
 * #login-form #email #password #toggle-pw #submit-btn #error-msg. */
import { useState, type FormEvent } from 'react';

import { ApiError, requestJson } from '../../api/client';
import type { LoginOk } from '../../api/types/auth';
import { useBranding } from '../../contexts/BrandingContext';

export default function LoginPage() {
  const { branding } = useBranding();
  const [showPw, setShowPw] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    const email = (form.elements.namedItem('email') as HTMLInputElement).value;
    const password = (form.elements.namedItem('password') as HTMLInputElement).value;
    setError('');
    setSubmitting(true);
    try {
      const data = await requestJson<LoginOk>('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      if (data.status === 'ok') {
        window.location.href = '/';
      } else {
        setError('Invalid credentials.');
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Network error. Try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="card">
      <div className="brand">
        <div className="brand-name" id="company">
          {branding?.company || 'Agentic Digital Thread'}
        </div>
        <div className="brand-sub">Manufacturing Intelligence Platform</div>
      </div>

      <form id="login-form" onSubmit={onSubmit}>
        <div className="field">
          <label htmlFor="email">Username</label>
          <input type="text" id="email" name="email" autoComplete="username" required />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <div className="password-wrap">
            <input
              type={showPw ? 'text' : 'password'}
              id="password"
              name="password"
              autoComplete="current-password"
              required
            />
            <button
              type="button"
              className="toggle-pw"
              id="toggle-pw"
              aria-label="Toggle password visibility"
              onClick={() => setShowPw((s) => !s)}
            >
              {showPw ? 'hide' : 'show'}
            </button>
          </div>
        </div>
        <button type="submit" className="btn" id="submit-btn" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign In'}
        </button>
        <div
          className={`error${error ? ' visible' : ''}`}
          id="error-msg"
          role="alert"
          aria-live="assertive"
        >
          {error}
        </div>
      </form>
    </div>
  );
}
