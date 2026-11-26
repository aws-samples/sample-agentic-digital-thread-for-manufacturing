// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { Component, type ErrorInfo, type ReactNode } from 'react';

export interface ViewErrorBoundaryProps {
  children: ReactNode;
  viewName: string;
}

interface ViewErrorBoundaryState {
  error: Error | null;
  retryKey: number;
}

/** Isolates a lazily loaded view so a chunk or render failure cannot take down
 * the shell or another already-mounted view. */
export class ViewErrorBoundary extends Component<
  ViewErrorBoundaryProps,
  ViewErrorBoundaryState
> {
  state: ViewErrorBoundaryState = { error: null, retryKey: 0 };

  static getDerivedStateFromError(error: Error): Partial<ViewErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('view failed', this.props.viewName, error, info);
  }

  private retry = (): void => {
    this.setState((state) => ({ error: null, retryKey: state.retryKey + 1 }));
  };

  private reload = (): void => {
    window.location.reload();
  };

  render() {
    if (this.state.error) {
      return (
        <section className="view-error" role="alert" aria-label={`${this.props.viewName} unavailable`}>
          <h2>{this.props.viewName} is temporarily unavailable</h2>
          <p>This view could not be loaded. Other views remain available.</p>
          <div className="view-error-actions">
            <button type="button" onClick={this.retry}>Try again</button>
            <button type="button" onClick={this.reload}>Reload page</button>
          </div>
        </section>
      );
    }

    return <div key={this.state.retryKey} className="view-boundary">{this.props.children}</div>;
  }
}
