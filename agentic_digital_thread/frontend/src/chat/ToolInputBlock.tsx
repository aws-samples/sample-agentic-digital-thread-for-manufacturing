// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ToolInputBlock/> — a tool call's input block as JSX (zero-innerHTML
 * migration; was the renderToolInputBlock HTML-string builder in
 * tool-labels.ts). Same structure/classes: a concise `.trace-input` summary
 * line, then (when the call carries a `kg_context`) a collapsed
 * `.trace-input-route` "Routing context" expander showing that ROUTE block
 * with its real line breaks (a bounded display copy: the code fence lines
 * the block is wrapped in at its source dropped, CRLF as LF, capped at
 * ROUTING_CONTEXT_MAX_CHARS with a visible truncation note), then (when the
 * raw payload adds detail) a collapsed `.trace-input-raw` expander with the
 * unchanged JSON. All text
 * renders as JSX text — escaped by construction. Renders nothing when there
 * is nothing to show.
 *
 * `showSummary={false}` drops the summary line when the caller already shows
 * it (the activity-line call row names what was asked); the routing-context
 * and raw-args expanders are unchanged.
 *
 * `routeContext` is the context the system was sent when the gateway merged
 * route-seed facts into the model's arguments (tool_result `route_context`).
 * The Routing context then shows it, with a note that some lines came from
 * the knowledge graph; raw args stays the model's own arguments. */
import { tf, useI18n } from '../lib/i18n';
import { formatToolInput, ROUTING_CONTEXT_MAX_CHARS, routingContextAdded, routingContextOf } from './tool-labels';

export interface ToolInputBlockProps {
  /** The REAL call args (`_toolInput` stash from tool_start). */
  input: unknown;
  tool: string;
  showSummary?: boolean;
  /** The routing context the system was sent (tool_result `route_context`). */
  routeContext?: string;
}

export function ToolInputBlock({ input, tool, showSummary = true, routeContext }: ToolInputBlockProps) {
  // Subscribes to the language store so the routing-context label follows
  // the language picker.
  const { t } = useI18n();
  const { summary, raw } = formatToolInput(tool, input);
  const route = routingContextOf(input, routeContext);
  const added = routingContextAdded(input, routeContext);
  if (!summary && !raw) return null;
  return (
    <>
      {summary && showSummary ? (
        <div className="trace-input">
          <span className="trace-input-label">input:</span> {summary}
        </div>
      ) : null}
      {route ? (
        <details className="trace-input-route" data-truncated={route.truncated ? 'true' : undefined}>
          <summary>{t('chat.routingContext')}</summary>
          <pre>
            {route.text}
            {route.truncated ? '…' : null}
          </pre>
          {added ? <p className="trace-input-route-added">{t('chat.routingContextAdded')}</p> : null}
          {route.truncated ? (
            <p className="trace-input-route-truncated">
              {tf('chat.routingContextTruncated', { shown: ROUTING_CONTEXT_MAX_CHARS })}
            </p>
          ) : null}
        </details>
      ) : null}
      {raw && raw !== summary ? (
        <details className="trace-input-raw">
          <summary>raw args</summary>
          <pre>{raw}</pre>
        </details>
      ) : null}
    </>
  );
}
