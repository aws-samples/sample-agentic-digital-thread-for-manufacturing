// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** Central query-key factories for cacheable server state. */
export const queryKeys = {
  admin: {
    settings: () => ['admin', 'settings'] as const,
    models: () => ['admin', 'models'] as const,
    liveUpdates: () => ['admin', 'live-updates'] as const,
    feedbackExploration: () => ['admin', 'feedback-exploration'] as const,
    llmSettings: () => ['admin', 'llm-settings'] as const,
    /** A memory extraction model switch job; 'current' is the latest one.
     * Outside ['admin', 'llm-settings'] so a settings invalidation does not
     * refetch it. */
    memoryModelJob: (jobId: string) => ['admin', 'memory-model-job', jobId] as const,
    conceptPacks: () => ['admin', 'concept-packs'] as const,
    vocabularyConcepts: () => ['admin', 'vocabulary', 'concepts'] as const,
    vocabularyRetirementCandidates: () =>
      ['admin', 'vocabulary', 'retirement-candidates'] as const,
    vocabularyValueMappings: () => ['admin', 'vocabulary', 'value-mappings'] as const,
    vocabularyMetrics: () => ['admin', 'vocabulary', 'metrics'] as const,
    vocabularyMetricPreview: (mappingId: string) =>
      ['admin', 'vocabulary', 'metric-preview', mappingId] as const,
    vocabularyGuidance: () => ['admin', 'vocabulary', 'query-guidance'] as const,
    vocabularyGuidanceProposals: () => ['admin', 'vocabulary', 'guidance-proposals'] as const,
    guidanceReconciliation: () => ['admin', 'vocabulary', 'guidance-reconciliation'] as const,
    guidancePreview: (mappingId: string) =>
      ['admin', 'vocabulary', 'guidance-preview', mappingId] as const,
    contextPlacementJob: (jobId: string) =>
      ['admin', 'learning', 'context-placement', jobId] as const,
    quickLinks: () => ['admin', 'quick-links'] as const,
    conversations: (days: number, user: string) =>
      ['admin', 'conversations', days, user] as const,
    fleet: (hours: number) => ['admin', 'fleet', hours] as const,
    neptune: (hours: number) => ['admin', 'neptune', hours] as const,
    logTail: () => ['admin', 'log-tail'] as const,
    traceSessions: (hours: number) => ['admin', 'traces', 'sessions', hours] as const,
    traceSession: (sessionId: string, hours: number) =>
      ['admin', 'traces', 'session', sessionId, hours] as const,
    evalRuns: () => ['admin', 'evals', 'runs'] as const,
    evalRun: (runId: string) => ['admin', 'evals', 'run', runId] as const,
    evalQueue: () => ['admin', 'evals', 'queue'] as const,
    /** The findings ledger; the digest key nests under it so one
     * invalidation refreshes both (ADR-0062 D6). */
    evalFindings: () => ['admin', 'evals', 'findings'] as const,
    evalFindingsDigest: (windowDays: number) =>
      ['admin', 'evals', 'findings', 'digest', windowDays] as const,
    evalFinding: (findingId: string) => ['admin', 'evals', 'finding', findingId] as const,
    evalAuditStatus: () => ['admin', 'evals', 'audit', 'status'] as const,
    /** ONE key shared by the admin Workshop tab and the header plant-clock chip (D7). */
    workshopStory: () => ['admin', 'workshop', 'story'] as const,
  },
  systems: () => ['systems'] as const,
  catalog: () => ['onboard', 'catalog'] as const,
  concepts: () => ['onboard', 'concepts'] as const,
  prompts: () => ['prompts'] as const,
  branding: () => ['branding'] as const,
  version: () => ['version'] as const,
  demoStatus: () => ['demo', 'status'] as const,
  onboardStatus: () => ['onboard', 'status'] as const,
  authMe: () => ['auth', 'me'] as const,
  thread: {
    entities: () => ['thread', 'entities'] as const,
    graphSummary: () => ['thread', 'graph-summary'] as const,
    analytics: () => ['thread', 'analytics'] as const,
  },
} as const;
