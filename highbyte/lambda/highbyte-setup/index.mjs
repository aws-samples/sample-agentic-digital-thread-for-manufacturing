// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * HighByte Setup Lambda
 * 
 * This Lambda configures HighByte Intelligence Hub via REST API:
 * 1. Wait for HighByte to be ready (with retries)
 * 2. Login with empty password (fresh install) or stored password
 * 3. Change admin password to the one stored in Secrets Manager (if fresh install)
 * 4. Import project configuration (with OPC UA endpoint and PostgreSQL credentials substituted)
 * 
 * Note: HighByte API does not support creating/reading API keys programmatically.
 * To use the MCP server, create an API key for the administrator user in the 
 * HighByte UI (Settings → Users → administrator → API Keys).
 */

import { SecretsManagerClient, GetSecretValueCommand, PutSecretValueCommand, CreateSecretCommand } from "@aws-sdk/client-secrets-manager";
import { adminUserUpdatePath, sitewisePipelineFromEnv, transformProject, HIGHBYTE_API_VERSION } from "./project-transform.mjs";
import { reconcileMcpCredential, reconcileDeadline, SETUP_RESERVE_MS } from "./mcp-credential.mjs";

// ---------------------------------------------------------------------------
// Handler time budget. The setup custom resource
// runs in a 5-minute Lambda behind the CDK Provider framework, which reports
// FAILED to CloudFormation when this handler REJECTS. If the invocation is
// instead killed by the Lambda timeout, the timeout -- not a caught error --
// decides the outcome and any key this run owns can be left behind. So the
// handler has ONE absolute deadline (the invocation's remaining time minus
// RESPONSE_MARGIN_MS) and EVERY HighByte request and EVERY Secrets Manager
// call goes through it: each request's timeout is min(its cap, time left), and
// once less than MIN_REQUEST_MS is left the next call throws a
// HandlerDeadlineError instead of starting. Nothing waits uncapped.
// ---------------------------------------------------------------------------

/** Kept at the end of the invocation so the rejection reaches the Provider. */
export const RESPONSE_MARGIN_MS = 5000;
/** Ceiling for a single HighByte request or Secrets Manager call. */
const REQUEST_CAP_MS = 10000;
/** No call is started with less than this much handler time left. */
const MIN_REQUEST_MS = 1000;
/** Secrets Manager client config. The asset ships no node_modules, so the
 *  Lambda runtime's own AWS SDK v3 runs, and its minor version varies by
 *  runtime and Region; only options every SDK v3 accepts are set. No
 *  requestHandler object: each call is bounded by its per-call abortSignal
 *  from the handler deadline, which also caps the total across the attempts. */
export const SM_CLIENT_CONFIG = {
    maxAttempts: 2,
};

export class HandlerDeadlineError extends Error {
    constructor(message) { super(message); this.name = 'HandlerDeadlineError'; }
}

/** The handler's absolute deadline. */
function handlerBudget(endMs) {
    return {
        end: endMs,
        /** min(cap, time left) for the next call; throws once the margin is gone. */
        timeout(what, cap = REQUEST_CAP_MS) {
            const left = endMs - Date.now();
            if (left < MIN_REQUEST_MS) {
                throw new HandlerDeadlineError(
                    `HighByte setup ran out of its time budget before ${what} `
                    + `(${Math.max(0, Math.round(left))} ms left before the ${RESPONSE_MARGIN_MS} ms `
                    + 'response margin); failing now so CloudFormation gets a FAILED response');
            }
            return Math.min(cap, left);
        },
    };
}

function isAbort(e) {
    return e && (e.name === 'TimeoutError' || e.name === 'AbortError');
}

/** fetch bounded by the handler budget; a timeout becomes a clear error. */
async function hbFetch(budget, what, url, init = {}, cap = REQUEST_CAP_MS) {
    const ms = budget.timeout(what, cap);
    try {
        return await fetch(url, { ...init, signal: AbortSignal.timeout(ms) });
    } catch (e) {
        if (isAbort(e)) {
            const err = new Error(`${what} timed out after ${ms} ms (setup time budget)`);
            err.name = 'TimeoutError';
            throw err;
        }
        throw e;
    }
}

/** Secrets Manager send bounded by the handler budget (and an optional cap). */
async function smSend(budget, client, command, what, cap = REQUEST_CAP_MS) {
    const ms = budget.timeout(what, cap);
    try {
        return await client.send(command, { abortSignal: AbortSignal.timeout(ms) });
    } catch (e) {
        if (isAbort(e)) {
            const err = new Error(`${what} timed out after ${ms} ms (setup time budget)`);
            err.name = 'TimeoutError';
            throw err;
        }
        throw e;
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForHighByte(budget, endpoint, maxAttempts = 60, deadline = Infinity) {
    console.log(`Waiting for HighByte to be ready at ${endpoint}...`);
    
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        // Stop waiting at the readiness deadline:
        // 60 x (5 s probe + 5 s sleep) could otherwise outlast the 5-minute
        // Lambda timeout and kill the invocation without a clear error.
        if (Date.now() + 10000 > deadline) break;
        try {
            const response = await hbFetch(budget, 'the HighByte readiness probe',
                endpoint + '/ui/', { method: 'GET' }, 5000);
            
            if (response.ok || response.status === 401 || response.status === 403) {
                console.log(`HighByte is ready after ${attempt} attempts`);
                return true;
            }
            console.log(`Attempt ${attempt}/${maxAttempts}: HighByte returned status ${response.status}`);
        } catch (e) {
            if (e instanceof HandlerDeadlineError) throw e;
            console.log(`Attempt ${attempt}/${maxAttempts}: HighByte not ready yet - ${e.message}`);
        }
        
        await sleep(5000);
    }
    
    throw new Error(`HighByte did not become ready within the setup time budget (${maxAttempts} attempts max)`);
}

/** HighByte rejected the credentials (as opposed to a timeout or transport error). */
class LoginRejected extends Error {
    constructor(message) { super(message); this.name = 'LoginRejected'; }
}

async function loginHighbyte(budget, endpoint, password) {
    const authResponse = await hbFetch(budget, 'logging in to HighByte (login)', endpoint + '/login', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            username: 'administrator',
            password: password
        })
    });

    if (!authResponse.ok) {
        throw new LoginRejected(`Failed to authenticate with HighByte: STATUS: ${authResponse.status}, MESSAGE: ${authResponse.statusText}`);
    }

    let authResponseJson = JSON.parse(await authResponse.text());
    if (!authResponseJson.success) {
        throw new LoginRejected(`Failed to authenticate with HighByte: ${authResponseJson.message}`);
    }

    return authResponseJson.token;
}

export const handler = async (event, context) => {
    console.log('HighByte Setup Lambda started');
    console.log('Event:', JSON.stringify(event, null, 2));

    // One wall-clock budget for the whole invocation, binding every call,
    // from the
    // invocation's own remaining time so it tracks the configured Lambda
    // timeout. budget.end keeps RESPONSE_MARGIN_MS for the rejection to reach
    // the Provider. The MCP reconcile must finish by mcpDeadline, which keeps
    // SETUP_RESERVE_MS back for the database-secret reads, project fetch +
    // import; cleanup of a key the reconcile owns may run up to budget.end.
    // The hub wait stops early enough to leave the reconcile at least a minute.
    const startMs = Date.now();
    const remainingMs = (context && typeof context.getRemainingTimeInMillis === 'function')
        ? context.getRemainingTimeInMillis() : 300000;
    const budget = handlerBudget(startMs + remainingMs - RESPONSE_MARGIN_MS);
    const mcpDeadline = reconcileDeadline(startMs, remainingMs);
    const hubWaitDeadline = mcpDeadline - 60000;
    console.log(`Time budget: ${remainingMs} ms; MCP reconcile deadline in ${mcpDeadline - startMs} ms `
        + `(reserve ${SETUP_RESERVE_MS} ms for project import + response)`);

    // Handle CloudFormation Custom Resource delete - just return success
    if (event.RequestType === 'Delete') {
        console.log('Delete request - nothing to clean up');
        return {
            statusCode: 200,
            body: JSON.stringify({ message: 'Delete successful' }),
            Data: { BearerToken: 'DELETED' }
        };
    }

    const SECRET_HIGHBYTE_PASSWORD = process.env.SECRET_HIGHBYTE_PASSWORD;
    const HIGHBYTE_ENDPOINT = process.env.HIGHBYTE_ENDPOINT;
    const HIGHBYTE_ALB_ENDPOINT = process.env.HIGHBYTE_ALB_ENDPOINT;
    const OPC_UA_ENDPOINT = process.env.OPC_UA_ENDPOINT;
    const OPC_UA_ENDPOINT_2 = process.env.OPC_UA_ENDPOINT_2 || '';
    const OPC_UA_ENDPOINT_3 = process.env.OPC_UA_ENDPOINT_3 || '';
    const OPC_UA_ENDPOINT_4 = process.env.OPC_UA_ENDPOINT_4 || '';
    const PROJECT_CONFIG_URL = process.env.PROJECT_CONFIG_URL;
    const AURORA_SECRET_ARN = process.env.AURORA_SECRET_ARN;
    // Secret to store the auto-created MCP bearer token in: the canonical
    // conn/highbyte JSON secret ({"token": "<jwt>"}) read by the ADT app
    // (catalog._get_highbyte_token / resolve_connection parse JSON .token).
    const MCP_TOKEN_SECRET = process.env.MCP_TOKEN_SECRET || 'adt/conn/highbyte';
    // The hub MCP endpoint the reconciler calls to PROVE a key authenticates
    // (ALB :8885/mcp). Required: without it the MCP credential cannot be
    // verified, so the setup must fail rather than claim a key it can't check.
    const MCP_VERIFY_ENDPOINT = process.env.MCP_VERIFY_ENDPOINT;
    // 'disabled' (the workshop: SFC is the only SiteWise writer, ADR-0082)
    // imports NameSpace_to_SiteWise with its triggers off. Read up front so a
    // typo fails the deploy before anything is changed on the hub.
    const SITEWISE_PIPELINE = sitewisePipelineFromEnv(process.env.HIGHBYTE_SITEWISE_PIPELINE);

    console.log(`HIGHBYTE_ENDPOINT: ${HIGHBYTE_ENDPOINT}`);
    console.log(`HIGHBYTE_ALB_ENDPOINT: ${HIGHBYTE_ALB_ENDPOINT}`);
    console.log(`OPC_UA_ENDPOINT: ${OPC_UA_ENDPOINT}`);
    console.log(`PROJECT_CONFIG_URL: ${PROJECT_CONFIG_URL}`);
    console.log(`AURORA_SECRET_ARN: ${AURORA_SECRET_ARN}`);
    console.log(`HIGHBYTE_SITEWISE_PIPELINE: ${SITEWISE_PIPELINE}`);

    // Use direct ALB endpoint for config API calls (port 45245)
    // CloudFront routes /v1/* to port 8885 (REST Data) which is wrong for config API
    const apiEndpoint = HIGHBYTE_ALB_ENDPOINT || HIGHBYTE_ENDPOINT;
    const smClient = new SecretsManagerClient(SM_CLIENT_CONFIG);

    try {
        // Wait for HighByte to be ready
        await waitForHighByte(budget, apiEndpoint, 60, hubWaitDeadline);

        // Get HighByte password from Secrets Manager
        const highbytePasswordResponse = await smSend(budget, smClient,
            new GetSecretValueCommand({ SecretId: SECRET_HIGHBYTE_PASSWORD }),
            'reading the HighByte admin password secret');
        const highbytePassword = highbytePasswordResponse.SecretString;
        console.log('Retrieved HighByte password from Secrets Manager');

        // Login with empty password (fresh install) or stored password (already configured)
        let highbyteToken;
        let alreadyConfigured = false;
        
        try {
            highbyteToken = await loginHighbyte(budget, apiEndpoint, '');
            console.log('Successfully authenticated with empty password (fresh install)');
        } catch (e) {
            // Only a credential rejection means "already configured"; a
            // timeout or transport error is a real failure, not a fallback.
            if (!(e instanceof LoginRejected)) throw e;
            console.log('Empty password failed, trying stored password...');
            highbyteToken = await loginHighbyte(budget, apiEndpoint, highbytePassword);
            console.log('Successfully authenticated with stored password - HighByte already configured');
            alreadyConfigured = true;
        }

        // Fetch the administrator user — needed for the password change (fresh
        // installs) AND for the MCP key path on every run.
        const getAdminResponse = await hbFetch(budget, 'fetching the HighByte admin user',
            apiEndpoint + '/v1/user/administrator', {
                method: 'GET',
                headers: { 'Authorization': highbyteToken },
            });
        if (!getAdminResponse.ok) {
            throw new Error(`Failed to fetch Admin user: STATUS: ${getAdminResponse.status}`);
        }
        const adminUser = await getAdminResponse.json();

        // Change admin password (only if fresh install)
        // ALWAYS re-import project config regardless — ensures OPC UA endpoints,
        // Bedrock region, and pipelines are always up to date on every deploy.
        if (!alreadyConfigured) {
            console.log('Changing admin password...');

            const updatePasswordResponse = await hbFetch(budget, 'changing the HighByte admin password',
                apiEndpoint + adminUserUpdatePath(adminUser),
                {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': highbyteToken
                    },
                    body: JSON.stringify({
                        username: adminUser.username,
                        password: highbytePassword,
                        policyVersion: adminUser.policyVersion,
                        roles: adminUser.roles,
                        claims: []
                    })
                }
            );

            if (!updatePasswordResponse.ok) {
                throw new Error(`Failed to update Admin password: STATUS: ${updatePasswordResponse.status}`);
            }

            console.log('Successfully changed admin password');

            // Login with new password
            highbyteToken = await loginHighbyte(budget, apiEndpoint, highbytePassword);
            console.log('Successfully authenticated with new password');
        }

        // REQUIRED MCP credential postcondition — runs on EVERY Create/Update,
        // not just fresh installs (the admin password alone used to make a
        // reconfigure skip this, so a missing/stale key was never repaired and
        // the custom resource still reported success). After this returns, the
        // conn/highbyte secret exists AND its token authenticates against the
        // hub's MCP server; otherwise it throws and the custom resource fails.
        // MCP_VERIFY_ENDPOINT is the hub's MCP endpoint (ALB :8885/mcp); without
        // it we cannot prove the key works, so treat its absence as fatal.
        if (!MCP_VERIFY_ENDPOINT) {
            throw new Error('MCP_VERIFY_ENDPOINT is not set — cannot verify the MCP credential');
        }
        // Secrets facade for the reconcile: every call bound by the handler
        // budget AND by the reconcile's own timeout (o.timeoutMs), so a slow
        // read/write ends by the reconcile deadline and leaves the reconcile
        // time to delete a key it could not persist.
        const secretsFacade = {
            get: async (id, o = {}) => (await smSend(budget, smClient,
                new GetSecretValueCommand({ SecretId: id }), `reading secret ${id}`,
                o.timeoutMs ?? REQUEST_CAP_MS)).SecretString,
            put: async (id, value, o = {}) => {
                const until = Date.now() + (o.timeoutMs ?? REQUEST_CAP_MS);
                const capLeft = () => Math.max(0, until - Date.now());
                try {
                    await smSend(budget, smClient,
                        new PutSecretValueCommand({ SecretId: id, SecretString: value }),
                        `writing secret ${id}`, capLeft());
                } catch (e) {
                    if (e.name === 'ResourceNotFoundException') {
                        await smSend(budget, smClient, new CreateSecretCommand({
                            Name: id, SecretString: value,
                            Description: 'HighByte MCP bearer token (managed by setup Lambda)'
                        }), `creating secret ${id}`, capLeft());
                    } else { throw e; }
                }
            },
        };
        await reconcileMcpCredential({
            fetchFn: fetch,
            secrets: secretsFacade,
            configEndpoint: apiEndpoint,
            mcpEndpoint: MCP_VERIFY_ENDPOINT,
            adminId: adminUser.id,
            apiVersion: HIGHBYTE_API_VERSION,
            authToken: highbyteToken,
            secretId: MCP_TOKEN_SECRET,
            deadline: mcpDeadline,
            // A key the reconcile owns (minted, unpersisted or rejected) may
            // still be deleted after mcpDeadline, up to the handler deadline.
            cleanupDeadline: budget.end,
        });
        console.log('MCP credential reconciled (secret present and token authenticates)');

        // Import project configuration if URL is provided
        let projectString = null;

        if (PROJECT_CONFIG_URL) {
            console.log(`Loading project configuration from ${PROJECT_CONFIG_URL}`);
            const projectResponse = await hbFetch(budget, 'fetching the project config',
                PROJECT_CONFIG_URL, {}, Infinity);
            if (!projectResponse.ok) {
                throw new Error(`Failed to fetch project config: ${projectResponse.status}`);
            }
            projectString = await projectResponse.text();
        } else if (process.env.CONFIG_FILE_PATH) {
            const fs = await import('fs');
            console.log(`Loading project configuration from bundled file: ${process.env.CONFIG_FILE_PATH}`);
            projectString = fs.readFileSync(process.env.CONFIG_FILE_PATH, 'utf-8');
        }

        if (projectString) {

            // OPC UA endpoints by connection name, Bedrock/SiteWise Region,
            // one Bedrock connection for the deployment Region, Bedrock model
            // prefixes, and (workshop) the SiteWise pipeline's triggers off:
            // project-transform.mjs. The source config file is never
            // modified; substitution happens at deploy time only.
            const awsRegion = process.env.AWS_REGION || 'us-east-1';
            console.log(`Deployment region: ${awsRegion}`);
            const projectConfig = transformProject(JSON.parse(projectString), {
                region: awsRegion,
                opcEndpoints: {
                    'opc_milwaukee':   OPC_UA_ENDPOINT,
                    'opc_detroit':     OPC_UA_ENDPOINT_2,
                    'opc_cedarrapids': OPC_UA_ENDPOINT_3,
                    'opc_indianapolis': OPC_UA_ENDPOINT_4,
                },
                sitewisePipeline: SITEWISE_PIPELINE,
            });

            projectString = JSON.stringify(projectConfig);
            console.log('OPC UA endpoints, Bedrock region, and model IDs substituted');

            // Wire the demo PostgreSQL connections (ERP + CMMS/Virtual Factory) ONLY
            // when their backing DB is deployed — this programmatic wiring is purely a
            // demo convenience (a user could add these in the HighByte UI). ERP fills
            // from AURORA_SECRET_ARN (--simulator); CMMS/Virtual Factory fills from ADT_POSTGRES_HOST
            // (--seed/--simulator) INDEPENDENTLY. Any jdbc.postgresql connection left
            // unwired (its DB absent, e.g. --highbyte without --seed/--simulator) is
            // then PRUNED along with the inputs that reference it, so HighByte boots
            // with a clean namespace (OPC UA -> SiteWise/MCP) and the user can add their
            // own ERP/CMMS connection in the HighByte UI. The OPC UA pipelines never
            // reference these connections, so pruning them is safe.
            {
                let projectConfig = JSON.parse(projectString);
                const proj = projectConfig.project || {};
                const conns = Array.isArray(proj.connections) ? proj.connections : [];

                let auroraCredentials = null;
                if (AURORA_SECRET_ARN) {
                    console.log('Retrieving Aurora (ERP) database credentials...');
                    const resp = await smSend(budget, smClient,
                        new GetSecretValueCommand({ SecretId: AURORA_SECRET_ARN }),
                        'reading the Aurora (ERP) database secret');
                    auroraCredentials = JSON.parse(resp.SecretString);
                }
                let adtPassword = '';
                if (process.env.ADT_POSTGRES_HOST && process.env.ADT_SECRET_ARN) {
                    const resp = await smSend(budget, smClient,
                        new GetSecretValueCommand({ SecretId: process.env.ADT_SECRET_ARN }),
                        'reading the ADT (CMMS) database secret');
                    adtPassword = (JSON.parse(resp.SecretString).password) || '';
                }

                for (const connection of conns) {
                    if (!connection.uri || !connection.uri.startsWith('jdbc.postgresql')) continue;
                    if (connection.name === 'PostgreSQL_VirtualFactory') {
                        if (process.env.ADT_POSTGRES_HOST) {
                            console.log(`Updating PostgreSQL connection: ${connection.name} (CMMS / Virtual Factory)`);
                            connection.uri = `jdbc.postgresql://${process.env.ADT_POSTGRES_HOST}:${process.env.ADT_POSTGRES_PORT || '5432'}`;
                            if (connection.settings) {
                                // "adt_demo" here is the fictitious demo company's database — demo content, not engine identity (see rename plan)
                                connection.settings.database = process.env.ADT_POSTGRES_DB || 'adt_demo';
                                connection.settings.username = process.env.ADT_POSTGRES_USER || 'postgres';
                                connection.settings.password = adtPassword;
                            }
                        }
                    } else if (auroraCredentials) {
                        // PostgreSQL_ERP / AuroraPostgreSQL (and any other jdbc.postgresql) -> ERP Aurora.
                        console.log(`Updating PostgreSQL connection: ${connection.name} (ERP Aurora)`);
                        connection.uri = `jdbc.postgresql://${auroraCredentials.host}:${auroraCredentials.port || 5432}`;
                        if (connection.settings) {
                            connection.settings.database = auroraCredentials.dbname || 'erp_simulation';
                            connection.settings.username = auroraCredentials.username;
                            connection.settings.password = auroraCredentials.password;
                        }
                    }
                }

                // Prune any jdbc.postgresql connection still pointing at the deploy-time
                // placeholder (not wired above because its seed/sim DB is absent), plus
                // every input that references it.
                const unwired = new Set(
                    conns.filter(c => (c.uri || '').startsWith('jdbc.postgresql') && c.uri.includes('<RDS_ENDPOINT>'))
                         .map(c => c.name)
                );
                if (unwired.size) {
                    proj.connections = conns.filter(c => !unwired.has(c.name));
                    if (Array.isArray(proj.inputs)) {
                        proj.inputs = proj.inputs.filter(i => !unwired.has(i.connection));
                    }
                    console.log(`Pruned unwired PostgreSQL connection(s) + their inputs (no backing DB deployed): ${[...unwired].join(', ')}`);
                }

                projectString = JSON.stringify(projectConfig);
                console.log('PostgreSQL connections resolved (wired where available, pruned otherwise)');
            }

            // Import project
            const importResponse = await hbFetch(budget, 'importing the HighByte project (import)',
                apiEndpoint + '/v1/project/import', {
                method: 'POST',
                body: projectString,
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': highbyteToken
                },
                // Capped to what is left of the invocation so a slow import
                // fails with a clear error instead of a Lambda timeout.
            }, Infinity);

            if (!importResponse.ok) {
                const responseBody = await importResponse.text();
                throw new Error(`Failed to import project: STATUS: ${importResponse.status}, BODY: ${responseBody}`);
            }

            console.log('Successfully imported project to HighByte');
        }

        const result = {
            statusCode: 200,
            body: JSON.stringify({ 
                message: 'HighByte configured successfully',
                opcUaEndpoint: OPC_UA_ENDPOINT,
                highbyteEndpoint: HIGHBYTE_ENDPOINT
            }),
        };

    } catch (error) {
        console.error('Setup failed:', error);
        throw error;
    }
};
// Updated config: OPC UA placeholders + 3 pipelines - Wed Apr 29 16:20:05 PDT 2026
