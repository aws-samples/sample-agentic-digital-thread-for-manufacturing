// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for agents/auth/routes.py responses. */

/** GET /api/auth/me — 401 when logged out (authFetch redirects). */
export interface AuthMe {
  user_id: string | null;
  email: string | null;
  username: string | null;
  is_admin: boolean;
}

/** POST /api/auth/login — success body; error bodies carry {message|detail}. */
export interface LoginOk {
  status: 'ok';
  email: string;
  is_admin: boolean;
}

export interface ApiError {
  message?: string;
  detail?: string;
}
