// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Settings tab — the #tab-settings panel:
 * loadSettings/saveSettings/saveDemoEnabled/saveLiveUpdatesEnabled/
 * saveHighByteToken/loadLLMSettings/saveLLMSettings/loadConceptPacks/
 * installConceptPack/uninstallConceptPack.
 *
 * The host keeps this component mounted after first activation. TanStack Query
 * owns server freshness while local form state remains stable across unrelated
 * refetches. Element ids and inline styles retain the static markup contract. */
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../api/client';
import type { ConceptPack, LlmSettings, MemoryModelJob } from '../../../api/types/admin';
import { useBranding } from '../../../contexts/BrandingContext';
import { askConfirm } from '../../../shared/ui/confirm-store';
import {
  adminConceptPacksQueryOptions,
  adminFeedbackExplorationQueryOptions,
  adminLiveUpdatesQueryOptions,
  adminLlmSettingsQueryOptions,
  adminModelsQueryOptions,
  adminQuickLinksQueryOptions,
  adminSettingsQueryOptions,
  installConceptPackMutationOptions,
  saveBrandingMutationOptions,
  saveDemoSettingMutationOptions,
  saveFeedbackExplorationMutationOptions,
  saveHighByteTokenMutationOptions,
  saveLiveUpdatesMutationOptions,
  saveLlmSettingsMutationOptions,
  uninstallConceptPackMutationOptions,
} from '../admin-queries';
import {
  MEMORY_JOB_POLL_LIMIT_MS,
  MEMORY_JOB_POLL_MS,
  MEMORY_JOB_RETRIES,
  memoryModelJobQueryOptions,
  startMemoryModelSwitchMutationOptions,
} from '../memory-model-queries';
import { queryKeys } from '../../../api/query-keys';
import { BEDROCK_PRICING_URL } from '../../../shared/agents/pricing';
import {
  buildMemoryModelOptions,
  buildModelOptions,
  escapeHtml,
  MEMORY_MODEL_MIXED,
  memoryModelLabel,
  memoryModelToSave,
  modelValueToSave,
} from './admin-logic';
import SnapshotSection from './settings/SnapshotSection';
import CalendarPolicySection from './settings/CalendarPolicySection';

export interface SettingsTabProps {
  /** Demo on/off propagated to the host (gates the Demo Script tab button). */
  onDemoEnabledChange: (enabled: boolean) => void;
  /** Successful branding save → host updates #brand-logo / #admin-footer. */
  onBrandingSaved: (company: string) => void;
}

// Verbatim inline styles for #tab-settings (carried over from the retired legacy UI).
const inputStyle: CSSProperties = {
  width: '100%',
  padding: '0.5rem 0.75rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.85rem',
  boxSizing: 'border-box',
};
const monoInputStyle: CSSProperties = { ...inputStyle, fontFamily: 'monospace' };
const fieldLabelStyle: CSSProperties = {
  display: 'block',
  fontSize: '0.75rem',
  color: 'var(--muted)',
  marginBottom: '0.35rem',
};
const checkboxLabelStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: '0.5rem',
  fontSize: '0.8rem',
  color: 'var(--text)',
  cursor: 'pointer',
};
const checkboxStyle: CSSProperties = {
  width: 16,
  height: 16,
  accentColor: 'var(--accent)',
  cursor: 'pointer',
};
const helpTextStyle: CSSProperties = {
  margin: '0.4rem 0 0',
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.4,
};
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const h3Style: CSSProperties = { margin: '0 0 1.25rem', fontSize: '0.95rem', fontWeight: 600 };

/** Live-updates snapshot cadence choices (seconds → label). 120s is the
 * server default (DASHBOARD_SNAPSHOT_INTERVAL fallback). */
const LIVE_INTERVAL_CHOICES: ReadonlyArray<{ value: number; label: string }> = [
  { value: 30, label: '30s' },
  { value: 60, label: '1m' },
  { value: 120, label: '2m (default)' },
  { value: 300, label: '5m' },
  { value: 900, label: '15m' },
  { value: 3600, label: '1h' },
];

/** Choice list including the current value when it is not a preset (e.g. an
 * env-configured cadence) — mirrors buildModelOptions' "(current)" idiom. */
function liveIntervalChoices(current: number): Array<{ value: number; label: string }> {
  if (LIVE_INTERVAL_CHOICES.some((c) => c.value === current)) {
    return [...LIVE_INTERVAL_CHOICES];
  }
  return [...LIVE_INTERVAL_CHOICES, { value: current, label: `${current}s (current)` }];
}

interface ColoredStatus {
  text: string;
  color?: string;
}

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

export default function SettingsTab({ onDemoEnabledChange, onBrandingSaved }: SettingsTabProps) {
  const queryClient = useQueryClient();
  const { branding, error: brandingError } = useBranding();
  const settingsQuery = useQuery(adminSettingsQueryOptions());
  const modelsQuery = useQuery(adminModelsQueryOptions());
  const liveUpdatesQuery = useQuery(adminLiveUpdatesQueryOptions());
  const feedbackExplorationQuery = useQuery(adminFeedbackExplorationQueryOptions());
  const llmSettingsQuery = useQuery(adminLlmSettingsQueryOptions());
  const packsQuery = useQuery(adminConceptPacksQueryOptions());
  // Same cached query AdminPage already runs for its header links.
  const quickLinksQuery = useQuery(adminQuickLinksQueryOptions());

  const demoMutation = useMutation(saveDemoSettingMutationOptions(queryClient));
  const liveUpdatesMutation = useMutation(saveLiveUpdatesMutationOptions(queryClient));
  const feedbackExplorationMutation = useMutation(saveFeedbackExplorationMutationOptions(queryClient));
  const brandingMutation = useMutation(saveBrandingMutationOptions(queryClient));
  const tokenMutation = useMutation(saveHighByteTokenMutationOptions(queryClient));
  const llmMutation = useMutation(saveLlmSettingsMutationOptions(queryClient));
  const memoryMutation = useMutation(startMemoryModelSwitchMutationOptions());
  const installPackMutation = useMutation(installConceptPackMutationOptions(queryClient));
  const uninstallPackMutation = useMutation(uninstallConceptPackMutationOptions(queryClient));

  const [company, setCompany] = useState('');
  const [tagline, setTagline] = useState('');
  const [demoEnabled, setDemoEnabled] = useState(false);
  const [liveUpdatesEnabled, setLiveUpdatesEnabled] = useState(false);
  const [liveInterval, setLiveInterval] = useState(120);
  const [feedbackExplorationEnabled, setFeedbackExplorationEnabled] = useState(false);
  const [settingsStatus, setSettingsStatus] = useState('');

  const [hbToken, setHbToken] = useState('');
  const [tokenStatus, setTokenStatus] = useState<ColoredStatus>({ text: '' });

  const [orchModel, setOrchModel] = useState('');
  const [agentModel, setAgentModel] = useState('');
  const [memoryModel, setMemoryModel] = useState('');
  const [llmStatus, setLlmStatus] = useState<ColoredStatus>({ text: '' });
  const [memoryStatus, setMemoryStatus] = useState<ColoredStatus>({ text: '' });
  // The memory switch job this page watches (started here, or found running
  // when the page opened), and whether its bounded polling has stopped.
  const [memoryJobId, setMemoryJobId] = useState<string | null>(null);
  const [memoryPollStopped, setMemoryPollStopped] = useState(false);
  const [packsStatus, setPacksStatus] = useState<ColoredStatus>({ text: '' });

  // Timeout bookkeeping keeps delayed status clears out of an unmounted tab.
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(
    () => () => {
      timersRef.current.forEach(clearTimeout);
    },
    [],
  );
  const later = (ms: number, fn: () => void) => {
    timersRef.current.push(setTimeout(fn, ms));
  };

  const onDemoRef = useRef(onDemoEnabledChange);
  onDemoRef.current = onDemoEnabledChange;

  // Hydrate each form domain only once. Background refetches update Query's
  // cache without overwriting text or selections the operator is editing.
  const brandingHydrated = useRef(false);
  useEffect(() => {
    if (!branding || brandingHydrated.current) return;
    brandingHydrated.current = true;
    setCompany(branding.company || '');
    setTagline(branding.tagline || '');
  }, [branding]);

  const settingsHydrated = useRef(false);
  useEffect(() => {
    if (!settingsQuery.data || settingsHydrated.current) return;
    settingsHydrated.current = true;
    const enabled = settingsQuery.data.demo_enabled === true;
    setDemoEnabled(enabled);
    onDemoRef.current(enabled);
  }, [settingsQuery.data]);

  const liveHydrated = useRef(false);
  useEffect(() => {
    if (!liveUpdatesQuery.data || liveHydrated.current) return;
    liveHydrated.current = true;
    setLiveUpdatesEnabled(liveUpdatesQuery.data.live_updates_enabled === true);
    if (typeof liveUpdatesQuery.data.interval_seconds === 'number') {
      setLiveInterval(liveUpdatesQuery.data.interval_seconds);
    }
  }, [liveUpdatesQuery.data]);

  const feedbackExplorationHydrated = useRef(false);
  useEffect(() => {
    if (!feedbackExplorationQuery.data || feedbackExplorationHydrated.current) return;
    feedbackExplorationHydrated.current = true;
    setFeedbackExplorationEnabled(feedbackExplorationQuery.data.feedback_exploration_enabled === true);
  }, [feedbackExplorationQuery.data]);

  // The env kill is server state, not form state — it always reflects the
  // latest fetch so the checkbox stays honestly disabled under an active kill.
  const feedbackExplorationKilled = feedbackExplorationQuery.data?.env_kill_active === true;

  const llmHydrated = useRef(false);
  const hydrateLlmSelects = (data: LlmSettings) => {
    setOrchModel(data.orchestrator_model || '');
    setAgentModel(data.agent_model || '');
    setMemoryModel(data.memory_model || '');
  };
  useEffect(() => {
    if (!llmSettingsQuery.data || llmHydrated.current) return;
    llmHydrated.current = true;
    hydrateLlmSelects(llmSettingsQuery.data);
  }, [llmSettingsQuery.data]);

  // The memory select is read-only unless the server says it may change.
  const memorySelectable = llmSettingsQuery.data?.memory_model_selectable === true;
  const memoryLockedReason = llmSettingsQuery.data?.memory_model_locked_reason || '';
  const memoryReadError = llmSettingsQuery.data?.memory_model_error || '';
  // SSM names another model than the memory runs (the next deploy puts it back).
  const memorySaved = llmSettingsQuery.data?.memory_model_saved || '';
  // A saved value the next deploy ignores (not a memory model offered here).
  const memorySavedInvalid = llmSettingsQuery.data?.memory_model_saved_invalid || '';
  const memoryLive = llmSettingsQuery.data?.memory_model || '';
  // Nothing valid saved: the next deploy would put this model back.
  const memoryUnsaved = llmSettingsQuery.data?.memory_model_unsaved === true;
  const memoryNextDeploy = llmSettingsQuery.data?.memory_model_next_deploy || '';
  // Saving the model the memory already runs repairs what the next deploy keeps.
  const memoryRepair = memoryUnsaved || !!memorySaved || !!memorySavedInvalid;

  // The switch job: on load the latest one (a page opened while one runs
  // shows it), then the watched one, polled while it runs, for a bounded time.
  const memoryJobQuery = useQuery({
    ...memoryModelJobQueryOptions(memoryJobId),
    refetchInterval: (query) =>
      memoryJobId && !memoryPollStopped && query.state.data?.status === 'running'
        ? MEMORY_JOB_POLL_MS
        : false,
    // A read that fails (the server's 503 when its store cannot answer, or
    // no answer) is tried again a few times, then shown; polling goes on
    // while the last known state is running. A 4xx is an answer.
    retry: (failures, error) =>
      !(error instanceof ApiError && error.status >= 400 && error.status < 500) &&
      failures < MEMORY_JOB_RETRIES,
    retryDelay: MEMORY_JOB_POLL_MS,
  });
  const memoryJob = memoryJobId ? memoryJobQuery.data : undefined;
  // The last known state: a read that fails keeps the record it had.
  const memoryRunning = memoryJob?.status === 'running' && memoryJob.job_id === memoryJobId;
  const memoryJobError = memoryJobQuery.isError ? errorDetail(memoryJobQuery.error) : '';
  const watchMemoryJob = (job: MemoryModelJob) => {
    queryClient.setQueryData(queryKeys.admin.memoryModelJob(job.job_id!), job);
    setMemoryStatus({ text: '' });
    setMemoryJobId(job.job_id!);
  };
  useEffect(() => {
    const job = memoryJobQuery.data;
    if (memoryJobId || job?.status !== 'running' || !job.job_id) return;
    watchMemoryJob(job);
  }, [memoryJobQuery.data, memoryJobId]);
  useEffect(() => {
    if (!memoryJobId) return;
    setMemoryPollStopped(false);
    const timer = setTimeout(() => setMemoryPollStopped(true), MEMORY_JOB_POLL_LIMIT_MS);
    return () => clearTimeout(timer);
  }, [memoryJobId]);
  // The select shows what the server reads after a switch ends or is refused.
  const rehydrateMemory = async () => {
    const fresh = await llmSettingsQuery.refetch();
    if (fresh.data) setMemoryModel(fresh.data.memory_model || '');
  };
  // Polling cannot say how a running switch ended (its status read fails,
  // or the page stopped checking): read the settings again, once, so the
  // select shows the model the memory runs.
  const memoryUnresolved = memoryRunning && (memoryJobQuery.isError || memoryPollStopped);
  const unresolvedRead = useRef(false);
  useEffect(() => {
    if (!memoryUnresolved) {
      unresolvedRead.current = false;
      return;
    }
    if (unresolvedRead.current) return;
    unresolvedRead.current = true;
    void rehydrateMemory();
  }, [memoryUnresolved]);
  const reportedJob = useRef<string | null>(null);
  useEffect(() => {
    const job = memoryJobQuery.data;
    if (!memoryJobId || !job || job.job_id !== memoryJobId || job.status === 'running') return;
    if (reportedJob.current === memoryJobId) return;
    reportedJob.current = memoryJobId;
    const ok = job.status === 'done';
    // Unknown: the switch ended without a recorded outcome; not an error it reported.
    const unknown = job.status === 'unknown';
    setMemoryStatus({
      text:
        (ok ? '✓ ' : unknown ? '⚠ ' : 'Error: ') +
        (job.message || (ok ? 'Saved' : 'The switch did not finish')),
      color: ok ? 'var(--green, #4ade80)' : unknown ? 'var(--amber, #fbbf24)' : 'var(--red, #f87171)',
    });
    void rehydrateMemory();
  }, [memoryJobQuery.data, memoryJobId]);

  const allowlistActive = modelsQuery.data?.allowlist_active === true;
  // The HighByte token card only applies where HighByte is deployed. Hide it
  // only once quick-links has answered with no HighByte URL (deployments
  // without HighByte seed the "placeholder" sentinel, normalized to ""); while
  // loading or on error it stays, so an outage never removes the form.
  const quickLinks = quickLinksQuery.data;
  const highbyteDeployed = quickLinks === undefined || !!quickLinks.highbyte;
  const highbyteTokenSet = quickLinks?.highbyte_i3x_token === true;
  const orchOptions = useMemo(
    () => buildModelOptions(modelsQuery.data?.orchestrator || [], orchModel, allowlistActive),
    [modelsQuery.data, orchModel, allowlistActive],
  );
  const agentOptions = useMemo(
    () => buildModelOptions(modelsQuery.data?.agent || [], agentModel, allowlistActive),
    [modelsQuery.data, agentModel, allowlistActive],
  );
  const memoryOptions = useMemo(
    () => buildMemoryModelOptions(modelsQuery.data?.memory || [], memoryModel, allowlistActive),
    [modelsQuery.data, memoryModel, allowlistActive],
  );
  const packs: ConceptPack[] | 'loading' | { error: string } = packsQuery.data
    ? packsQuery.data.packs || []
    : packsQuery.isError
      ? { error: errorDetail(packsQuery.error) }
      : 'loading';

  const settingsLoadErrors = [
    brandingError ? 'Branding unavailable: ' + brandingError : '',
    settingsQuery.isError ? 'Admin settings unavailable: ' + errorDetail(settingsQuery.error) : '',
    liveUpdatesQuery.isError ? 'Live updates unavailable: ' + errorDetail(liveUpdatesQuery.error) : '',
    feedbackExplorationQuery.isError
      ? 'Feedback exploration unavailable: ' + errorDetail(feedbackExplorationQuery.error)
      : '',
  ].filter(Boolean);
  const llmLoadErrors = [
    modelsQuery.isError ? 'Model catalog unavailable: ' + errorDetail(modelsQuery.error) : '',
    llmSettingsQuery.isError ? 'LLM settings unavailable: ' + errorDetail(llmSettingsQuery.error) : '',
  ].filter(Boolean);

  // requestJson transport is centralized in admin-queries; this component owns
  // only form state, Query observers, and visible mutation outcomes.

  // ── saveDemoEnabled ─────────────────────────────────────────────────────────
  const saveDemoEnabled = async (): Promise<boolean> => {
    try {
      const result = await demoMutation.mutateAsync(demoEnabled);
      const enabled = result.demo_enabled === true;
      setDemoEnabled(enabled);
      onDemoRef.current(enabled);
      return true;
    } catch (error) {
      setSettingsStatus('Error saving demo setting: ' + errorDetail(error));
      return false;
    }
  };

  // ── saveLiveUpdatesEnabled ──────────────────────────────────────────────────
  const saveLiveUpdatesEnabled = async (): Promise<boolean> => {
    try {
      const result = await liveUpdatesMutation.mutateAsync({
        enabled: liveUpdatesEnabled,
        intervalSeconds: liveInterval,
      });
      setLiveUpdatesEnabled(result.live_updates_enabled === true);
      if (typeof result.interval_seconds === 'number') {
        setLiveInterval(result.interval_seconds);
      }
      return true;
    } catch (error) {
      setSettingsStatus('Error saving live-updates setting: ' + errorDetail(error));
      return false;
    }
  };

  // ── saveFeedbackExplorationEnabled ──────────────────────────────────────────
  const saveFeedbackExplorationEnabled = async (): Promise<boolean> => {
    // Under an active env kill the checkbox is disabled and the server would
    // refuse the write (409) — skip honestly instead of surfacing an error.
    if (feedbackExplorationKilled) return true;
    try {
      const result = await feedbackExplorationMutation.mutateAsync(feedbackExplorationEnabled);
      setFeedbackExplorationEnabled(result.feedback_exploration_enabled === true);
      return true;
    } catch (error) {
      setSettingsStatus('Error saving feedback-exploration setting: ' + errorDetail(error));
      return false;
    }
  };

  // ── saveSettings ────────────────────────────────────────────────────────────
  const saveSettings = async () => {
    const companyVal = company.trim();
    const taglineVal = tagline.trim();
    // Persist the demo + live-updates + feedback-exploration toggles
    // (independent of branding validation).
    const [demoSaved, liveSaved, feedbackSaved] = await Promise.all([
      saveDemoEnabled(),
      saveLiveUpdatesEnabled(),
      saveFeedbackExplorationEnabled(),
    ]);
    if (!demoSaved || !liveSaved || !feedbackSaved) return;
    if (!companyVal) {
      setSettingsStatus('Company name is required');
      return;
    }
    setSettingsStatus('Saving...');
    try {
      await brandingMutation.mutateAsync({ company: companyVal, tagline: taglineVal });
      setSettingsStatus('✓ Saved');
      onBrandingSaved(companyVal);
      later(3000, () => setSettingsStatus(''));
    } catch (error) {
      setSettingsStatus('Error: ' + errorDetail(error));
    }
  };

  // ── saveHighByteToken ───────────────────────────────────────────────────────
  const saveHighByteToken = async () => {
    const token = hbToken.trim();
    if (!token) {
      setTokenStatus({ text: 'Token is required' });
      return;
    }
    setTokenStatus({ text: 'Saving...' });
    try {
      const result = await tokenMutation.mutateAsync(token);
      setTokenStatus({ text: '✓ ' + (result.message || 'Saved'), color: 'var(--green, #4ade80)' });
      later(5000, () => setTokenStatus({ text: '' }));
    } catch (error) {
      setTokenStatus({
        text: 'Error: ' + errorDetail(error),
        color: 'var(--red, #f87171)',
      });
    }
  };

  // ── saveLLMSettings ─────────────────────────────────────────────────────────
  const saveLLMSettings = async () => {
    // A dropdown still showing a configured model the allowlist excludes
    // saves as '' (leave unchanged); the server would refuse the id.
    const orchVal = modelValueToSave(orchOptions, orchModel);
    const agentVal = modelValueToSave(agentOptions, agentModel);
    if (!orchVal && !agentVal) {
      setLlmStatus({
        text: orchModel.trim() || agentModel.trim()
          ? 'Choose a model offered in this deployment'
          : 'Choose at least one model',
      });
      return;
    }
    if (!llmSettingsQuery.data) {
      setLlmStatus({
        text: 'Could not load current models: ' + (llmSettingsQuery.error ? errorDetail(llmSettingsQuery.error) : 'still loading'),
        color: 'var(--red, #f87171)',
      });
      return;
    }
    const currentOrch = llmSettingsQuery.data.orchestrator_model || '';
    const currentAgent = llmSettingsQuery.data.agent_model || '';
    const orchChanged = !!orchVal && orchVal !== currentOrch;
    const agentChanged = !!agentVal && agentVal !== currentAgent;
    if (
      orchChanged &&
      !(await askConfirm({
        key: 'settings:restart-orchestrator',
        title: 'Restart the orchestrator?',
        body: 'The orchestrator runtime will be restarted. This takes about 60 seconds.',
        confirmLabel: 'Restart runtime',
      }))
    ) return;
    if (
      agentChanged &&
      !(await askConfirm({
        key: 'settings:change-agent-model',
        title: 'Change the agent model?',
        body: "Each running system agent switches to the new model in place. If one can't be switched, you'll be asked to re-onboard it.",
        confirmLabel: 'Change agent model',
      }))
    ) return;

    setLlmStatus({ text: 'Saving...' });
    try {
      const result = await llmMutation.mutateAsync({ orchestrator_model: orchVal, agent_model: agentVal });
      let msg = '✓ Saved';
      if (result.restart_required) msg += '; orchestrator restarting (~60s)';
      if (result.reonboard_required) {
        // Name the systems the in-place switch could not move (failed, or no
        // runtime); fall back to the general wording when none are listed.
        const refresh = result.agent_runtime_refresh;
        const stuck = [...(refresh?.failed ?? []), ...(refresh?.skipped ?? [])];
        msg += stuck.length
          ? `; re-onboard ${stuck.join(', ')} for the new agent model`
          : '; re-onboard systems for the new agent model';
      }
      setLlmStatus({ text: msg, color: 'var(--green, #4ade80)' });
      later(8000, () => setLlmStatus({ text: '' }));
    } catch (error) {
      setLlmStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  // ── saveMemoryModel: a background switch job with its own Save ──────────────
  const memoryToSave = memoryModelToSave(
    memoryOptions,
    memoryModel,
    memoryLive,
    memorySelectable,
    memoryRepair,
  );
  const saveMemoryModel = async () => {
    const value = memoryToSave;
    if (!value || memoryRunning) return;
    const confirmed = await askConfirm(
      value === memoryLive
        ? {
            key: 'settings:save-memory-model',
            title: 'Save the memory extraction model?',
            body: 'The memory already runs this model; nothing is switched. Saving it keeps it on the next deploy.',
            confirmLabel: 'Save memory model',
          }
        : {
            key: 'settings:change-memory-model',
            title: 'Change the memory extraction model?',
            body:
              'AgentCore Memory switches to the new model in place. The switch takes about two ' +
              'minutes and runs in the background; you can leave this page. New extractions use ' +
              'it; records already extracted stay as they are.',
            confirmLabel: 'Change memory model',
          },
    );
    if (!confirmed) return;
    setMemoryStatus({ text: 'Starting the switch...' });
    try {
      watchMemoryJob(await memoryMutation.mutateAsync(value));
    } catch (error) {
      // Refused (400/409/503) before anything changed: say why, show the live model.
      setMemoryStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
      await rehydrateMemory();
    }
  };
  const memoryRunningLabel = memoryModelLabel(modelsQuery.data?.memory || [], memoryJob?.memory_model || '');
  const memoryShown: ColoredStatus = !memoryRunning
    ? memoryJobError && !memoryStatus.text
      ? {
          text: `Could not read whether a memory model switch is running: ${memoryJobError}`,
          color: 'var(--red, #f87171)',
        }
      : memoryStatus
    : memoryPollStopped
      ? {
          text:
            `The switch to ${memoryRunningLabel} is still running; this page has stopped ` +
            'checking it. Reload the page to see how it ended.',
        }
      : memoryJobError
        ? {
            text:
              `The switch to ${memoryRunningLabel} was running when this page last read it, ` +
              `but its status cannot be read now: ${memoryJobError}`,
            color: 'var(--red, #f87171)',
          }
        : {
            text:
              `Switching the memory to ${memoryRunningLabel}... It takes about two minutes and ` +
              'runs in the background.',
          };

  // ── installConceptPack / uninstallConceptPack ───────────────────────────────
  const installConceptPack = async (packId: string) => {
    setPacksStatus({ text: `Installing ${packId}…`, color: 'var(--muted)' });
    try {
      const result = await installPackMutation.mutateAsync(packId);
      if (result.status === 'ok') {
        setPacksStatus({
          text: `✓ Installed ${result.concepts_inserted} concepts`,
          color: 'var(--green, #4ade80)',
        });
      } else {
        setPacksStatus({ text: result.detail || result.message || 'Error', color: 'var(--red, #f87171)' });
      }
    } catch (error) {
      setPacksStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  const uninstallConceptPack = async (packId: string) => {
    const uninstall = await askConfirm({
      key: `settings:uninstall-pack:${packId}`,
      title: `Uninstall the ${packId} concept pack?`,
      body: 'Concepts that are mapped to system fields will be preserved.',
      confirmLabel: 'Uninstall pack',
      tone: 'danger',
    });
    if (!uninstall) return;
    setPacksStatus({ text: `Uninstalling ${packId}…`, color: 'var(--muted)' });
    try {
      const result = await uninstallPackMutation.mutateAsync(packId);
      if (result.status === 'ok') {
        let msg = `✓ Removed ${result.deleted} concepts`;
        if (result.preserved_due_to_mappings) msg += ` (${result.preserved_due_to_mappings} preserved — still mapped)`;
        setPacksStatus({ text: msg, color: 'var(--green, #4ade80)' });
      } else {
        setPacksStatus({ text: result.detail || result.message || 'Error', color: 'var(--red, #f87171)' });
      }
    } catch (error) {
      setPacksStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  const savingSettings =
    demoMutation.isPending ||
    liveUpdatesMutation.isPending ||
    feedbackExplorationMutation.isPending ||
    brandingMutation.isPending;
  const savingToken = tokenMutation.isPending;
  const savingLlm = llmMutation.isPending;

  return (
    <div className="settings-grid">
      <section className="settings-section">
      <h3 style={h3Style}>General</h3>
      {settingsLoadErrors.length > 0 && (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)', marginBottom: '1rem' }}>
          {settingsLoadErrors.join(' · ')}{' '}
          <button
            type="button"
            onClick={() => {
              if (settingsQuery.isError) void settingsQuery.refetch();
              if (liveUpdatesQuery.isError) void liveUpdatesQuery.refetch();
              if (feedbackExplorationQuery.isError) void feedbackExplorationQuery.refetch();
            }}
          >
            Retry
          </button>
        </div>
      )}
      <div style={{ marginBottom: '1rem' }}>
        <label style={fieldLabelStyle}>Company Name</label>
        <input
          id="settings-company"
          type="text"
          placeholder="e.g. AnyCompany Manufacturing"
          style={inputStyle}
          value={company}
          onChange={(e) => setCompany(e.target.value)}
        />
      </div>
      <div style={{ marginBottom: '1.25rem' }}>
        <label style={fieldLabelStyle}>Tagline</label>
        <input
          id="settings-tagline"
          type="text"
          placeholder="e.g. Manufacturing Intelligence"
          style={inputStyle}
          value={tagline}
          onChange={(e) => setTagline(e.target.value)}
        />
      </div>
      {/* The three runtime feature toggles, grouped under one open disclosure.
          Ids/testids are unchanged — tests pin them. */}
      <details open style={{ marginBottom: '1.25rem' }}>
        <summary
          style={{ fontSize: '0.8rem', fontWeight: 600, cursor: 'pointer', marginBottom: '0.9rem' }}
        >
          Feature toggles
        </summary>
      <div style={{ marginBottom: '1.25rem' }}>
        <label style={checkboxLabelStyle}>
          <input
            id="settings-demo-enabled"
            type="checkbox"
            style={checkboxStyle}
            checked={demoEnabled}
            onChange={(e) => setDemoEnabled(e.target.checked)}
          />
          Enable Demo Mode
        </label>
        <p style={helpTextStyle}>
          Seeds the suggested demo prompts and shows the Demo button and Demo Script tab. Turning
          it off removes the seeded prompts and hides them.
        </p>
      </div>
      <div style={{ marginBottom: '1.25rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <label style={checkboxLabelStyle}>
            <input
              id="settings-live-updates-enabled"
              type="checkbox"
              style={checkboxStyle}
              checked={liveUpdatesEnabled}
              onChange={(e) => setLiveUpdatesEnabled(e.target.checked)}
            />
            Enable Live Updates
          </label>
          <select
            id="settings-live-updates-interval"
            aria-label="Live update interval"
            disabled={!liveUpdatesEnabled}
            value={String(liveInterval)}
            onChange={(e) => setLiveInterval(Number(e.target.value))}
            style={{ ...inputStyle, width: 'auto', padding: '0.25rem 0.4rem', fontSize: '0.75rem' }}
          >
            {liveIntervalChoices(liveInterval).map((c) => (
              <option key={c.value} value={String(c.value)}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
        <p style={helpTextStyle}>
          Auto-refreshes dashboard cards in the background with recurring queries against your
          connected systems — these can incur cost. Turn it off to pause all background refresh;
          cards then load only on demand when someone opens the dashboard. Takes effect
          immediately, no redeploy.
        </p>
      </div>
      <div style={{ marginBottom: '1.25rem' }}>
        <label style={{ ...checkboxLabelStyle, cursor: feedbackExplorationKilled ? 'not-allowed' : 'pointer' }}>
          <input
            id="settings-feedback-exploration-enabled"
            type="checkbox"
            style={checkboxStyle}
            checked={feedbackExplorationEnabled}
            disabled={feedbackExplorationKilled}
            onChange={(e) => setFeedbackExplorationEnabled(e.target.checked)}
          />
          Enable Feedback Exploration
        </label>
        <p style={helpTextStyle}>
          Lets a thumbs-down with a comment trigger ONE bounded background re-examination of the
          question; a routing divergence files a review-queue proposal (never auto-accepted).
          Default off. Takes effect immediately, no redeploy.
        </p>
        {feedbackExplorationKilled ? (
          <p id="settings-feedback-exploration-kill-note" role="note" style={{ ...helpTextStyle, color: 'var(--red, #f87171)' }}>
            Disabled by the environment kill switch (FEEDBACK_EXPLORATION_ENABLED). This toggle
            cannot override it — clear the environment variable to re-enable the control.
          </p>
        ) : feedbackExplorationQuery.data?.source === 'admin-flag' ? (
          <p id="settings-feedback-exploration-provenance" style={helpTextStyle}>
            Current value was set here by an administrator.
          </p>
        ) : null}
      </div>
      </details>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <button
          id="settings-save-btn"
          className="btn-primary"
          disabled={savingSettings}
          onClick={() => void saveSettings()}
        >
          Save
        </button>
        <span id="settings-status" style={statusStyle}>
          {settingsStatus}
        </span>
      </div>
      <p style={{ marginTop: '1.25rem', fontSize: '0.72rem', color: 'var(--muted)', lineHeight: 1.5 }}>
        Changes take effect immediately in the header, footer, and AI briefing prompt. The app
        will reload branding automatically.
      </p>
      </section>

      {highbyteDeployed && (
      <section className="settings-section">
      <h3 style={h3Style}>HighByte MCP Token</h3>
      <p style={sectionHelpStyle}>
        Paste the Bearer token from HighByte UI (Settings → Users → administrator → API Keys).
        This enables the HighByte MCP connection for onboarding.
        {highbyteTokenSet && (
          <>
            {' '}
            <span id="settings-highbyte-token-set">A token is already set.</span>
          </>
        )}
      </p>
      <div style={{ marginBottom: '1rem' }}>
        <label style={fieldLabelStyle}>Bearer Token</label>
        <input
          id="settings-highbyte-token"
          type="text"
          placeholder="e.g. your-highbyte-api-token"
          style={monoInputStyle}
          value={hbToken}
          onChange={(e) => setHbToken(e.target.value)}
        />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <button
          id="settings-token-btn"
          className="btn-primary"
          disabled={savingToken}
          onClick={() => void saveHighByteToken()}
        >
          Save Token
        </button>
        <span id="settings-token-status" style={{ ...statusStyle, color: tokenStatus.color ?? statusStyle.color }}>
          {tokenStatus.text}
        </span>
      </div>
      </section>
      )}

      <section className="settings-section">
      <h3 style={h3Style}>LLM Configuration</h3>
      {llmLoadErrors.length > 0 && (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)', marginBottom: '1rem' }}>
          {llmLoadErrors.join(' · ')}{' '}
          <button
            type="button"
            onClick={() => {
              if (modelsQuery.isError) void modelsQuery.refetch();
              if (llmSettingsQuery.isError) void llmSettingsQuery.refetch();
            }}
          >
            Retry
          </button>
        </div>
      )}
      <p style={sectionHelpStyle}>
        Set the default models for the orchestrator and system agents. Saving switches the
        running agents over; an orchestrator change takes about a minute. The price next to
        each model is its Amazon Bedrock on-demand list price in USD per million tokens, shown
        as an estimate; see{' '}
        <a
          id="settings-model-pricing-link"
          href={BEDROCK_PRICING_URL}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: 'var(--accent)' }}
        >
          Amazon Bedrock pricing<span className="visually-hidden"> (opens in a new tab)</span>
        </a>{' '}
        for current rates.
        {allowlistActive && (
          <>
            {' '}
            This deployment limits the models you can choose; the others are shown greyed out.
          </>
        )}
      </p>
      <div style={{ marginBottom: '1rem' }}>
        <label style={fieldLabelStyle}>Orchestrator Model</label>
        <select
          id="settings-orch-model"
          style={monoInputStyle}
          value={orchModel}
          onChange={(e) => setOrchModel(e.target.value)}
        >
          {orchOptions.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ marginBottom: '1.25rem' }}>
        <label style={fieldLabelStyle}>System Agent Model</label>
        <select
          id="settings-agent-model"
          style={monoInputStyle}
          value={agentModel}
          onChange={(e) => setAgentModel(e.target.value)}
        >
          {agentOptions.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <button
          id="settings-llm-btn"
          className="btn-primary"
          disabled={savingLlm}
          onClick={() => void saveLLMSettings()}
        >
          Save Models
        </button>
        <span id="settings-llm-status" style={{ ...statusStyle, color: llmStatus.color ?? statusStyle.color }}>
          {llmStatus.text}
        </span>
      </div>
      {/* The memory extraction model has its own Save: its switch takes about
          two minutes, so it runs as a background job the page polls. */}
      <div style={{ marginTop: '1.5rem' }}>
        <label style={fieldLabelStyle} htmlFor="settings-memory-model">
          Memory Extraction Model
        </label>
        <select
          id="settings-memory-model"
          style={monoInputStyle}
          value={memoryModel}
          disabled={!memorySelectable || memoryRunning}
          aria-describedby="settings-memory-model-help"
          onChange={(e) => setMemoryModel(e.target.value)}
        >
          {memoryOptions.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        </select>
        <p id="settings-memory-model-help" style={helpTextStyle}>
          The model AgentCore Memory uses to turn conversations into facts, preferences and
          summaries. A change applies to new extractions; existing records stay.
        </p>
        {!memorySelectable && memoryLockedReason && (
          <p id="settings-memory-model-locked" style={helpTextStyle}>
            {memoryLockedReason}
          </p>
        )}
        {memorySaved && memoryLive && (
          <p id="settings-memory-model-saved" style={helpTextStyle}>
            {`Saved for the next deploy: ${memoryModelLabel(modelsQuery.data?.memory || [], memorySaved)}. ` +
              `The memory runs ${memoryModelLabel(modelsQuery.data?.memory || [], memoryLive)} now.`}
          </p>
        )}
        {memorySavedInvalid && (
          <p id="settings-memory-model-saved-invalid" style={helpTextStyle}>
            {`A saved memory model (${memorySavedInvalid}) is not one this deployment offers; ` +
              'the next deploy ignores it.'}
          </p>
        )}
        {memoryUnsaved && memoryLive && memoryNextDeploy && (
          <p id="settings-memory-model-unsaved" style={helpTextStyle}>
            {`The memory runs ${memoryModelLabel(modelsQuery.data?.memory || [], memoryLive)}, but ` +
              'nothing is saved for the next deploy, which would put ' +
              `${memoryModelLabel(modelsQuery.data?.memory || [], memoryNextDeploy)} back.` +
              (memoryLive === MEMORY_MODEL_MIXED
                ? ''
                : ` Save ${memoryModelLabel(modelsQuery.data?.memory || [], memoryLive)} to keep it.`)}
          </p>
        )}
        {memoryReadError && (
          <p
            id="settings-memory-model-error"
            role="alert"
            style={{ ...helpTextStyle, color: 'var(--red, #f87171)' }}
          >
            Could not read the memory&apos;s current model: {memoryReadError}
          </p>
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginTop: '0.75rem' }}>
          <button
            id="settings-memory-model-btn"
            className="btn-primary"
            disabled={!memoryToSave || memoryRunning || memoryMutation.isPending}
            onClick={() => void saveMemoryModel()}
          >
            Save Memory Model
          </button>
          <span
            id="settings-memory-model-status"
            role="status"
            style={{ ...statusStyle, color: memoryShown.color ?? statusStyle.color }}
          >
            {memoryShown.text}
          </span>
        </div>
      </div>
      </section>

      <section className="settings-section">
      <h3 style={{ ...h3Style, margin: '0 0 0.5rem' }}>Industry Concept Packs</h3>
      <p style={sectionHelpStyle}>
        The orchestrator&apos;s knowledge graph ships with ~67 universal manufacturing concepts.
        Install an industry pack to add domain-specific vocabulary the orchestrator can reason
        about. Packs are additive — you can install multiple, and uninstalling preserves any
        concept currently mapped to a system field.
      </p>
      <div
        id="concept-packs-list"
        style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginBottom: '1rem' }}
      >
        {packs === 'loading' ? (
          <div style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>Loading packs…</div>
        ) : Array.isArray(packs) ? (
          packs.length ? (
            packs.map((p) => <PackRow key={p.id} pack={p} onInstall={installConceptPack} onUninstall={uninstallConceptPack} />)
          ) : (
            <div style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>No packs available.</div>
          )
        ) : (
          <div style={{ fontSize: '0.75rem', color: 'var(--red, #f87171)' }}>
            Error loading packs: {escapeHtml(packs.error)}{' '}
            <button type="button" onClick={() => void packsQuery.refetch()}>Retry</button>
          </div>
        )}
      </div>
      <span
        id="concept-packs-status"
        style={{ ...statusStyle, color: packsStatus.color ?? statusStyle.color }}
      >
        {packsStatus.text}
      </span>
      </section>

      <section className="settings-section">
      <CalendarPolicySection />
      </section>

      <section className="settings-section">
      <SnapshotSection />
      </section>
    </div>
  );
}

/** One concept-pack row — verbatim markup/styles from loadConceptPacks(). */
function PackRow({
  pack,
  onInstall,
  onUninstall,
}: {
  pack: ConceptPack;
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
}) {
  const isInst = !!pack.is_installed;
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0.6rem 0.75rem',
        background: 'var(--surface2)',
        border: '1px solid var(--border)',
        borderRadius: 6,
      }}
    >
      <div>
        <div style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--text)' }}>{pack.name}</div>
        <div style={{ marginTop: '0.1rem' }}>
          {isInst ? (
            <span style={{ color: 'var(--green, #4ade80)', fontSize: '0.7rem' }}>
              ✓ Installed ({pack.concepts_installed} concepts)
            </span>
          ) : (
            <span style={{ color: 'var(--muted)', fontSize: '0.7rem' }}>
              Not installed ({pack.concepts_in_file} concepts available)
            </span>
          )}
        </div>
      </div>
      <button
        data-pack-id={pack.id}
        className={isInst ? undefined : 'btn-primary'}
        onClick={() => (isInst ? onUninstall(pack.id) : onInstall(pack.id))}
        style={
          isInst
            ? {
                background: 'var(--surface2)',
                color: 'var(--text)',
                border: '1px solid var(--border)',
                padding: '0.35rem 0.85rem',
                borderRadius: 4,
                fontSize: '0.78rem',
                cursor: 'pointer',
                fontWeight: 600,
              }
            : undefined
        }
      >
        {isInst ? 'Uninstall' : 'Install'}
      </button>
    </div>
  );
}
