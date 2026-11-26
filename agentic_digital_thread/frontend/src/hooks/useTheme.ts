// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Theme hook — owns the toggleTheme/restore behavior.
 *
 * Contract: localStorage key 'theme' ('light' | 'dark'); light mode applies the
 * 'light' class on <html>. The entry HTMLs also apply the saved class in an
 * inline <script> before first paint (anti-flash), so this hook only needs to
 * keep state and the class in sync from React.
 *
 * NOTE: the old toggleTheme also restyled the live cytoscape graph in place —
 * that part belongs to the architecture view (Task 2.3), which should watch
 * this hook's value. */
import { useCallback, useState } from 'react';

const THEME_KEY = 'theme';

function readIsLight(): boolean {
  return localStorage.getItem(THEME_KEY) === 'light';
}

export function useTheme(): { isLight: boolean; toggleTheme: () => void } {
  const [isLight, setIsLight] = useState<boolean>(readIsLight);

  const toggleTheme = useCallback(() => {
    const next = document.documentElement.classList.toggle('light');
    localStorage.setItem(THEME_KEY, next ? 'light' : 'dark');
    setIsLight(next);
  }, []);

  return { isLight, toggleTheme };
}
