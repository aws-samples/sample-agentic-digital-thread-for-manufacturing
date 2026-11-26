// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Pure project-configuration transforms for the HighByte setup Lambda.
 *
 * Everything here is synchronous and side-effect free apart from the optional
 * log callback, so tests/test_highbyte_setup_transform.py runs it under node
 * against the bundled config/highbyte/intelligencehub-configuration.json.
 * The PostgreSQL wiring (it reads Secrets Manager) stays in index.mjs.
 */

/** The API-version path segment HighByte's own UI sends on /v1/user routes. */
export const HIGHBYTE_API_VERSION = '42';
/** The only pipeline in the bundled project that writes AWS IoT SiteWise. */
export const SITEWISE_PIPELINE = 'NameSpace_to_SiteWise';
export const SITEWISE_PIPELINE_VALUES = ['enabled', 'disabled'];

/**
 * PUT path that sets the administrator's password.
 *
 * HighByte 4.4.2's GET /v1/user/administrator returns no versionId, so the old
 * `/v1/user/${id}/${versionId}` sent the literal string "undefined". The hub
 * happened to accept it; its own UI sends the constant API version instead
 * (UserService.replace -> PUT /v1/user/{id}/42, the same segment the apiKey
 * route uses), so do the same whenever no versionId comes back.
 */
export function adminUserUpdatePath(adminUser) {
    const version = adminUser.versionId ?? HIGHBYTE_API_VERSION;
    return `/v1/user/${adminUser.id}/${version}`;
}

/** HIGHBYTE_SITEWISE_PIPELINE: 'enabled' (default, the demo) or 'disabled'. */
export function sitewisePipelineFromEnv(raw) {
    const value = String(raw ?? '').trim().toLowerCase();
    if (!value) return 'enabled';
    if (!SITEWISE_PIPELINE_VALUES.includes(value)) {
        throw new Error(`HIGHBYTE_SITEWISE_PIPELINE=${JSON.stringify(raw)} is not one of ${SITEWISE_PIPELINE_VALUES.join(', ')}`);
    }
    return value;
}

export function bedrockRegionName(region) {
    return region === 'us-east-1' ? 'N_Virginia' :
           region === 'us-west-2' ? 'Oregon' :
           region === 'eu-central-1' ? 'Frankfurt' :
           region === 'eu-west-1' ? 'Ireland' :
           region === 'ap-southeast-1' ? 'Singapore' :
           region.replace(/-/g, '_');
}

function clearPasswords(connection, kind, log) {
    const settings = connection.settings;
    if (!settings) return;
    // EC2_NATIVE auth uses the task role, so no stored password is needed.
    if (settings.authentication && settings.authentication.password) {
        log(`Clearing encrypted password for ${kind} connection: ${connection.name}`);
        settings.authentication.password = '';
    }
    if (settings.password) {
        log(`Clearing encrypted password for ${kind} connection: ${connection.name}`);
        settings.password = '';
    }
}

/**
 * OPC UA endpoints by connection name, and the deployment Region (passwords
 * cleared) on every Bedrock and IoT SiteWise connection.
 */
export function substituteConnections(projectConfig, { region, opcEndpoints }, log = console.log) {
    const connections = (projectConfig.project && projectConfig.project.connections) || [];
    for (const connection of connections) {
        const uri = connection.uri || '';
        const mapped = opcEndpoints[connection.name];
        if (mapped && uri.startsWith('opc.tcp://')) {
            log(`Updating OPC UA connection: ${connection.name} -> ${mapped}`);
            connection.uri = mapped;
        }
        for (const [scheme, kind] of [['aws.bedrock://', 'Bedrock'], ['aws.iotsitewise://', 'SiteWise']]) {
            if (!uri.startsWith(scheme)) continue;
            if (connection.settings && connection.settings.region) {
                log(`Updating ${kind} connection region: ${connection.name} ${connection.settings.region} -> ${region}`);
                connection.settings.region = region;
            }
            clearPasswords(connection, kind, log);
        }
    }
}

/**
 * Collapse every aws.bedrock:// connection into ONE named for the deployment
 * Region and point every input that used any of them at it.
 *
 * The project ships two (`Bedrock` and `Bedrock_us_east_2`), and the Region
 * substitution above already sends both to the deployment Region (the hub's
 * task role only allows inference profiles there). Renaming both to the same
 * name imported two connections with one name, so one overwrote the other,
 * and inputs that referenced `Bedrock` (no underscore) were left pointing at
 * a connection that no longer existed. Keep the first, drop the rest.
 */
export function collapseBedrockConnections(projectConfig, region, log = console.log) {
    const project = projectConfig.project || {};
    const bedrockName = `Bedrock_${bedrockRegionName(region)}`;
    const replaced = new Set();
    let kept = false;
    project.connections = (project.connections || []).filter((connection) => {
        if (!(connection.uri || '').startsWith('aws.bedrock://')) return true;
        replaced.add(connection.name);
        if (kept) {
            log(`Dropping duplicate Bedrock connection: ${connection.name} (inputs now use ${bedrockName})`);
            return false;
        }
        kept = true;
        if (connection.name !== bedrockName) {
            log(`Renaming Bedrock connection: ${connection.name} -> ${bedrockName}`);
            connection.name = bedrockName;
        }
        return true;
    });
    for (const input of project.inputs || []) {
        if (replaced.has(input.connection) && input.connection !== bedrockName) {
            log(`Updating input connection reference: ${input.name} ${input.connection} -> ${bedrockName}`);
            input.connection = bedrockName;
        }
    }
    return bedrockName;
}

/** Bedrock model ids in pipelines and inputs take the deployment's prefix (eu./us./ap.). */
export function substituteModels(projectConfig, region, log = console.log) {
    const prefix = region.startsWith('eu') ? 'eu' : region.startsWith('ap') ? 'ap' : 'us';
    const modelPrefixRegex = /^(eu|us|ap|ca)\.(anthropic\.|amazon\.)/;
    const visit = (obj) => {
        if (!obj || typeof obj !== 'object') return;
        if (Array.isArray(obj)) {
            obj.forEach(visit);
            return;
        }
        for (const key of Object.keys(obj)) {
            if (key === 'modelType' && typeof obj[key] === 'string' && modelPrefixRegex.test(obj[key])) {
                const original = obj[key];
                obj[key] = obj[key].replace(modelPrefixRegex, `${prefix}.$2`);
                log(`Updating Bedrock model: ${original} -> ${obj[key]}`);
            } else {
                visit(obj[key]);
            }
        }
    };
    const project = projectConfig.project || {};
    visit(project.pipelines);
    visit(project.inputs);
}

/**
 * Import NameSpace_to_SiteWise with every trigger disabled
 * (pipelines[].triggers[].config.enabled=false: the field the hub's own
 * pipeline-level Disable writes, proven on 4.4.2). The workshop sets this:
 * SFC is its only SiteWise writer (ADR-0082). Returns the trigger names.
 */
export function disableSiteWisePipeline(projectConfig, log = console.log) {
    const pipelines = (projectConfig.project && projectConfig.project.pipelines) || [];
    const pipeline = pipelines.find((p) => p.name === SITEWISE_PIPELINE);
    if (!pipeline) {
        log(`WARNING: HIGHBYTE_SITEWISE_PIPELINE=disabled but the project has no ${SITEWISE_PIPELINE} pipeline`);
        return [];
    }
    const switched = [];
    for (const trigger of pipeline.triggers || []) {
        trigger.config = trigger.config || {};
        trigger.config.enabled = false;
        switched.push(trigger.name);
    }
    log(`${SITEWISE_PIPELINE}: ${switched.length} trigger(s) imported disabled (${switched.join(', ')})`);
    return switched;
}

/** Every deploy-time substitution except the PostgreSQL wiring, in order. */
export function transformProject(projectConfig, { region, opcEndpoints, sitewisePipeline = 'enabled' }, log = console.log) {
    substituteConnections(projectConfig, { region, opcEndpoints }, log);
    collapseBedrockConnections(projectConfig, region, log);
    substituteModels(projectConfig, region, log);
    if (sitewisePipeline === 'disabled') {
        disableSiteWisePipeline(projectConfig, log);
    }
    return projectConfig;
}
