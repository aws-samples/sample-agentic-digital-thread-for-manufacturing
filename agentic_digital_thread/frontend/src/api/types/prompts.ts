// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the prompt-library endpoints — derived from the client usage
 * (initPromptLibrary / openSaveModal / saveSaveModal). */

/** One prompt (GET /api/prompts). `source === 'registry'` groups under
 * "Registry"; everything else under "Saved". */
export interface Prompt {
  id?: string;
  title?: string;
  text?: string;
  display_text?: string;
  category?: string;
  pinned?: boolean;
  source?: string;
  required_systems?: string[];
}

/** POST /api/prompts */
export interface SavePromptRequest {
  text: string;
  title: string;
  category?: string;
  pinned?: boolean;
  required_systems?: string[];
}

export interface SavePromptResponse {
  id?: string;
  [k: string]: unknown;
}

/** POST /api/prompts/synthesize — compose a prompt from the conversation. */
export interface SynthesizeRequest {
  messages: Array<{ role: string; content: string }>;
}

export interface SynthesizeResponse {
  prompt?: string;
}

/** POST /api/prompts/{id}/schedule */
export interface ScheduleRequest {
  schedule_text: string;
}

export interface ScheduleResponse {
  cron?: string;
}

/** GET /api/onboard/status subset the prompt library reads (system dimming). */
export interface OnboardStatusSystems {
  onboarded_systems?: Array<{ id?: string; system_type?: string }>;
}
