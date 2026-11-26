// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export type ToolIdentityShape = 'gateway' | 'query' | 'plain';

export interface ToolIdentity {
  raw: string;
  shape: ToolIdentityShape;
  /** Gateway system id or local query target, without a query_ prefix. */
  systemId: string | null;
  /** Gateway operation after the first triple-underscore separator. */
  operation: string | null;
  /** Common leading system family (for example mes or erp). */
  family: string | null;
  /** Site/system qualifier after the family, retaining its source separator. */
  qualifier: string | null;
  separator: '-' | '_' | null;
}

function splitSystem(systemId: string, separator: '-' | '_'): Pick<ToolIdentity, 'family' | 'qualifier' | 'separator'> {
  const index = systemId.indexOf(separator);
  if (index < 0) return { family: systemId || null, qualifier: null, separator: null };
  return {
    family: systemId.slice(0, index) || null,
    qualifier: systemId.slice(index + 1) || null,
    separator,
  };
}

/** Parse identity only; dashboard and chat retain their distinct presentation. */
export function parseToolIdentity(toolName: string): ToolIdentity {
  const raw = String(toolName || '');
  const gatewaySeparator = raw.indexOf('___');
  if (gatewaySeparator >= 0) {
    const systemId = raw.slice(0, gatewaySeparator);
    // Match the callers' historical split('___')[1] contract even for
    // malformed names containing more than one separator.
    const operation = raw.slice(gatewaySeparator + 3).split('___')[0];
    return {
      raw,
      shape: 'gateway',
      systemId,
      operation,
      ...splitSystem(systemId, '-'),
    };
  }
  if (raw.startsWith('query_')) {
    const systemId = raw.slice(6);
    return {
      raw,
      shape: 'query',
      systemId,
      operation: raw,
      ...splitSystem(systemId, '_'),
    };
  }
  return {
    raw,
    shape: 'plain',
    systemId: null,
    operation: null,
    family: null,
    qualifier: null,
    separator: null,
  };
}
