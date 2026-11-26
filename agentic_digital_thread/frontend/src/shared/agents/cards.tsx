// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Agents-fleet card components (jsx-migration W5.2) — the JSX form of the
 * former HTML string builders in ./html.ts. The markup, classes, and inline styles stay
 * faithful to the original (admin.css .agent-card family contract:
 * agent-card, agent-card-header/-name, agent-status-dot ready|error,
 * agent-metrics, agent-metric(-label/-value), agent-scores(-title),
 * agent-score-row/-name/-bar-wrap/-bar/-val, agent-no-scores,
 * agent-card-links). All content is display-only; JSX auto-escaping replaces
 * the old raw interpolation (fixes the unescaped display_name) and makes the
 * former escAttr helper redundant (deleted — no other consumer). */

import type { AgentInfo, NeptuneStats } from '../../api/types/agents';
import type { PricingMap, PriceEntry } from './pricing';
import {
  BEDROCK_PRICING_URL,
  EVAL_META,
  fmtCost,
  fmtLatency,
  fmtNum,
  modelDisplayName,
  NEPTUNE_PRICING_URL,
  scoreColor,
} from './pricing';

// ── Token stacked bar ─────────────────────────────────────────────────────────
export function TokenBar({ inputTok, outputTok }: { inputTok: number; outputTok: number }) {
  const total = (inputTok || 0) + (outputTok || 0);
  if (!total) {
    return (
      <div style={{ fontSize: '0.7rem', color: 'var(--muted)', marginTop: '0.4rem' }}>
        No token data
      </div>
    );
  }
  const inputPct = total > 0 ? Math.round((inputTok / total) * 100) : 50;
  const outputPct = 100 - inputPct;
  return (
    <div style={{ marginTop: '0.5rem' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: '0.72rem',
          color: 'var(--muted)',
          marginBottom: '0.25rem',
        }}
      >
        <span style={{ fontWeight: 600, color: 'var(--text2)' }}>Tokens: {fmtNum(total)}</span>
        <span style={{ display: 'flex', gap: '0.75rem' }}>
          <span style={{ color: '#60a5fa' }}>▪ In {fmtNum(inputTok)}</span>
          <span style={{ color: '#f97316' }}>▪ Out {fmtNum(outputTok)}</span>
        </span>
      </div>
      <div
        style={{
          height: 6,
          background: 'var(--surface2)',
          borderRadius: 3,
          overflow: 'hidden',
          width: '100%',
        }}
      >
        <div
          style={{
            height: '100%',
            width: '100%',
            display: 'flex',
            borderRadius: 3,
            overflow: 'hidden',
          }}
        >
          <div style={{ flex: inputPct, background: '#60a5fa' }} />
          <div style={{ flex: outputPct, background: '#f97316' }} />
        </div>
      </div>
    </div>
  );
}

// ── Single agent card ─────────────────────────────────────────────────────────
const deepLinkStyle: React.CSSProperties = {
  color: 'var(--muted)',
  textDecoration: 'none',
  fontSize: '0.7rem',
};

/** Allow only http(s): and relative URLs in anchor hrefs — blocks javascript:
 * and other dangerous schemes if a backend payload were ever tampered with. */
function safeHref(url: string): string | undefined {
  try {
    const u = new URL(url, window.location.origin);
    if (u.protocol === 'http:' || u.protocol === 'https:') return url;
  } catch {
    /* unparseable → treat as unsafe */
  }
  return undefined;
}

export function AgentCard({ agent, pricing }: { agent: AgentInfo; pricing: PricingMap }) {
  const statusClass = agent.status === 'READY' ? 'ready' : 'error';
  const scores = agent.eval_scores || {};
  // A score without an average yet (the figure check before any answer with
  // a figure was sampled) gets no bar.
  const scoreKeys = Object.keys(scores).filter((k) => typeof scores[k]?.avg === 'number');
  const inputTok = agent.input_tokens_24h || 0;
  const outputTok = agent.output_tokens_24h || 0;
  const isOrch = (agent.runtime_name || agent.name || '').includes('orchestrator');

  const modelLabel = agent.no_llm
    ? 'Tools · no LLM'
    : agent.model_id
      ? modelDisplayName(agent.model_id, pricing)
      : agent.runtime_name?.includes('orchestrator')
        ? 'Claude Sonnet'
        : 'Claude Haiku 4.5';

  return (
    <div className="agent-card">
      <div className="agent-card-header">
        <span className="agent-card-name">{agent.display_name}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span
            style={{
              fontSize: '0.72rem',
              color: 'var(--muted)',
              textTransform: 'uppercase',
              letterSpacing: '0.04em',
            }}
          >
            {modelLabel}
          </span>
          <span
            style={{
              fontSize: '0.72rem',
              color: 'var(--muted)',
              fontFamily: "'IBM Plex Mono',monospace",
            }}
          >
            {agent.status}
          </span>
          <div className={`agent-status-dot ${statusClass}`} />
        </div>
      </div>
      <div className="agent-metrics">
        <div className="agent-metric">
          <div className="agent-metric-label">Sessions</div>
          <div className="agent-metric-value">{fmtNum(agent.invocations_24h)}</div>
        </div>
        <div className="agent-metric">
          <div className="agent-metric-label">Avg Latency</div>
          <div className={`agent-metric-value ${agent.avg_latency_s ? '' : 'dim'}`}>
            {fmtLatency(agent.avg_latency_s)}
          </div>
        </div>
        <div className="agent-metric">
          <div className="agent-metric-label">Est. Cost</div>
          <div className={`agent-metric-value ${agent.cost_usd_24h ? '' : 'dim'}`}>
            {fmtCost(agent.cost_usd_24h)}
          </div>
        </div>
        <div className="agent-metric">
          <div className="agent-metric-label">Cost/Session</div>
          <div className="agent-metric-value dim" style={{ fontSize: '0.8rem' }}>
            {fmtCost(agent.cost_per_request)}
          </div>
        </div>
        {isOrch && (
          <>
            <div className="agent-metric">
              <div className="agent-metric-label">Tool Calls</div>
              <div className="agent-metric-value">{fmtNum(agent.tool_calls_24h)}</div>
            </div>
            <div className="agent-metric">
              <div className="agent-metric-label">Avg Events/Session</div>
              <div className="agent-metric-value dim" style={{ fontSize: '0.8rem' }}>
                {agent.avg_events_per_session ? Math.round(agent.avg_events_per_session) : '—'}
              </div>
            </div>
          </>
        )}
      </div>
      {isOrch ? (
        <TokenBar inputTok={inputTok} outputTok={outputTok} />
      ) : inputTok > 0 ? (
        <div style={{ marginTop: '0.5rem', fontSize: '0.7rem', color: 'var(--muted)' }}>
          <span style={{ color: '#60a5fa' }}>▪ In {fmtNum(inputTok)}</span>
          <span style={{ marginLeft: '0.5rem', color: '#f97316' }}>▪ Out {fmtNum(outputTok)}</span>
          <span style={{ marginLeft: '0.5rem', opacity: 0.6 }}>(fleet share)</span>
        </div>
      ) : null}
      {agent.model_shared_across_roles && !agent.no_llm && (
        <div style={{ marginTop: '0.35rem', fontSize: '0.7rem', color: 'var(--muted)' }}>
          Includes tokens from a model the other role also uses; Bedrock can&apos;t split them,
          so this card may overstate its share. The fleet totals count them once.
        </div>
      )}
      {isOrch && (
        <div className="agent-scores" style={{ marginTop: '0.6rem' }}>
          <div className="agent-scores-title">
            Quality Scores (last 7d)
            {scoreKeys.length ? ` · ${scoreKeys.length} metrics` : ''}
          </div>
          {scoreKeys.length === 0 ? (
            <div className="agent-no-scores">
              {agent.has_eval ? 'No evaluations yet' : 'Evaluation not configured'}
            </div>
          ) : (
            scoreKeys.map((k) => {
              const v = scores[k].avg ?? 0;
              const cnt = scores[k].count || 0;
              const pct = Math.round(v * 100);
              const color = scoreColor(v);
              const meta = EVAL_META[k] || { label: k, desc: '' };
              return (
                <div
                  key={k}
                  className="agent-score-row"
                  title={`${meta.desc}${cnt ? ' · ' + cnt + ' evals' : ''}`}
                >
                  <span className="agent-score-name">{meta.label}</span>
                  <div className="agent-score-bar-wrap">
                    <div className={`agent-score-bar ${color}`} style={{ width: `${pct}%` }} />
                  </div>
                  <span className="agent-score-val">{pct}%</span>
                </div>
              );
            })
          )}
        </div>
      )}
      {/* Native CloudWatch deep-links (fail-soft: absent on older/local payloads) */}
      {agent.observability_url && (
        <div
          className="agent-card-links"
          style={{
            display: 'flex',
            gap: '0.75rem',
            marginTop: '0.6rem',
            paddingTop: '0.5rem',
            borderTop: '1px solid var(--border)',
          }}
        >
          {/* Security-reviewed (react-href-var): value passes safeHref(),
              which permits only http:/https: and returns undefined otherwise. */}
          {/* nosemgrep */}
          <a
            href={safeHref(agent.observability_url) /* nosemgrep */}
            target="_blank"
            rel="noopener"
            style={deepLinkStyle}
            aria-label={`Open CloudWatch GenAI Observability for ${agent.display_name ?? ''}`}
            title="CloudWatch GenAI Observability (AgentCore)"
          >
            Observability ↗
          </a>
          {agent.logs_url && (
            // Security-reviewed (react-href-var): value passes safeHref().
            // nosemgrep
            <a
              href={safeHref(agent.logs_url) /* nosemgrep */}
              target="_blank"
              rel="noopener"
              style={deepLinkStyle}
              aria-label={`Open CloudWatch Logs for ${agent.display_name ?? ''}`}
              title="CloudWatch runtime log group"
            >
              Logs ↗
            </a>
          )}
        </div>
      )}
    </div>
  );
}

// ── Pricing table ─────────────────────────────────────────────────────────────
const pricingLinkStyle: React.CSSProperties = { color: 'var(--accent)' };
const priceCellStyle: React.CSSProperties = { padding: '0.35rem 0.6rem', color: 'var(--text2)' };
const priceHeadStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '0.3rem 0.6rem',
  color: 'var(--muted)',
  fontWeight: 500,
};

export function PricingTable({ entries }: { entries: Array<[string, PriceEntry]> }) {
  return (
    <div style={{ marginTop: '2rem', borderTop: '1px solid var(--border)', paddingTop: '1rem' }}>
      <div
        style={{
          fontSize: '0.72rem',
          color: 'var(--muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          marginBottom: '0.5rem',
        }}
      >
        Pricing Reference: Amazon Bedrock on-demand list prices, USD per 1K tokens
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.75rem' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            <th style={priceHeadStyle}>Model</th>
            <th style={priceHeadStyle}>Model ID</th>
            <th style={{ ...priceHeadStyle, textAlign: 'right' }}>Input /1K</th>
            <th style={{ ...priceHeadStyle, textAlign: 'right' }}>Output /1K</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([id, p]) => (
            <tr key={id}>
              <td style={priceCellStyle}>{p.label}</td>
              <td style={{ ...priceCellStyle, fontFamily: "'IBM Plex Mono',monospace" }}>{id}</td>
              <td style={{ ...priceCellStyle, textAlign: 'right' }}>
                {p.input ? p.input.toFixed(5) : '—'}
              </td>
              <td style={{ ...priceCellStyle, textAlign: 'right' }}>
                {p.output ? p.output.toFixed(5) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ fontSize: '0.7rem', color: 'var(--muted)', marginTop: '0.4rem' }}>
        Cost = (input_tokens × input_price + output_tokens × output_price) / 1000. Estimates
        only: model prices are Amazon Bedrock list prices, and the Neptune Analytics estimate uses
        approximate prices. Current rates:{' '}
        <a href={BEDROCK_PRICING_URL} target="_blank" rel="noopener noreferrer" style={pricingLinkStyle}>
          Amazon Bedrock pricing<span className="visually-hidden"> (opens in a new tab)</span>
        </a>
        {', '}
        <a href={NEPTUNE_PRICING_URL} target="_blank" rel="noopener noreferrer" style={pricingLinkStyle}>
          Amazon Neptune pricing<span className="visually-hidden"> (opens in a new tab)</span>
        </a>
        .
      </div>
    </div>
  );
}

// ── Neptune metrics card ──────────────────────────────────────────────────────
// Amazon Neptune Analytics is billed per provisioned m-NCU-hour (1 m-NCU = 1 GB
// of graph memory) with no storage or I/O charge. The server-side estimate
// (agents_routes.py NEPTUNE_ANALYTICS_MNCU_HOUR_USD, AWS Price List per region)
// returns the rate it used as `mncu_price_per_hour`.
function CpuBar({ pct }: { pct: number }) {
  const color = pct > 80 ? '#f87171' : pct > 50 ? '#fbbf24' : '#4ade80';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
      <div
        style={{
          flex: 1,
          height: 4,
          background: 'var(--surface2)',
          borderRadius: 2,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${Math.min(pct, 100).toFixed(0)}%`,
            background: color,
            borderRadius: 2,
          }}
        />
      </div>
      <span
        style={{ fontSize: '0.72rem', color: 'var(--text2)', minWidth: '2.5rem', textAlign: 'right' }}
      >
        {pct.toFixed(1)}%
      </span>
    </div>
  );
}

const fmtMb = (mb: number | null | undefined): string =>
  mb == null ? '—' : mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(0) + ' MB';
const fmtRate = (r: number | null | undefined): string => (r == null ? '—' : r.toFixed(2) + '/s');

export function NeptuneCard({ d, hoursLbl }: { d: NeptuneStats; hoursLbl: string }) {
  const statusColor =
    d.status === 'AVAILABLE' ? '#4ade80' : d.status === 'CREATING' ? '#fbbf24' : '#f87171';

  // Cost breakdown tooltip: m-NCU-hours are the whole Neptune Analytics bill.
  const costBreakdown =
    d.cost_ncu != null
      ? `m-NCU-hours (${hoursLbl}): ${fmtCost(d.cost_ncu)} | no storage or I/O charge`
      : '';

  const rowLabelStyle: React.CSSProperties = { color: 'var(--muted)' };
  const rowValueStyle: React.CSSProperties = { color: 'var(--text2)' };

  return (
    <div className="agent-card" style={{ width: '100%', boxSizing: 'border-box', height: '100%' }}>
      <div className="agent-card-header">
        <span className="agent-card-name">Amazon Neptune Analytics</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span
            style={{
              fontSize: '0.72rem',
              color: 'var(--muted)',
              fontFamily: "'IBM Plex Mono',monospace",
            }}
          >
            {d.status || 'UNKNOWN'}
          </span>
          <div
            style={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: statusColor,
              flexShrink: 0,
            }}
          />
        </div>
      </div>
      <div className="agent-metrics">
        <div className="agent-metric">
          <div className="agent-metric-label">m-NCUs</div>
          <div className="agent-metric-value">{d.ncu_count != null ? d.ncu_count : '—'}</div>
        </div>
        <div className="agent-metric">
          <div className="agent-metric-label">Memory</div>
          <div className="agent-metric-value">
            {d.provisioned_memory_gb != null ? d.provisioned_memory_gb + 'GB' : '—'}
          </div>
        </div>
        <div className="agent-metric" title={costBreakdown}>
          <div className="agent-metric-label">Est. Cost ({hoursLbl})</div>
          <div className={`agent-metric-value ${d.cost_usd ? '' : 'dim'}`}>{fmtCost(d.cost_usd)}</div>
        </div>
      </div>
      <div
        style={{
          padding: '0.65rem 0.75rem',
          display: 'flex',
          flexDirection: 'column',
          gap: '0.4rem',
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            fontSize: '0.72rem',
          }}
        >
          <span style={rowLabelStyle}>Query Rate</span>
          <span style={rowValueStyle}>
            {fmtRate(d.query_rate_avg)} avg {'\u00a0/\u00a0'} {fmtRate(d.query_rate_peak)} peak
          </span>
        </div>
        <div style={{ fontSize: '0.72rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.2rem' }}>
            <span style={rowLabelStyle}>CPU Avg</span>
            <span style={rowValueStyle}>
              {d.cpu_peak != null ? 'peak ' + d.cpu_peak.toFixed(1) + '%' : ''}
            </span>
          </div>
          {d.cpu_avg != null ? (
            <CpuBar pct={d.cpu_avg} />
          ) : (
            <span style={{ color: 'var(--muted)', fontSize: '0.72rem' }}>—</span>
          )}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem' }}>
          <span style={rowLabelStyle}>Graph Size</span>
          <span style={rowValueStyle}>{fmtMb(d.graph_size_mb)}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem' }}>
          <span style={rowLabelStyle}>Edges</span>
          <span style={rowValueStyle}>{fmtNum(d.edges)}</span>
        </div>
      </div>
      <div
        style={{
          marginTop: 'auto',
          fontSize: '0.72rem',
          color: 'var(--muted)',
          borderTop: '1px solid var(--border)',
          padding: '0.4rem 0.75rem',
          display: 'flex',
          justifyContent: 'space-between',
        }}
      >
        <span>
          {d.graph_id} {'\u00a0·\u00a0'} {d.replica_count != null ? d.replica_count : 0} replica
          {d.replica_count !== 1 ? 's' : ''}
        </span>
        <span>
          {d.mncu_price_per_hour != null
            ? `$${d.mncu_price_per_hour}/m-NCU-hr${
                d.billing_state === 'stopped' ? ' (stopped rate, whole window)' : ''
              } · no storage or I/O charge`
            : 'No price for this region'}
        </span>
      </div>
    </div>
  );
}

// ── Live log tail helpers ─────────────────────────────────────────────────────
export function colorizeLogMsg(msg: string): string {
  if (msg.includes('ERROR') || msg.includes('error') || msg.includes('failed')) return '#f87171';
  if (msg.includes('WARNING') || msg.includes('warn')) return '#fbbf24';
  if (msg.includes('COMPLETE') || msg.includes('complete') || msg.includes('success')) return '#4ade80';
  if (msg.includes('Tool #') || msg.includes('tool')) return '#60a5fa';
  if (msg.includes('session') || msg.includes('Session')) return '#c084fc';
  return '#a3e635';
}

/** One rendered log line (timestamp + colorized msg — JSX escaping replaces
 * the old manual angle-bracket escape). */
export function LogLine({ ts, msg }: { ts: number; msg: string }) {
  const t = new Date(ts).toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  return (
    <div>
      <span style={{ color: '#4b5563' }}>{t}</span>{' '}
      <span style={{ color: colorizeLogMsg(msg) }}>{msg}</span>
    </div>
  );
}
