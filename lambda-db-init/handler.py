# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Database initialization Lambda handler.
Creates the ERP simulation database schema and seeds reference data.

CONSOLIDATED VERSION — All seed data in one path, no additions/ directory needed.

Tables created: 36
- Core ERP: customers, suppliers, products, inventory_items, inventory_locations,
  inventory_balances, inventory_transactions, bill_of_materials
- Orders: customer_orders, order_line_items
- Procurement: purchase_orders, po_line_items, po_receipts
- Production: production_runs, production_materials, sites, product_site_routing,
  production_lines, site_product_config
- Shipping/Finance: shipments, shipment_items, invoices, invoice_line_items, payments
- MES: mes_work_orders, mes_production_events, mes_quality_checks,
  mes_material_consumption, mes_downtime_events, mes_equipment (created by
  seed_mes_equipment, which the Create seed and the seed-only Update run)
- Lot Traceability: material_lots, lot_warehouse_movements, lot_production_usage,
  lot_mes_tracking, item_suppliers
- Config: simulation_config, simulation_events
- Transfers: inventory_transfers

Seed data: customers (30), suppliers (23), products (28), inventory_items (56),
  inventory_locations (14), bill_of_materials (93), simulation_config (38),
  sites (4), product_site_routing (13), production_lines (9),
  material_lots (ALL sites), inventory_balances (ALL sites)
"""
import json
import math
import os
import logging
import random
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _pgdriver():
    """The PostgreSQL client module, imported when a connection or a statement
    needs it: the image's copy beside this file, else the ERP simulator's
    (erp_simulation/seed_data.py and scripts/seed_local_db.py load this file
    by path, where only the package copy is importable)."""
    try:
        import pgdriver
    except ImportError:
        from erp_simulation import pgdriver
    return pgdriver


def get_db_credentials():
    """Retrieve database credentials from Secrets Manager."""
    secret_arn = os.environ['DB_SECRET_ARN']
    region = os.environ.get('AWS_REGION', 'us-east-1')

    client = boto3.client('secretsmanager', region_name=region)
    response = client.get_secret_value(SecretId=secret_arn)
    return json.loads(response['SecretString'])


def get_connection(credentials):
    """Create database connection."""
    return _pgdriver().connect(
        host=credentials['host'],
        port=credentials.get('port', 5432),
        database=credentials.get('dbname', os.environ.get('DB_NAME', 'erp_simulation')),
        user=credentials['username'],
        password=credentials['password'],
        connect_timeout=30
    )


def handler(event, context):
    """Lambda handler for database initialization."""
    # Log only the control fields, never the whole event: its
    # ResourceProperties carry the PasswordTrigger and other properties.
    logger.info(
        "Event: RequestType=%s action=%s SeedVersion=%s",
        event.get("RequestType"),
        event.get("action"),
        (event.get("ResourceProperties") or {}).get("SeedVersion"),
    )

    # Direct-invoke maintenance action (not a CloudFormation lifecycle event)
    if event.get('action') == 'fix_timezone':
        return fix_timezone()

    request_type = event.get('RequestType', 'Create')

    if request_type == 'Delete':
        return {'Status': 'SUCCESS', 'Data': {'Message': 'Delete - no action needed'}}

    # An existing account's in-place update that only ships new seed rows.
    if is_seed_only_update(event):
        if not seed_only_update_is_for_this_code(event):
            return _skip_seed_catalogue_upsert(event)
        return _run_seed_catalogue_upsert()

    # Create, or an Update that drops and reseeds (a Timestamp bump, a
    # database rename, a new cluster, the dashboard's Reset All).
    if request_type == 'Update' and not full_reset_update_is_for_this_code(event):
        return _skip_full_reset(event)
    # CloudFormation re-runs what depends on db-init after its own resets
    # (an optional site's seed, the read-only logins); a direct reset runs it
    # here, its steps read and checked before anything is dropped.
    steps = None if is_cloudformation_request(event) else direct_reset_steps()
    result = _run_full_reset()
    if steps:
        run_direct_reset_steps(steps, context)
    return result


class DatabaseInitError(RuntimeError):
    """A Create or full-reset Update that did not leave a verified ERP
    database. Raised to the Provider, which reports the custom resource
    FAILED with this message (see _run_full_reset)."""


# The most a failure message may take of the custom resource response.
# CloudFormation refuses a response body over 4096 bytes. The Provider
# (aws-cdk-lib 2.261.0, custom-resources/lib/provider-framework/runtime)
# sends FAILED as the JSON {Status, Reason, StackId, RequestId,
# PhysicalResourceId, LogicalResourceId}, its Reason being
# "Error: <this message>\n\nLogs: /aws/lambda/<function>\n\n" and then the
# framework's own Node.js stack frames: the Python runtime's error payload has
# a "stackTrace", not the "trace" framework.js copies, so none of this
# function's frames are added, and V8 keeps at most 10 (Error.stackTraceLimit).
# The rest of that body stays well under 4096 - 1024 bytes
# (erp_simulation/tests/test_db_init_reset_failures.py builds it).
_MESSAGE_BYTE_LIMIT = 1024


def _json_bytes(ch):
    """Bytes one character takes in the Provider's response body as
    JSON.stringify writes it: a quote or a backslash 2, a control character
    at most 6 (\\u00XX), a lone surrogate 6 (\\udXXX), anything else its
    UTF-8 encoding (1 to 4)."""
    code = ord(ch)
    if code < 0x20 or 0xD800 <= code <= 0xDFFF:
        return 6
    if ch in '"\\':
        return 2
    return len(ch.encode('utf-8'))


def _bounded(message, limit=_MESSAGE_BYTE_LIMIT):
    """``message`` cut to at most ``limit`` bytes of the response body
    (_json_bytes), ending in '...' when cut. Counted in bytes, not
    characters: a thousand four-byte characters alone fill 4000 bytes."""
    if sum(map(_json_bytes, message)) <= limit:
        return message
    kept, used = [], len('...')
    for ch in message:
        used += _json_bytes(ch)
        if used > limit:
            break
        kept.append(ch)
    return ''.join(kept) + '...'


def _run_full_reset():
    """Drop every ERP table, create the schema, seed it and verify it.

    A failure is logged and RAISED as DatabaseInitError naming the step (and,
    for the verification, the missing tables or the sites without APPROVED
    lots), not returned: this Lambda sits behind
    aws_cdk.custom_resources.Provider (stacks/erp_database_stack.py), which
    reports the custom resource FAILED only when its handler raises and
    treats any returned value as success. Raising makes a Create fail the
    stack instead of reporting CREATE_COMPLETE over an empty or half-built
    database. The connection is closed first; nothing after the failing step
    runs (each step commits its own work, so the steps before it stay)."""
    step = 'connect'
    try:
        conn = get_connection(get_db_credentials())
        try:
            # Terminate other connections and drop all tables for a clean slate
            step = 'drop_all_tables'
            drop_all_tables(conn)

            # Create all tables (consolidated — includes lot traceability, production_lines, etc.)
            step = 'create_schema'
            create_schema(conn)

            # Seed reference data (consolidated — one path, correct order)
            step = 'seed_data'
            seed_data(conn)

            # Verify (verify_schema answers yes or no; on no, schema_problems
            # says what is wrong, for the failure message)
            step = 'verify_schema'
            problems = [] if verify_schema(conn) else (
                schema_problems(conn) or ['verify_schema reported a problem'])
        finally:
            conn.close()
    except Exception as e:
        # The log keeps the whole message; the raised one is cut to fit the
        # custom resource response (_bounded).
        message = f"ERP database initialization failed during {step}: {type(e).__name__}: {e}"
        logger.error(message)
        raise DatabaseInitError(_bounded(message)) from e
    if problems:
        message = f"ERP database initialization failed during verify_schema: {'; '.join(problems)}"
        logger.error(message)
        raise DatabaseInitError(_bounded(message))
    return {
        'Status': 'SUCCESS',
        'Data': {'Message': 'Database initialized and seeded successfully'}
    }


# The db-init CR's properties whose change ALONE is a catalogue-only Update
# (stacks/erp_database_stack.py::db_init_seed_version). Any other change — a
# Timestamp bump, a database rename, a new cluster — still drops and reseeds.
SEED_ONLY_UPDATE_PROPERTIES = frozenset({'SeedVersion'})


def is_seed_only_update(event):
    """True for a CloudFormation Update that changes SeedVersion and nothing
    else (ServiceToken ignored; an added SeedVersion counts as a change)."""
    if event.get('RequestType') != 'Update':
        return False
    new = event.get('ResourceProperties') or {}
    old = event.get('OldResourceProperties') or {}
    changed = {k for k in set(new) | set(old) if k != 'ServiceToken' and new.get(k) != old.get(k)}
    return bool(changed) and changed <= SEED_ONLY_UPDATE_PROPERTIES


# The function's environment variable holding the SeedVersion of the code it
# runs. The stack sets it to db_init_seed_version(), the value it also gives
# the custom resource as SeedVersion (stacks/erp_database_stack.py), so the
# two cannot drift. It lives in the function's configuration, not in this
# file, so the hash stays a hash of this file alone.
SEED_VERSION_ENV = 'DB_INIT_SEED_VERSION'


def seed_only_update_is_for_this_code(event):
    """True when a seed-only Update asks for the SeedVersion of the running
    code, or when that version is unknown (unset or empty: a function
    deployed before the stack set it, which keeps the old behaviour).

    False for the Update CloudFormation sends while rolling back a failed
    seed-only Update: it carries the PREVIOUS SeedVersion. If the function
    still runs the new code then, running its upsert again would fail the same
    way and leave the stack in UPDATE_ROLLBACK_FAILED, so the caller skips it.
    If the function was rolled back first, the old code receives its own
    version and runs its own additive upsert."""
    running = os.environ.get(SEED_VERSION_ENV)
    if not running:
        return True
    return (event.get('ResourceProperties') or {}).get('SeedVersion') == running


def _skip_seed_catalogue_upsert(event):
    """A seed-only Update for another code version: logged, nothing opened or
    changed, reported as success (the Provider sends SUCCESS for a return)."""
    requested = (event.get('ResourceProperties') or {}).get('SeedVersion')
    running = os.environ.get(SEED_VERSION_ENV)
    logger.warning(
        f"Seed catalogue upsert skipped: the update asks for SeedVersion {requested!r}, "
        f"this code is {running!r} (an update rollback reaching the new code); nothing changed"
    )
    return {'Status': 'SUCCESS', 'Data': {
        'Message': f'Seed catalogue upsert skipped: SeedVersion {requested} is not this code ({running})'}}


def seed_catalogue_upsert(conn):
    """ADDITIVE, idempotent seed catalogue upsert for an EXISTING ERP database.

    Runs the machining-site catalogue (seed_machining_site: products, finished
    goods, BOM, balances, lines, site config, routing, customers, lots — every
    INSERT ON CONFLICT DO NOTHING, ADD COLUMN IF NOT EXISTS, the PLM
    cross-reference UPDATEs), the MES equipment registry (seed_mes_equipment:
    CREATE TABLE IF NOT EXISTS, rows ON CONFLICT DO NOTHING), config rows added
    since Create (add_new_config_rows), moves untouched
    config defaults that changed
    since Create (migrate_moved_config_defaults), gives the downtime length
    two decimal places (migrate_downtime_duration_type: the views that read it
    are recreated as they were, in the same transaction), and then the view
    builders (CREATE OR REPLACE).
    Never drops a table, truncates or deletes: rows the simulator and the live
    story wrote since Create stay as they are (a downtime row's generated
    length is recomputed from its own, unchanged timestamps). Two commits, as
    seed_data."""
    with conn.cursor() as cursor:
        seed_machining_site(cursor)
        seed_mes_equipment(cursor)
        _seed_anchor_suppliers(cursor)
        add_new_config_rows(cursor)
        migrate_moved_config_defaults(cursor)
        migrate_downtime_duration_type(cursor)
    conn.commit()
    with conn.cursor() as cursor:
        cursor.execute(get_views_sql())
        cursor.execute(get_machining_views_sql())
    conn.commit()


def _run_seed_catalogue_upsert():
    """The seed-only Update. A failure is logged and RAISED, not returned:
    this Lambda sits behind aws_cdk.custom_resources.Provider
    (stacks/erp_database_stack.py), which reports the custom resource FAILED
    only when its handler raises and treats any returned value as success
    (its ``Status`` key is never read). Raising makes CloudFormation fail and
    roll back the update instead of carrying on with the old cap and column
    type. The connection is closed first; nothing after the failure commits."""
    try:
        conn = get_connection(get_db_credentials())
        try:
            seed_catalogue_upsert(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Seed catalogue upsert failed: {e}")
        raise
    return {'Status': 'SUCCESS', 'Data': {'Message': 'Seed catalogue upserted (additive, no reset)'}}


# The function's environment variable holding the Timestamp of the code it
# runs. The stack sets it to DB_INIT_TIMESTAMP, the value it also gives the
# custom resource as Timestamp (stacks/erp_database_stack.py). Set, it marks
# a function whose stack pins every property a full reset deploys
# (FULL_RESET_PINS); unset or empty (a function deployed before the stack set
# it), every full-reset Update runs, as before.
INIT_TIMESTAMP_ENV = 'DB_INIT_TIMESTAMP'

# Every custom resource property but ServiceToken, each with the environment
# variable holding the value this code's stack deploys for it (the stack sets
# both from the same expression; tests/test_seed_cr_versions.py fails when a
# property of the custom resource is missing here). A change to any of them
# except SeedVersion alone selects the full reset (is_seed_only_update), so a
# rollback that changes only one of them is still recognised.
FULL_RESET_PINS = (
    ('Timestamp', INIT_TIMESTAMP_ENV),
    ('DatabaseName', 'DB_NAME'),
    ('SeedVersion', SEED_VERSION_ENV),
    ('ClusterEndpoint', 'DB_CLUSTER_ENDPOINT'),
    ('SecretArn', 'DB_SECRET_ARN'),
)


def _full_reset_mismatches(event):
    """The pinned properties a full-reset Update asks for that this code does
    not deploy: [(property, requested, this code's)]."""
    requested = event.get('ResourceProperties') or {}
    return [(prop, requested.get(prop), os.environ.get(env))
            for prop, env in FULL_RESET_PINS
            if os.environ.get(env) and requested.get(prop) != os.environ.get(env)]


def full_reset_update_is_for_this_code(event):
    """True when a full-reset Update asks for the properties this code's
    stack deploys (FULL_RESET_PINS), when they are unknown (DB_INIT_TIMESTAMP
    unset or empty, which keeps the old behaviour), or when the Update is not
    a CloudFormation request (no StackId: the Streamlit dashboard's Reset All
    invokes the function directly with ``{'RequestType': 'Update'}`` and
    expects the reset).

    False for the Update CloudFormation sends while rolling back a failed
    full-reset Update: it carries the PREVIOUS properties. If the function
    still runs the new code then, its reset would drop the database again and
    could fail the same way, leaving the stack in UPDATE_ROLLBACK_FAILED, so
    the caller skips it. If the function was rolled back first, the old code
    receives its own properties and runs its own reset."""
    if not os.environ.get(INIT_TIMESTAMP_ENV) or not is_cloudformation_request(event):
        return True
    return not _full_reset_mismatches(event)


def _skip_full_reset(event):
    """A full-reset Update for another code version: logged, nothing opened
    or changed, reported as success (the Provider sends SUCCESS for a
    return). The database stays as the failed forward Update left it."""
    mismatches = '; '.join(f"{prop} {asked!r} (this code: {running!r})"
                           for prop, asked, running in _full_reset_mismatches(event))
    logger.warning(
        f"Database reset skipped: the update asks for {mismatches} "
        f"(an update rollback reaching the new code); nothing changed"
    )
    return {'Status': 'SUCCESS', 'Data': {
        'Message': f'Database reset skipped: the update is not for this code ({mismatches})'}}


def is_cloudformation_request(event):
    """True for an event CloudFormation sent through the Provider (it carries
    a StackId); False for a direct invoke such as the dashboard's Reset All."""
    return bool(event.get('StackId'))


# What a full reset outside CloudFormation owes afterwards
# (stacks/_direct_reset.py). After its own runs CloudFormation re-runs the
# custom resources that depend on db-init: an optional site re-seeds the rows
# the reset dropped, and the read-only logins (stacks/_db_readers.py, and a
# dependent stack's) are re-granted on the schemas and tables it recreated.
# A direct reset (the ERP dashboard's Reset All, or a hand invoke) runs the
# same steps here, in order: the stack's own (DIRECT_RESET_STEPS_ENV, a JSON
# list), then those dependent stacks registered in the SSM parameter(s)
# DIRECT_RESET_REGISTRY_ENV names (one bare name, or a JSON list of names when
# more than one dependent stack registers), read in that order. Each step is
# one synchronous invoke of the function that does that work; this handler
# seeds no site and grants nothing itself. Every step is read and checked
# BEFORE the reset drops
# anything (direct_reset_steps): a list that cannot be read, or a registry
# that is named but missing, refuses the reset. Unset (a function deployed
# before the stack set them), a direct reset runs as before and logs that
# nothing will be restored.
DIRECT_RESET_STEPS_ENV = 'DB_DIRECT_RESET_STEPS'
DIRECT_RESET_REGISTRY_ENV = 'DB_DIRECT_RESET_REGISTRY'
# How a step's invoke waits. The steps run after the reset has committed and
# under this invoke's own deadline (its function timeout), so no wait may
# outlast it (_step_wait). An invoke connects within STEP_CONNECT_SECONDS and
# waits for the answer up to the step function's own timeout (the step's
# TimeoutSeconds) plus STEP_MARGIN_SECONDS, so a step that times out comes
# back as its FunctionError, but never longer than what is left of this
# invoke less DEADLINE_RESERVE_SECONDS (kept to raise and answer) and the
# connect time. A step that would be left less than STEP_MIN_SECONDS (the ERP
# login setup took 4.5 s live) is not started. One attempt, so one reset
# never starts a step twice. A step's TimeoutSeconds is at most Lambda's own
# limit, MAX_STEP_TIMEOUT_SECONDS.
STEP_CONNECT_SECONDS = 5
STEP_MARGIN_SECONDS = 10
DEADLINE_RESERVE_SECONDS = 10
STEP_MIN_SECONDS = 30
MAX_STEP_TIMEOUT_SECONDS = 900


class ResetRefusedError(DatabaseInitError):
    """A direct full reset refused before anything was dropped: the steps it
    owes afterwards could not be read. Raised, so the direct caller gets a
    FunctionError (the dashboard reports Reset All as failed, with this
    message)."""


class ResetFollowUpError(DatabaseInitError):
    """A direct full reset that reseeded the database but did not finish the
    steps it owes afterwards. Raised, so the direct caller gets a
    FunctionError (the dashboard reports Reset All as failed, with this
    message)."""


def direct_reset_steps():
    """Every step a direct full reset owes afterwards, read and checked
    before the reset drops anything: the stack's own, then those a dependent
    stack registered (registered_reset_steps). None when the stack set no
    steps. Raises ResetRefusedError when a list cannot be read or is
    malformed."""
    raw = os.environ.get(DIRECT_RESET_STEPS_ENV)
    if not raw:
        logger.warning(f"{DIRECT_RESET_STEPS_ENV} is not set: what depends on this database "
                       f"is not restored after this reset")
        return None
    return _reset_steps(raw, DIRECT_RESET_STEPS_ENV) + registered_reset_steps()


def registered_reset_steps():
    """The steps dependent stacks registered in the SSM parameter(s) that
    DIRECT_RESET_REGISTRY_ENV names; none when it names none (no dependent
    stack is part of the deployment). The env is one bare parameter name, or a
    JSON list of names when more than one dependent stack registers, read in
    that order. Each named parameter is expected: missing (that stack is not
    deployed, or not yet) or unreadable, it refuses the reset."""
    raw = os.environ.get(DIRECT_RESET_REGISTRY_ENV)
    if not raw:
        return []
    steps = []
    for name in _registry_parameter_names(raw):
        steps.extend(_reset_steps(_read_registry_parameter(name), name))
    return steps


def _registry_parameter_names(raw):
    """The registry parameter name(s) in DIRECT_RESET_REGISTRY_ENV: a JSON list
    of names, or the single bare name of the only-one-registrant form (kept so
    an account deployed before the second registrant reads its env
    unchanged). Raises ResetRefusedError on a malformed list."""
    text = raw.strip()
    if not text.startswith('['):
        return [raw]
    try:
        names = json.loads(text)
        if not (isinstance(names, list) and names
                and all(isinstance(name, str) and name for name in names)):
            raise ValueError('not a non-empty list of parameter names')
    except (ValueError, TypeError) as e:
        raise _reset_refused(f"the registry list {DIRECT_RESET_REGISTRY_ENV}",
                             f"{type(e).__name__}: {e}") from e
    return names


def _read_registry_parameter(name):
    """The value of registry parameter ``name``; ResetRefusedError when it
    cannot be read (missing: the stack that registers there is not deployed
    yet)."""
    try:
        return _ssm_client().get_parameter(Name=name)['Parameter']['Value']
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        if _aws_error_code(e) == 'ParameterNotFound':
            detail = (f"it does not exist, though {DIRECT_RESET_REGISTRY_ENV} names it: the stack "
                      f"that registers its steps there is not deployed yet. Deploy it, then reset")
        raise _reset_refused(f"the steps in {name}", detail) from e


def _reset_steps(raw, source):
    """The steps in ``raw`` (a JSON list read from ``source``), each with a
    Name, a FunctionName, its function's TimeoutSeconds and one event: a
    Payload, or a custom resource's PhysicalResourceId and
    ResourceProperties. Raises ResetRefusedError on anything else."""
    try:
        steps = json.loads(raw)
        if not isinstance(steps, list):
            raise ValueError('not a list of steps')
        for step in steps:
            named = isinstance(step.get('Name'), str) and isinstance(step.get('FunctionName'), str)
            timeout = step.get('TimeoutSeconds')
            timed = (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                     and 0 < timeout <= MAX_STEP_TIMEOUT_SECONDS)
            resource = 'ResourceProperties' in step and 'PhysicalResourceId' in step
            if not (named and timed) or ('Payload' in step) == resource:
                raise ValueError(f'not a step: {json.dumps(step)[:200]}')
    except (ValueError, TypeError, AttributeError) as e:
        raise _reset_refused(f"the steps in {source}", f"{type(e).__name__}: {e}") from e
    return steps


def run_direct_reset_steps(steps, context):
    """Run ``steps`` (direct_reset_steps) in order, each invoke waiting at
    most what is left of this invoke's own time (_step_wait), and return
    their names. Raises ResetFollowUpError naming every step not restored
    when one fails, does not answer in time, or has too little time left to
    start; the steps after it do not run."""
    for index, step in enumerate(steps):
        not_restored = [later['Name'] for later in steps[index:]]
        wait, left = _step_wait(step, context)
        if wait is None:
            raise _reset_step_failed(not_restored, step['FunctionName'], (
                f"not started: {left:.0f} s of this invoke were left, and a step is started "
                f"only with {STEP_MIN_SECONDS} s to wait after the "
                f"{DEADLINE_RESERVE_SECONDS + STEP_CONNECT_SECONDS} s kept back"))
        _run_reset_step(_lambda_client(wait), step, not_restored)
    return [step['Name'] for step in steps]


def _step_wait(step, context):
    """(how long the invoke of ``step`` waits for its answer, the seconds
    left of this invoke): the step function's own timeout plus
    STEP_MARGIN_SECONDS, cut to what is left less DEADLINE_RESERVE_SECONDS
    and the connect time; None for the wait when that is under
    STEP_MIN_SECONDS. Without a Lambda context (run in process) there is no
    deadline."""
    full = step['TimeoutSeconds'] + STEP_MARGIN_SECONDS
    remaining = getattr(context, 'get_remaining_time_in_millis', None)
    if remaining is None:
        return full, math.inf
    left = remaining() / 1000
    available = left - DEADLINE_RESERVE_SECONDS - STEP_CONNECT_SECONDS
    return (min(full, available) if available >= STEP_MIN_SECONDS else None), left


def reset_step_event(step):
    """What one step sends: its Payload, or the Update event CloudFormation
    sends a custom resource whose properties did not change (the deployed
    properties as both new and old, so its handler re-applies them)."""
    if 'Payload' in step:
        return step['Payload']
    return {'RequestType': 'Update', 'PhysicalResourceId': step['PhysicalResourceId'],
            'ResourceProperties': step['ResourceProperties'],
            'OldResourceProperties': step['ResourceProperties']}


def _run_reset_step(client, step, not_restored):
    """One synchronous invoke; raises ResetFollowUpError naming
    ``not_restored`` (this step and the ones after it)."""
    name, function = step['Name'], step['FunctionName']
    try:
        response = client.invoke(FunctionName=function, InvocationType='RequestResponse',
                                 Payload=json.dumps(reset_step_event(step)).encode('utf-8'))
        payload = response['Payload'].read().decode('utf-8', 'replace')
    except Exception as e:
        raise _reset_step_failed(not_restored, function, f"{type(e).__name__}: {e}") from e
    if response.get('FunctionError'):
        raise _reset_step_failed(
            not_restored, function, f"{response['FunctionError']}: {_function_error_detail(payload)}")
    logger.info(f"Restored after the reset: {name} ({function})")


def _lambda_client(read_timeout):
    """A Lambda client whose invoke waits ``read_timeout`` seconds for the
    answer (_step_wait) and is sent once."""
    from botocore.config import Config

    return boto3.client('lambda', region_name=os.environ.get('AWS_REGION'), config=Config(
        connect_timeout=STEP_CONNECT_SECONDS, read_timeout=read_timeout,
        retries={'total_max_attempts': 1, 'mode': 'standard'}))


def _ssm_client():
    from botocore.config import Config

    return boto3.client('ssm', region_name=os.environ.get('AWS_REGION'), config=Config(
        connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 3, 'mode': 'standard'}))


def _aws_error_code(error):
    """A botocore ClientError's code (``response['Error']['Code']``), else None."""
    response = getattr(error, 'response', None)
    return (response.get('Error') or {}).get('Code') if isinstance(response, dict) else None


def _function_error_detail(payload):
    """``errorType: errorMessage`` of a Python runtime error payload; any
    other payload as it came."""
    try:
        body = json.loads(payload)
        return f"{body['errorType']}: {body['errorMessage']}"
    except (ValueError, TypeError, KeyError):
        return payload


def _reset_step_failed(not_restored, where, detail):
    """The ResetFollowUpError naming every step in ``not_restored`` (logged
    whole, raised cut to _MESSAGE_BYTE_LIMIT; the names and the remedy come
    first, so a cut takes only the detail)."""
    them = 'it' if len(not_restored) == 1 else 'them'
    message = (f"ERP database reset and reseeded, but not restored: {'; '.join(not_restored)}. "
               f"The reset has not finished: a second Reset All restores {them} once the cause "
               f"is cleared. {where}: {detail}")
    logger.error(message)
    return ResetFollowUpError(_bounded(message))


def _reset_refused(where, detail):
    """The ResetRefusedError for ``detail`` (logged whole, raised cut to
    _MESSAGE_BYTE_LIMIT)."""
    message = (f"ERP database reset refused, nothing was dropped: the steps it owes afterwards "
               f"could not be read ({where}): {detail}")
    logger.error(message)
    return ResetRefusedError(_bounded(message))


def _ddl(template, *names):
    """*template* with each ``{}`` filled by the next name, quoted by the driver
    as an identifier. DDL cannot bind identifiers, so this is how a name gets
    into a statement; callers pass module constants or names they validated.
    Returned as text, so a connection from any copy of pgdriver runs it
    (erp_simulation/seed_data.py passes its own package's connections)."""
    sql = _pgdriver().sql

    return sql.SQL(template).format(*(sql.Identifier(name) for name in names)).as_string(None)


_SET_UTC_SQL = "ALTER DATABASE {} SET timezone = 'UTC'"


def _alter_databases_to_utc(cursor, erp_db_name, app_db_name):
    """ALTER DATABASE ... SET timezone='UTC'; returns the list of DBs fixed.

    Applies to the ERP simulation database always, and to the app database
    only if it exists on the same host.
    """
    databases_fixed = []
    logger.info(f"Setting timezone for {erp_db_name} database...")
    cursor.execute(_ddl(_SET_UTC_SQL, erp_db_name))
    databases_fixed.append(erp_db_name)
    logger.info(f"{erp_db_name} timezone set to UTC")

    # Set timezone for the app database only if it lives on this host
    cursor.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (app_db_name,)
    )
    if cursor.fetchone():
        logger.info(f"Setting timezone for {app_db_name} database...")
        cursor.execute(_ddl(_SET_UTC_SQL, app_db_name))
        databases_fixed.append(app_db_name)
        logger.info(f"{app_db_name} timezone set to UTC")
    else:
        logger.info(f"{app_db_name} database not found, skipping")
    return databases_fixed


def _verify_utc_default(credentials, erp_db_name):
    """Verify a NEW connection picks up the database-level timezone default."""
    verify_conn = _pgdriver().connect(
        host=credentials['host'],
        port=credentials.get('port', 5432),
        database=erp_db_name,
        user=credentials['username'],
        password=credentials['password'],
        connect_timeout=30
    )
    try:
        with verify_conn.cursor() as cursor:
            cursor.execute("SHOW timezone")
            tz = cursor.fetchone()[0]
            logger.info(f"Verified {erp_db_name} timezone: {tz}")
            return tz
    finally:
        verify_conn.close()


def fix_timezone():
    """Set the cluster's databases to UTC via ALTER DATABASE ... SET timezone.

    Aurora PostgreSQL defaults the session timezone to the cluster parameter
    group value; queries that mix NOW() with UTC-written rows drift unless the
    database default is pinned to UTC. Applies to the ERP simulation database
    always, and to the app database only if it exists on the same host.
    """
    erp_db_name = os.environ.get('DB_NAME', 'erp_simulation')
    app_db_name = os.environ.get('APP_DB_NAME', 'adt_demo')

    try:
        credentials = get_db_credentials()
        logger.info(f"Connected to database host: {credentials['host']}")

        # Connect to the maintenance database to run ALTER DATABASE commands
        conn = _pgdriver().connect(
            host=credentials['host'],
            port=credentials.get('port', 5432),
            database='postgres',
            user=credentials['username'],
            password=credentials['password'],
            connect_timeout=30
        )
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                databases_fixed = _alter_databases_to_utc(
                    cursor, erp_db_name, app_db_name)
        finally:
            conn.close()

        tz = _verify_utc_default(credentials, erp_db_name)
        return {
            'Status': 'SUCCESS',
            'Data': {
                'Message': 'Timezone set to UTC successfully',
                'Databases': databases_fixed,
                'VerifiedTimezone': tz,
            }
        }
    except Exception as e:
        logger.error(f"Timezone fix failed: {e}")
        return {'Status': 'FAILED', 'Data': {'Message': str(e)}}


#: The ERP dashboard's Reset All lock session: its application_name
#: (erp_simulation/external_mes.py RESET_ALL_LOCK_APPLICATION) and the
#: advisory lock key it holds (external_mes.SENDER_LOCK_KEY), both pinned
#: equal by test. A full reset ends every other session but that one: it holds
#: only an external sender's advisory lock, and must keep it until Reset All
#: is done so no sender cycle runs mid-reset. The session is kept only when it
#: has the name AND holds the lock: the name alone is client-chosen, and the
#: lock is exclusive, so only the session Reset All took it on matches.
RESET_ALL_LOCK_APPLICATION = 'erp-dashboard-reset-all'
RESET_ALL_LOCK_KEY = 6076847149970312772

#: pg_locks shows a bigint advisory key as its high half (classid), its low
#: half (objid) and objsubid 1.
_RESET_ALL_LOCK_PARAMS = {'app': RESET_ALL_LOCK_APPLICATION,
                          'classid': (RESET_ALL_LOCK_KEY >> 32) & 0xFFFFFFFF,
                          'objid': RESET_ALL_LOCK_KEY & 0xFFFFFFFF}


def drop_all_tables(conn):
    """Terminate other connections (except Reset All's lock session) and drop
    all tables/schemas for a clean reset."""
    db_name = os.environ.get('DB_NAME', 'erp_simulation')
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT pg_terminate_backend(a.pid)
            FROM pg_stat_activity a
            WHERE a.datname = %(db)s AND a.pid <> pg_backend_pid()
              AND NOT (COALESCE(a.application_name, '') = %(app)s AND EXISTS (
                  SELECT 1 FROM pg_locks l
                   WHERE l.pid = a.pid AND l.locktype = 'advisory' AND l.granted
                     AND l.classid = %(classid)s::oid AND l.objid = %(objid)s::oid AND l.objsubid = 1))
        """, {'db': db_name, **_RESET_ALL_LOCK_PARAMS})
        terminated = cursor.rowcount
        conn.commit()
        logger.info(f"Terminated {terminated} other connections")

        for schema in ['erp_data', 'mes_milwaukee', 'mes_detroit', 'mes_cedarrapids']:
            cursor.execute(_ddl("DROP SCHEMA IF EXISTS {} CASCADE", schema))
        conn.commit()

        cursor.execute("""
            DO $$ DECLARE r RECORD;
            BEGIN
                FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
                    EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
                END LOOP;
            END $$;
        """)
        conn.commit()
        logger.info("All tables and schemas dropped")


# Indexes the bounded simulator hot paths need (ADR-0090 backlog drain; see
# erp_simulation/hot_path_indexes.py, which also ensures them on an existing
# account every cycle). A parity test holds this literal, that module and
# erp_simulation/schema/*.sql equal.
HOT_PATH_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_production_runs_traceable ON production_runs (product_id, status, actual_end_time DESC NULLS LAST, updated_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_customer_orders_status_order ON customer_orders (status, order_id);
CREATE INDEX IF NOT EXISTS idx_customer_orders_status_date ON customer_orders (status, order_date NULLS FIRST, order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_status_shipment_date ON shipments (status, shipment_date);
CREATE INDEX IF NOT EXISTS idx_shipments_status_updated ON shipments (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_invoices_status_due ON invoices (status, due_date);
"""


def create_schema(conn):
    """Create all database tables — consolidated, no additions directory needed."""
    schema_sql = "".join((
        get_schema_sql(),
        get_schema_sql_part2(),
        get_schema_sql_part3(),
        get_schema_sql_part4(),
        get_schema_sql_part5(),
        HOT_PATH_INDEXES_SQL,
    ))

    with conn.cursor() as cursor:
        cursor.execute(schema_sql)
        conn.commit()

    logger.info("Schema creation completed - 37 tables created")


def verify_schema(conn):
    """True when the reset verifies: schema_problems finds nothing (each
    problem it finds is logged)."""
    problems = schema_problems(conn)
    for problem in problems:
        logger.warning(problem)
    if problems:
        return False
    logger.info(f"Schema verification passed: {len(EXPECTED_TABLES)} tables exist")
    return True


# The tables a full reset leaves (verify_schema).
EXPECTED_TABLES = (
    'customers', 'suppliers', 'products', 'inventory_items',
    'inventory_locations', 'inventory_balances', 'inventory_transactions',
    'bill_of_materials', 'customer_orders', 'order_line_items',
    'purchase_orders', 'po_line_items', 'po_receipts',
    'production_runs', 'production_materials', 'shipments',
    'shipment_items', 'invoices', 'invoice_line_items', 'payments',
    'mes_work_orders', 'mes_production_events', 'mes_quality_checks',
    'mes_material_consumption', 'mes_downtime_events', 'mes_line_status',
    'simulation_config', 'simulation_events',
    'sites', 'product_site_routing',
    'inventory_transfers',
    'material_lots', 'lot_warehouse_movements', 'lot_production_usage',
    'lot_mes_tracking', 'production_lines', 'item_suppliers',
    'site_product_config',
    # Created by seed_data (seed_mes_equipment), before this check runs.
    'mes_equipment',
)


def schema_problems(conn):
    """What a reset left wrong, as messages; empty when it verifies.

    The EXPECTED_TABLES that are missing (then nothing else is checked), else
    every one of the 3 active legacy sites without an APPROVED material lot."""
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tablename = ANY(%s)
        """, (list(EXPECTED_TABLES),))
        existing = [row[0] for row in cursor.fetchall()]

    missing = sorted(set(EXPECTED_TABLES) - set(existing))
    if missing:
        return [f"Missing tables: {', '.join(missing)}"]

    # Verify material lots exist for all 3 active sites
    problems = []
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT s.site_code, COUNT(ml.lot_id)
            FROM sites s
            LEFT JOIN inventory_locations il ON il.location_code LIKE
                CASE s.site_code
                    WHEN 'MILWAUKEE_BOTTLING' THEN 'MKE-%'
                    WHEN 'DETROIT_AUTOPARTS' THEN 'DET-%'
                    WHEN 'CEDARRAPIDS_FOOD' THEN 'CR-%'
                END
            LEFT JOIN material_lots ml ON ml.current_location_id = il.location_id
                AND ml.quality_status = 'APPROVED'
            WHERE s.site_code IN ('MILWAUKEE_BOTTLING', 'DETROIT_AUTOPARTS', 'CEDARRAPIDS_FOOD')
            GROUP BY s.site_code
        """)
        for site_code, lot_count in cursor.fetchall():
            if lot_count == 0:
                problems.append(f"No APPROVED lots for site {site_code}")
            else:
                logger.info(f"Site {site_code}: {lot_count} APPROVED lots")
    return problems


# =============================================================================
# SEED DATA — Consolidated single path
# =============================================================================

def seed_data(conn):
    """Seed all reference data tables in correct dependency order."""
    logger.info("Starting consolidated seed data insertion...")

    with conn.cursor() as cursor:
        _seed_reference_and_sites(cursor)
        _seed_boms_inventory_and_lots(cursor)
        # Indianapolis — fourth simulated site (ADR-0083); after the three-site
        # seed so it reuses every table above.
        seed_machining_site(cursor)
        # The MES equipment registry of all four sites, before the site views
        # that read it.
        seed_mes_equipment(cursor)
        conn.commit()

    # Create view-based schemas LAST (they reference all tables above)
    with conn.cursor() as cursor:
        cursor.execute(get_views_sql())
        cursor.execute(get_machining_views_sql())
        conn.commit()

    logger.info("Consolidated seed data insertion completed")


def _seed_reference_and_sites(cursor):
    """Phases 1-2: core reference data, then sites and routing."""
    # Phase 1: Core reference data
    seed_customers(cursor)
    seed_suppliers(cursor)           # Base 12 + 11 multi-site = 23
    seed_products(cursor)            # Base 8 + 10 Detroit + 5 CR + 12 additional = 35
    seed_inventory_locations(cursor)  # Base 5 + 9 multi-site = 14
    seed_materials(cursor)           # Base 17 Milwaukee raw materials
    seed_multisite_materials(cursor)  # 13 Detroit + 13 Cedar Rapids raw materials
    seed_finished_goods(cursor)      # Base 8 Milwaukee FG
    seed_multisite_finished_goods(cursor)  # 5 Detroit + 5 Cedar Rapids FG

    # Phase 2: Sites and routing (needed before BOMs reference site-specific items)
    seed_sites(cursor)
    seed_product_site_routing(cursor)
    seed_production_lines(cursor)
    seed_site_product_config(cursor)
    seed_mes_line_status(cursor)     # Initial per-line status so cards aren't empty pre-simulation


def _seed_boms_inventory_and_lots(cursor):
    """Phases 3-10: BOMs, balances, config, suppliers, lots, extras."""
    # Phase 3: BOMs (all sites)
    seed_bom(cursor)                 # Milwaukee beverage BOMs
    seed_multisite_bom(cursor)       # Detroit + Cedar Rapids BOMs

    # Phase 4: Inventory balances (all sites)
    seed_inventory_balances(cursor)          # Milwaukee raw at WH-RAW + FG at FG-WAREHOUSE
    seed_multisite_raw_inventory(cursor)     # Detroit + Cedar Rapids raw materials
    seed_multisite_fg_inventory(cursor)      # All 3 sites FG

    # Phase 5: Simulation config
    seed_simulation_config(cursor)

    # Phase 6: Item suppliers (needed before material lots)
    seed_item_suppliers(cursor)

    # Phase 7: Material lots for ALL sites (critical for production)
    seed_material_lots_all_sites(cursor)

    # Phase 8: Customer segments
    seed_customer_segments(cursor)

    # Phase 9: Additional products
    seed_additional_products(cursor)

    # Phase 10: Parent category routing
    seed_parent_category_routing(cursor)


# =============================================================================
# SCHEMA SQL FUNCTIONS
# =============================================================================

def get_schema_sql():
    """Return SQL for tables 1-10."""
    return """
-- ============================================================================
-- 1. CUSTOMERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS customers (
    customer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_code VARCHAR(50) UNIQUE NOT NULL,
    company_name VARCHAR(200) NOT NULL,
    contact_name VARCHAR(100),
    email VARCHAR(100),
    phone VARCHAR(20),
    billing_address_line1 VARCHAR(200),
    billing_address_line2 VARCHAR(200),
    billing_city VARCHAR(100),
    billing_state VARCHAR(50),
    billing_postal_code VARCHAR(20),
    billing_country VARCHAR(50) DEFAULT 'USA',
    shipping_address_line1 VARCHAR(200),
    shipping_address_line2 VARCHAR(200),
    shipping_city VARCHAR(100),
    shipping_state VARCHAR(50),
    shipping_postal_code VARCHAR(20),
    shipping_country VARCHAR(50) DEFAULT 'USA',
    payment_terms VARCHAR(20) DEFAULT 'NET_30',
    credit_limit DECIMAL(15,2),
    customer_segment VARCHAR(50) DEFAULT 'BEVERAGE',
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'CUSTOMER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_customers_code ON customers(customer_code);
CREATE INDEX IF NOT EXISTS idx_customers_active ON customers(is_active);
CREATE INDEX IF NOT EXISTS idx_customers_segment ON customers(customer_segment);

-- ============================================================================
-- 2. SUPPLIERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_code VARCHAR(50) UNIQUE NOT NULL,
    company_name VARCHAR(200) NOT NULL,
    contact_name VARCHAR(100),
    email VARCHAR(100),
    phone VARCHAR(20),
    address_line1 VARCHAR(200),
    address_line2 VARCHAR(200),
    city VARCHAR(100),
    state VARCHAR(50),
    postal_code VARCHAR(20),
    country VARCHAR(50) DEFAULT 'USA',
    payment_terms VARCHAR(20) DEFAULT 'NET_30',
    lead_time_days INTEGER DEFAULT 7,
    is_active BOOLEAN DEFAULT TRUE,
    qualification_status VARCHAR(20) DEFAULT 'APPROVED',
    entity_type VARCHAR(50) DEFAULT 'SUPPLIER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_suppliers_code ON suppliers(supplier_code);
CREATE INDEX IF NOT EXISTS idx_suppliers_active ON suppliers(is_active);
CREATE INDEX IF NOT EXISTS idx_suppliers_qualification ON suppliers(qualification_status);

-- ============================================================================
-- 3. PRODUCTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS products (
    product_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sku VARCHAR(50) UNIQUE NOT NULL,
    product_name VARCHAR(200) NOT NULL,
    description TEXT,
    category VARCHAR(100),
    unit_of_measure VARCHAR(20) DEFAULT 'EA',
    standard_cost DECIMAL(15,2) NOT NULL,
    list_price DECIMAL(15,2) NOT NULL,
    weight_kg DECIMAL(10,2),
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'PRODUCT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);

-- ============================================================================
-- 4. INVENTORY ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_items (
    item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_code VARCHAR(50) UNIQUE NOT NULL,
    item_name VARCHAR(200) NOT NULL,
    description TEXT,
    item_type VARCHAR(20) NOT NULL CHECK (item_type IN ('RAW_MATERIAL', 'COMPONENT', 'WIP', 'FINISHED_GOOD')),
    unit_of_measure VARCHAR(20) DEFAULT 'EA',
    unit_cost DECIMAL(15,2) NOT NULL,
    reorder_point INTEGER DEFAULT 100,
    reorder_quantity INTEGER DEFAULT 500,
    product_id UUID REFERENCES products(product_id),
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inventory_items_code ON inventory_items(item_code);
CREATE INDEX IF NOT EXISTS idx_inventory_items_type ON inventory_items(item_type);
CREATE INDEX IF NOT EXISTS idx_inventory_items_product ON inventory_items(product_id);

-- ============================================================================
-- 5. INVENTORY LOCATIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_locations (
    location_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_code VARCHAR(50) UNIQUE NOT NULL,
    location_name VARCHAR(200) NOT NULL,
    location_type VARCHAR(50) DEFAULT 'WAREHOUSE',
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_LOCATION',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inventory_locations_code ON inventory_locations(location_code);
CREATE INDEX IF NOT EXISTS idx_inventory_locations_type ON inventory_locations(location_type);

-- ============================================================================
-- 6. INVENTORY BALANCES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_balances (
    balance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    location_id UUID NOT NULL REFERENCES inventory_locations(location_id),
    quantity_on_hand INTEGER DEFAULT 0,
    quantity_allocated INTEGER DEFAULT 0,
    quantity_available INTEGER GENERATED ALWAYS AS (quantity_on_hand - quantity_allocated) STORED,
    last_transaction_date TIMESTAMP,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_BALANCE',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_id, location_id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_balances_item ON inventory_balances(item_id);
CREATE INDEX IF NOT EXISTS idx_inventory_balances_location ON inventory_balances(location_id);

-- ============================================================================
-- 7. INVENTORY TRANSACTIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_transactions (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    location_id UUID NOT NULL REFERENCES inventory_locations(location_id),
    transaction_type VARCHAR(20) NOT NULL CHECK (transaction_type IN ('RECEIPT', 'CONSUMPTION', 'ADJUSTMENT', 'TRANSFER')),
    quantity INTEGER NOT NULL,
    unit_cost DECIMAL(15,2),
    reference_type VARCHAR(50),
    reference_id UUID,
    batch_id VARCHAR(100),
    transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_TRANSACTION',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_item ON inventory_transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_location ON inventory_transactions(location_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_date ON inventory_transactions(transaction_date);

-- ============================================================================
-- 8. BILL OF MATERIALS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS bill_of_materials (
    bom_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(product_id),
    component_item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    quantity_required DECIMAL(10,4) NOT NULL,
    unit_of_measure VARCHAR(20) DEFAULT 'EA',
    scrap_factor DECIMAL(5,4) DEFAULT 0.05,
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'BOM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, component_item_id)
);
CREATE INDEX IF NOT EXISTS idx_bom_product ON bill_of_materials(product_id);
CREATE INDEX IF NOT EXISTS idx_bom_component ON bill_of_materials(component_item_id);

-- ============================================================================
-- 9. CUSTOMER ORDERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS customer_orders (
    order_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number VARCHAR(50) UNIQUE NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    requested_delivery_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'BACKORDERED', 'IN_PRODUCTION', 'READY_TO_SHIP', 'SHIPPED', 'DELIVERED', 'CANCELLED')),
    subtotal DECIMAL(15,2),
    tax_amount DECIMAL(15,2),
    shipping_amount DECIMAL(15,2),
    total_amount DECIMAL(15,2),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'CUSTOMER_ORDER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_customer_orders_number ON customer_orders(order_number);
CREATE INDEX IF NOT EXISTS idx_customer_orders_customer ON customer_orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_orders_status ON customer_orders(status);
CREATE INDEX IF NOT EXISTS idx_customer_orders_date ON customer_orders(order_date);

-- ============================================================================
-- 10. ORDER LINE ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS order_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES customer_orders(order_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    product_id UUID NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    line_total DECIMAL(15,2) GENERATED ALWAYS AS (quantity * unit_price) STORED,
    entity_type VARCHAR(50) DEFAULT 'ORDER_LINE_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, line_number)
);
CREATE INDEX IF NOT EXISTS idx_order_line_items_order ON order_line_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_line_items_product ON order_line_items(product_id);
"""


def get_schema_sql_part2():
    """Return SQL for tables 11-20."""
    return """
-- ============================================================================
-- 11. PURCHASE ORDERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_number VARCHAR(50) UNIQUE NOT NULL,
    supplier_id UUID NOT NULL REFERENCES suppliers(supplier_id),
    po_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expected_delivery_date TIMESTAMP,
    actual_delivery_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'SENT', 'ACKNOWLEDGED', 'IN_TRANSIT', 'RECEIVED', 'CANCELLED')),
    total_amount DECIMAL(15,2),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PURCHASE_ORDER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_number ON purchase_orders(po_number);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_supplier ON purchase_orders(supplier_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_status ON purchase_orders(status);

-- ============================================================================
-- 12. PO LINE ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS po_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_id UUID NOT NULL REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    quantity_ordered INTEGER NOT NULL,
    quantity_received INTEGER DEFAULT 0,
    unit_price DECIMAL(15,2) NOT NULL,
    line_total DECIMAL(15,2) GENERATED ALWAYS AS (quantity_ordered * unit_price) STORED,
    entity_type VARCHAR(50) DEFAULT 'PO_LINE_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(po_id, line_number)
);
CREATE INDEX IF NOT EXISTS idx_po_line_items_po ON po_line_items(po_id);
CREATE INDEX IF NOT EXISTS idx_po_line_items_item ON po_line_items(item_id);

-- ============================================================================
-- 13. PO RECEIPTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS po_receipts (
    receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_id UUID NOT NULL REFERENCES purchase_orders(po_id),
    receipt_number VARCHAR(50) UNIQUE NOT NULL,
    receipt_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    received_by VARCHAR(100),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PO_RECEIPT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_po_receipts_po ON po_receipts(po_id);
CREATE INDEX IF NOT EXISTS idx_po_receipts_number ON po_receipts(receipt_number);

-- ============================================================================
-- 14. PRODUCTION RUNS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS production_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_number VARCHAR(50) UNIQUE NOT NULL,
    product_id UUID NOT NULL REFERENCES products(product_id),
    order_id UUID REFERENCES customer_orders(order_id),
    planned_quantity INTEGER NOT NULL,
    actual_quantity INTEGER,
    status VARCHAR(20) DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'RELEASED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')),
    planned_start_time TIMESTAMP,
    actual_start_time TIMESTAMP,
    planned_end_time TIMESTAMP,
    actual_end_time TIMESTAMP,
    location_id UUID REFERENCES inventory_locations(location_id),
    site VARCHAR(50),
    line VARCHAR(50),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PRODUCTION_RUN',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_production_runs_number ON production_runs(run_number);
CREATE INDEX IF NOT EXISTS idx_production_runs_product ON production_runs(product_id);
CREATE INDEX IF NOT EXISTS idx_production_runs_order ON production_runs(order_id);
CREATE INDEX IF NOT EXISTS idx_production_runs_status ON production_runs(status);

-- ============================================================================
-- 15. PRODUCTION MATERIALS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS production_materials (
    material_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES production_runs(run_id) ON DELETE CASCADE,
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    planned_quantity DECIMAL(10,4) NOT NULL,
    actual_quantity DECIMAL(10,4),
    entity_type VARCHAR(50) DEFAULT 'PRODUCTION_MATERIAL',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_production_materials_run ON production_materials(run_id);
CREATE INDEX IF NOT EXISTS idx_production_materials_item ON production_materials(item_id);

-- ============================================================================
-- 16. SHIPMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_number VARCHAR(50) UNIQUE NOT NULL,
    order_id UUID NOT NULL REFERENCES customer_orders(order_id),
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    shipment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    carrier VARCHAR(100),
    tracking_number VARCHAR(100),
    estimated_delivery_date TIMESTAMP,
    actual_delivery_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'PREPARING' CHECK (status IN ('PREPARING', 'PICKED', 'PACKED', 'SHIPPED', 'IN_TRANSIT', 'DELIVERED')),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'SHIPMENT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shipments_number ON shipments(shipment_number);
CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_customer ON shipments(customer_id);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);

-- ============================================================================
-- 17. SHIPMENT ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS shipment_items (
    shipment_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_id UUID NOT NULL REFERENCES shipments(shipment_id) ON DELETE CASCADE,
    line_item_id UUID NOT NULL REFERENCES order_line_items(line_item_id),
    product_id UUID NOT NULL REFERENCES products(product_id),
    quantity_shipped INTEGER NOT NULL,
    batch_id VARCHAR(100),
    entity_type VARCHAR(50) DEFAULT 'SHIPMENT_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shipment_items_shipment ON shipment_items(shipment_id);
CREATE INDEX IF NOT EXISTS idx_shipment_items_product ON shipment_items(product_id);
CREATE INDEX IF NOT EXISTS idx_shipment_items_batch ON shipment_items(batch_id);

-- ============================================================================
-- 18. INVOICES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_number VARCHAR(50) UNIQUE NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    order_id UUID REFERENCES customer_orders(order_id),
    shipment_id UUID REFERENCES shipments(shipment_id),
    invoice_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    due_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'SENT', 'PAID', 'OVERDUE', 'CANCELLED')),
    subtotal DECIMAL(15,2),
    tax_amount DECIMAL(15,2),
    total_amount DECIMAL(15,2),
    amount_paid DECIMAL(15,2) DEFAULT 0,
    balance_due DECIMAL(15,2) GENERATED ALWAYS AS (total_amount - amount_paid) STORED,
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'INVOICE',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_order ON invoices(order_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);

-- ============================================================================
-- 19. INVOICE LINE ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoice_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    description VARCHAR(200) NOT NULL,
    product_id UUID REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    line_total DECIMAL(15,2) GENERATED ALWAYS AS (quantity * unit_price) STORED,
    entity_type VARCHAR(50) DEFAULT 'INVOICE_LINE_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(invoice_id, line_number)
);
CREATE INDEX IF NOT EXISTS idx_invoice_line_items_invoice ON invoice_line_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_line_items_product ON invoice_line_items(product_id);

-- ============================================================================
-- 20. PAYMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS payments (
    payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_number VARCHAR(50) UNIQUE NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    invoice_id UUID REFERENCES invoices(invoice_id),
    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    payment_method VARCHAR(50) DEFAULT 'WIRE_TRANSFER',
    amount DECIMAL(15,2) NOT NULL,
    reference_number VARCHAR(100),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PAYMENT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_payments_number ON payments(payment_number);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
"""


def get_schema_sql_part3():
    """Return SQL for tables 21-27 (MES tables + simulation config/events)."""
    return """
-- ============================================================================
-- 21. MES WORK ORDERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS mes_work_orders (
    work_order_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_number VARCHAR(50) UNIQUE NOT NULL,
    production_run_id UUID NOT NULL REFERENCES production_runs(run_id),
    product_id UUID NOT NULL REFERENCES products(product_id),
    site VARCHAR(50) NOT NULL DEFAULT 'detroit',
    line VARCHAR(50) NOT NULL DEFAULT 'line1',
    planned_quantity INTEGER NOT NULL,
    actual_quantity INTEGER DEFAULT 0,
    good_quantity INTEGER DEFAULT 0,
    reject_quantity INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED', 'READY', 'RUNNING', 'PAUSED', 'COMPLETED', 'CANCELLED')),
    scheduled_start_time TIMESTAMP,
    actual_start_time TIMESTAMP,
    actual_end_time TIMESTAMP,
    target_rate_per_minute INTEGER,
    actual_rate_per_minute DECIMAL(10,2),
    availability DECIMAL(5,2),
    performance DECIMAL(5,2),
    oee_percentage DECIMAL(5,2),
    entity_type VARCHAR(50) DEFAULT 'MES_WORK_ORDER',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mes_work_orders_number ON mes_work_orders(work_order_number);
CREATE INDEX IF NOT EXISTS idx_mes_work_orders_production_run ON mes_work_orders(production_run_id);
CREATE INDEX IF NOT EXISTS idx_mes_work_orders_product ON mes_work_orders(product_id);
CREATE INDEX IF NOT EXISTS idx_mes_work_orders_status ON mes_work_orders(status);
CREATE INDEX IF NOT EXISTS idx_mes_work_orders_site_line ON mes_work_orders(site, line);

-- ============================================================================
-- 22. MES PRODUCTION EVENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS mes_production_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('START', 'STOP', 'PAUSE', 'RESUME', 'CHANGEOVER', 'QUALITY_CHECK', 'DOWNTIME', 'MATERIAL_CHANGE')),
    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_seconds INTEGER,
    reason_code VARCHAR(50),
    reason_description TEXT,
    operator_id VARCHAR(50),
    quantity_at_event INTEGER,
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'MES_PRODUCTION_EVENT',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mes_production_events_work_order ON mes_production_events(work_order_id);
CREATE INDEX IF NOT EXISTS idx_mes_production_events_type ON mes_production_events(event_type);
CREATE INDEX IF NOT EXISTS idx_mes_production_events_time ON mes_production_events(event_time);

-- ============================================================================
-- 23. MES QUALITY CHECKS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS mes_quality_checks (
    check_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    check_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    check_type VARCHAR(50) NOT NULL CHECK (check_type IN ('VISUAL', 'WEIGHT', 'FILL_LEVEL', 'CAP_TORQUE', 'LABEL_PLACEMENT', 'LEAK_TEST')),
    sample_size INTEGER,
    pass_count INTEGER,
    fail_count INTEGER,
    pass_rate DECIMAL(5,2) GENERATED ALWAYS AS (CASE WHEN sample_size > 0 THEN (pass_count::DECIMAL / sample_size) * 100 ELSE 0 END) STORED,
    inspector_id VARCHAR(50),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'MES_QUALITY_CHECK',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_work_order ON mes_quality_checks(work_order_id);
CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_type ON mes_quality_checks(check_type);
CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_time ON mes_quality_checks(check_time);

-- ============================================================================
-- 24. MES MATERIAL CONSUMPTION TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS mes_material_consumption (
    consumption_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    planned_quantity DECIMAL(10,4) NOT NULL,
    actual_quantity DECIMAL(10,4) NOT NULL,
    variance_quantity DECIMAL(10,4) GENERATED ALWAYS AS (actual_quantity - planned_quantity) STORED,
    variance_percentage DECIMAL(5,2) GENERATED ALWAYS AS (CASE WHEN planned_quantity > 0 THEN ((actual_quantity - planned_quantity) / planned_quantity) * 100 ELSE 0 END) STORED,
    consumption_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    lot_number VARCHAR(50),
    entity_type VARCHAR(50) DEFAULT 'MES_MATERIAL_CONSUMPTION',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_work_order ON mes_material_consumption(work_order_id);
CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_item ON mes_material_consumption(item_id);
CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_time ON mes_material_consumption(consumption_time);

-- ============================================================================
-- 25. MES DOWNTIME EVENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS mes_downtime_events (
    downtime_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID REFERENCES mes_work_orders(work_order_id),
    line_id VARCHAR(50) NOT NULL,
    downtime_category VARCHAR(50) NOT NULL CHECK (downtime_category IN ('MECHANICAL', 'ELECTRICAL', 'MATERIAL_SHORTAGE', 'QUALITY_ISSUE', 'CHANGEOVER', 'OPERATOR', 'OTHER')),
    downtime_reason VARCHAR(200),
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_minutes NUMERIC(8,2) GENERATED ALWAYS AS (CASE WHEN end_time IS NOT NULL THEN EXTRACT(EPOCH FROM (end_time - start_time)) / 60 ELSE NULL END) STORED,
    resolved_by VARCHAR(50),
    resolution_notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'MES_DOWNTIME_EVENT',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_work_order ON mes_downtime_events(work_order_id);
CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_line ON mes_downtime_events(line_id);
CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_category ON mes_downtime_events(downtime_category);
CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_start_time ON mes_downtime_events(start_time);

-- ============================================================================
-- 25b. MES LINE STATUS TABLE (current per-line telemetry: machine state,
-- runtime hours, line speed, throughput) — feeds the OEE / Line State cards.
-- ============================================================================
CREATE TABLE IF NOT EXISTS mes_line_status (
    line_status_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site VARCHAR(50) NOT NULL,
    line VARCHAR(50) NOT NULL,
    machine_state VARCHAR(20) NOT NULL DEFAULT 'IDLE' CHECK (machine_state IN ('RUNNING', 'IDLE', 'DOWN', 'CHANGEOVER')),
    runtime_hours DECIMAL(10,2) NOT NULL DEFAULT 0,
    line_speed DECIMAL(10,2),
    throughput DECIMAL(10,2),
    state_since TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    entity_type VARCHAR(50) DEFAULT 'MES_LINE_STATUS',
    source_system VARCHAR(50) DEFAULT 'MES',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_mes_line_status_site_line UNIQUE (site, line)
);
CREATE INDEX IF NOT EXISTS idx_mes_line_status_site_line ON mes_line_status(site, line);
CREATE INDEX IF NOT EXISTS idx_mes_line_status_machine_state ON mes_line_status(machine_state);

-- ============================================================================
-- 26. SIMULATION CONFIG TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS simulation_config (
    config_id SERIAL PRIMARY KEY,
    config_key VARCHAR(100) UNIQUE NOT NULL,
    config_value TEXT NOT NULL,
    data_type VARCHAR(20) NOT NULL,
    category VARCHAR(50) NOT NULL,
    description TEXT,
    min_value NUMERIC,
    max_value NUMERIC,
    default_value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW(),
    updated_by VARCHAR(100)
);
CREATE INDEX IF NOT EXISTS idx_simulation_config_key ON simulation_config(config_key);

-- ============================================================================
-- 27. SIMULATION EVENTS TABLE
-- ============================================================================
DROP TABLE IF EXISTS simulation_events;
CREATE TABLE IF NOT EXISTS simulation_events (
    event_id SERIAL PRIMARY KEY,
    event_timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    cycle_number INTEGER NOT NULL,
    orders_new INTEGER DEFAULT 0,
    orders_pending INTEGER DEFAULT 0,
    orders_confirmed INTEGER DEFAULT 0,
    orders_backordered INTEGER DEFAULT 0,
    orders_shipped INTEGER DEFAULT 0,
    orders_in_production INTEGER DEFAULT 0,
    production_new INTEGER DEFAULT 0,
    production_planned INTEGER DEFAULT 0,
    production_released INTEGER DEFAULT 0,
    production_in_progress INTEGER DEFAULT 0,
    production_completed INTEGER DEFAULT 0,
    procurement_sent INTEGER DEFAULT 0,
    procurement_in_transit INTEGER DEFAULT 0,
    procurement_received INTEGER DEFAULT 0,
    shipping_preparing INTEGER DEFAULT 0,
    shipping_picked INTEGER DEFAULT 0,
    shipping_packed INTEGER DEFAULT 0,
    shipping_shipped INTEGER DEFAULT 0,
    shipping_in_transit INTEGER DEFAULT 0,
    shipping_delivered INTEGER DEFAULT 0,
    finance_invoices INTEGER DEFAULT 0,
    finance_paid INTEGER DEFAULT 0,
    finance_revenue DECIMAL(12,2) DEFAULT 0,
    finance_collected DECIMAL(12,2) DEFAULT 0,
    mes_scheduled INTEGER DEFAULT 0,
    mes_running INTEGER DEFAULT 0,
    mes_completed INTEGER DEFAULT 0,
    mes_mke_scheduled INTEGER DEFAULT 0,
    mes_mke_running INTEGER DEFAULT 0,
    mes_mke_completed INTEGER DEFAULT 0,
    mes_det_scheduled INTEGER DEFAULT 0,
    mes_det_running INTEGER DEFAULT 0,
    mes_det_completed INTEGER DEFAULT 0,
    mes_cr_scheduled INTEGER DEFAULT 0,
    mes_cr_running INTEGER DEFAULT 0,
    mes_cr_completed INTEGER DEFAULT 0,
    inventory_transactions INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_simulation_events_timestamp ON simulation_events(event_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_simulation_events_created_at ON simulation_events(created_at);
"""


def get_schema_sql_part4():
    """Return SQL for tables 28-30 (Sites, Product Site Routing, Inventory Transfers)."""
    return """
-- ============================================================================
-- 28. SITES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS sites (
    site_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_code VARCHAR(50) UNIQUE NOT NULL,
    site_name VARCHAR(255) NOT NULL,
    site_type VARCHAR(50) NOT NULL,
    location VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sites_site_code ON sites(site_code);
CREATE INDEX IF NOT EXISTS idx_sites_is_active ON sites(is_active);

-- ============================================================================
-- 29. PRODUCT SITE ROUTING TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS product_site_routing (
    routing_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_category VARCHAR(100) NOT NULL,
    site_id UUID NOT NULL REFERENCES sites(site_id),
    priority INTEGER DEFAULT 1,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(product_category, site_id)
);
CREATE INDEX IF NOT EXISTS idx_product_site_routing_category ON product_site_routing(product_category);
CREATE INDEX IF NOT EXISTS idx_product_site_routing_site_id ON product_site_routing(site_id);
CREATE INDEX IF NOT EXISTS idx_product_site_routing_active ON product_site_routing(is_active);

-- ============================================================================
-- 30. INVENTORY TRANSFERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_transfers (
    transfer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_site_id UUID NOT NULL REFERENCES sites(site_id),
    destination_site_id UUID NOT NULL REFERENCES sites(site_id),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status VARCHAR(20) DEFAULT 'PROPOSED' CHECK (status IN (
        'PROPOSED', 'APPROVED', 'IN_TRANSIT', 'COMPLETED', 'REJECTED'
    )),
    requested_by VARCHAR(100),
    approved_by VARCHAR(100),
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,
    completed_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inventory_transfers_status ON inventory_transfers(status);
CREATE INDEX IF NOT EXISTS idx_inventory_transfers_source ON inventory_transfers(source_site_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transfers_dest ON inventory_transfers(destination_site_id);
"""


def get_schema_sql_part5():
    """Return SQL for tables 31-36 (Lot traceability, production lines, item suppliers, site config)."""
    return """
-- ============================================================================
-- 31. MATERIAL LOTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS material_lots (
    lot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_number VARCHAR(100) UNIQUE NOT NULL,
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    supplier_id UUID NOT NULL REFERENCES suppliers(supplier_id),
    po_id UUID REFERENCES purchase_orders(po_id),
    received_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    received_quantity DECIMAL(15,2) NOT NULL,
    remaining_quantity DECIMAL(15,2) NOT NULL,
    unit_of_measure VARCHAR(20) NOT NULL,
    expiry_date TIMESTAMP,
    certificate_of_analysis VARCHAR(200),
    quality_status VARCHAR(20) DEFAULT 'PENDING' CHECK (quality_status IN (
        'PENDING', 'APPROVED', 'REJECTED', 'QUARANTINE', 'EXPIRED'
    )),
    current_location_id UUID REFERENCES inventory_locations(location_id),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'MATERIAL_LOT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_material_lots_number ON material_lots(lot_number);
CREATE INDEX IF NOT EXISTS idx_material_lots_item ON material_lots(item_id);
CREATE INDEX IF NOT EXISTS idx_material_lots_supplier ON material_lots(supplier_id);
CREATE INDEX IF NOT EXISTS idx_material_lots_quality ON material_lots(quality_status);

-- ============================================================================
-- 32. LOT WAREHOUSE MOVEMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS lot_warehouse_movements (
    movement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id UUID NOT NULL REFERENCES material_lots(lot_id),
    movement_type VARCHAR(30) NOT NULL CHECK (movement_type IN (
        'RECEIVING', 'PUT_AWAY', 'PICK', 'TRANSFER', 'ADJUSTMENT', 'SHIP'
    )),
    from_location_id UUID REFERENCES inventory_locations(location_id),
    to_location_id UUID REFERENCES inventory_locations(location_id),
    quantity DECIMAL(15,2) NOT NULL,
    unit_of_measure VARCHAR(20) NOT NULL,
    reference_number VARCHAR(100),
    performed_by VARCHAR(100),
    movement_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lot_wh_movements_lot ON lot_warehouse_movements(lot_id);
CREATE INDEX IF NOT EXISTS idx_lot_wh_movements_type ON lot_warehouse_movements(movement_type);

-- ============================================================================
-- 33. LOT PRODUCTION USAGE TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS lot_production_usage (
    usage_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id UUID NOT NULL REFERENCES material_lots(lot_id),
    production_run_id UUID NOT NULL REFERENCES production_runs(run_id),
    quantity_used DECIMAL(15,2) NOT NULL,
    unit_of_measure VARCHAR(20) NOT NULL,
    usage_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lot_prod_usage_lot ON lot_production_usage(lot_id);
CREATE INDEX IF NOT EXISTS idx_lot_prod_usage_run ON lot_production_usage(production_run_id);

-- ============================================================================
-- 34. LOT MES TRACKING TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS lot_mes_tracking (
    tracking_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id UUID NOT NULL REFERENCES material_lots(lot_id),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    batch_id VARCHAR(100) NOT NULL,
    site VARCHAR(50) NOT NULL,
    line VARCHAR(50) NOT NULL,
    quantity_consumed DECIMAL(15,2) NOT NULL,
    unit_of_measure VARCHAR(20) NOT NULL,
    consumption_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lot_mes_tracking_lot ON lot_mes_tracking(lot_id);
CREATE INDEX IF NOT EXISTS idx_lot_mes_tracking_wo ON lot_mes_tracking(work_order_id);
CREATE INDEX IF NOT EXISTS idx_lot_mes_tracking_batch ON lot_mes_tracking(batch_id);
CREATE INDEX IF NOT EXISTS idx_lot_mes_tracking_site ON lot_mes_tracking(site);

-- ============================================================================
-- 35. PRODUCTION LINES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS production_lines (
    line_id VARCHAR(20) PRIMARY KEY,
    line_name VARCHAR(100) NOT NULL,
    site_id VARCHAR(20) NOT NULL,
    site_name VARCHAR(100) NOT NULL,
    product_type VARCHAR(50) NOT NULL,
    capacity_per_hour INTEGER DEFAULT 100,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- 36. ITEM SUPPLIERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS item_suppliers (
    item_supplier_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    supplier_id UUID NOT NULL REFERENCES suppliers(supplier_id),
    is_preferred BOOLEAN DEFAULT FALSE,
    lead_time_days INTEGER DEFAULT 14,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(item_id, supplier_id)
);

-- ============================================================================
-- 37. SITE PRODUCT CONFIG TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS site_product_config (
    config_id SERIAL PRIMARY KEY,
    site_id VARCHAR(20) NOT NULL,
    site_name VARCHAR(100) NOT NULL,
    product_category VARCHAR(50) NOT NULL,
    is_primary_site BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(site_id, product_category)
);
"""


# =============================================================================
# SEED FUNCTIONS — All data consolidated, correct dependency order
# =============================================================================

def seed_customers(cursor):
    """Seed 30 customer records with segment alignment."""
    segments = ['BEVERAGE', 'AUTOMOTIVE', 'FOOD']
    segment_businesses = {
        'BEVERAGE': [('Retail', ['Beverage Outlet', 'Convenience Mart', 'Liquor Store']), ('Distribution', ['Beverage Distributor', 'Wholesale Drinks', 'Regional Beverage Supply']), ('Food Service', ['Restaurant', 'Cafe', 'Fast Food Chain'])],
        'AUTOMOTIVE': [('Retail', ['Auto Parts Dealer', 'Tire & Service Center', 'Auto Body Shop']), ('Distribution', ['Automotive Distributor', 'Regional Auto Parts', 'OEM Supply Chain']), ('Industrial', ['Fleet Service Center', 'OEM Assembly Plant', 'Heavy Equipment Depot'])],
        'FOOD': [('Retail', ['Grocery Chain', 'Supermarket', 'Food Co-op', 'Health Food Store']), ('Distribution', ['Food Distributor', 'Wholesale Grocery', 'Regional Food Supply']), ('Food Service', ['Restaurant Group', 'Cafeteria Services', 'School District Food'])],
    }
    addresses = [('100 Main Street','Anytown','WA','00000'),('123 Any Street','Any Town','CA','00000')]
    contact_names = ['John Stiles', 'Jane Doe', 'Mary Major', 'Carlos Salazar', 'Diego Ramirez', 'Saanvi Sarkar', 'Mateo Jackson', 'Shirley Rodriguez']
    for i in range(30):
        random.seed(i + 100)
        segment = segments[i // 10] if i // 10 < 3 else 'BEVERAGE'
        biz_types = segment_businesses[segment]
        category, subtypes = random.choice(biz_types)  # nosec B311 - synthetic data generation, not security context
        subtype = random.choice(subtypes)  # nosec B311 - synthetic data generation, not security context
        street, city, state, postal = random.choice(addresses)  # nosec B311 - synthetic data generation, not security context
        company_prefix = random.choice(['AnyCompany', 'Example Corp'])  # nosec B311 - synthetic data generation, not security context
        company = f"{company_prefix} {subtype} {i + 1:02d}"
        contact = random.choice(contact_names)  # nosec B311 - synthetic data generation, not security context
        credit = random.randint(100000, 500000) if category == 'Distribution' else random.randint(25000, 150000)  # nosec B311 - synthetic data generation, not security context
        cursor.execute("""
            INSERT INTO customers (customer_code, company_name, contact_name, email, phone, billing_address_line1, billing_city, billing_state, billing_postal_code, billing_country, shipping_address_line1, shipping_city, shipping_state, shipping_postal_code, shipping_country, payment_terms, credit_limit, customer_segment, is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (customer_code) DO NOTHING
        """, (f"CUST{i+1:04d}", company, contact, f"{contact.lower().replace(' ','.')}@example.com", f"(202) 555-{random.randint(100,199):04d}", street, city, state, postal, 'USA', street, city, state, postal, 'USA', random.choice(['NET_3','NET_5','NET_7']), credit, segment, True))  # nosec B311 - synthetic data generation, not security context
    logger.info("Seeded 30 customers with segments")


# The three ERP suppliers whose company_name matches the supplier-master
# vendors' legal_name under a DIFFERENT id (SUPP-VM-## here vs VM-1000## in
# lambda-adt-seed/012_supplier_master.sql). These are the real exact-name
# overlap the ValueMapping crosswalk miner discovers. The names MUST stay
# byte-identical to the supplier-master legal_names and to erp_simulation's
# ANCHOR_SUPPLIERS — a path-based parity test pins all three sources together.
# (code, company_name, payment_terms, lead_time_days)
ERP_CROSSWALK_ANCHOR_SUPPLIERS = (
    ('SUPP-VM-01', 'AnyCompany Precision Alloys Inc',     'NET_30', 5),
    ('SUPP-VM-02', 'AnyCompany Industrial Fasteners LLC', 'NET_45', 7),
    ('SUPP-VM-03', 'Example Corp Packaging Systems',      'NET_60', 4),
)


def _seed_anchor_suppliers(cursor):
    """Idempotently insert the crosswalk-anchor suppliers into the ERP
    ``suppliers`` table. Shared by the full-reset seed and additive updates so
    the deployed ERP always carries the names the supplier-master crosswalks to."""
    for code, name, terms, lead_time in ERP_CROSSWALK_ANCHOR_SUPPLIERS:
        cursor.execute("""
            INSERT INTO suppliers (supplier_code, company_name, contact_name, email, phone, address_line1, city, state, postal_code, country, payment_terms, lead_time_days, is_active, qualification_status)
            VALUES (%s,%s,'Jane Doe','sales@example.com','(202) 555-0100','100 Main Street','Anytown','WA','00000','USA',%s,%s,TRUE,'APPROVED')
            ON CONFLICT (supplier_code) DO NOTHING
        """, (code, name, terms, lead_time))


def seed_suppliers(cursor):
    """Seed all 23 suppliers (base + multi-site), then the crosswalk anchors."""
    suppliers_data = [
        ('SUPP0001','AnyCompany PET Bottles','Anytown','WA','00000',3,'APPROVED'),
        ('SUPP0002','Example Corp Glass Bottles','Any Town','CA','00000',4,'APPROVED'),
        ('SUPP0003','AnyCompany Plastic Caps','Anytown','WA','00000',2,'APPROVED'),
        ('SUPP0004','Example Corp Closure Systems','Any Town','CA','00000',2,'APPROVED'),
        ('SUPP0005','AnyCompany Pressure Sensitive Labels','Anytown','WA','00000',3,'APPROVED'),
        ('SUPP0006','Example Corp Shrink Sleeves','Any Town','CA','00000',4,'CONDITIONAL'),
        ('SUPP0007','AnyCompany Beverage Concentrates','Anytown','WA','00000',5,'APPROVED'),
        ('SUPP0008','Example Corp Flavor Systems','Any Town','CA','00000',4,'DISQUALIFIED'),
        ('SUPP0009','AnyCompany Syrups','Anytown','WA','00000',5,'APPROVED'),
        ('SUPP0010','Example Corp Water Treatment','Any Town','CA','00000',2,'APPROVED'),
        ('SUPP0011','AnyCompany CO2 Supply','Anytown','WA','00000',1,'APPROVED'),
        ('SUPP0012','Example Corp Sweeteners','Any Town','CA','00000',3,'APPROVED'),
        ('SUP-MKE-GLASS','AnyCompany Glass','Anytown','WA','00000',3,'APPROVED'),
        ('SUP-MKE-FLAVOR','AnyCompany Flavors','Any Town','CA','00000',5,'APPROVED'),
        ('SUP-MKE-PACK','AnyCompany Packaging','Anytown','WA','00000',4,'UNDER_EVALUATION'),
        ('SUP-DET-CAST','AnyCompany Castings','Any Town','CA','00000',10,'APPROVED'),
        ('SUP-DET-STEEL','AnyCompany Steel Supply','Anytown','WA','00000',5,'APPROVED'),
        ('SUP-DET-ELEC','AnyCompany Electronics','Any Town','CA','00000',7,'DISQUALIFIED'),
        ('SUP-DET-COAT','AnyCompany Coatings','Anytown','WA','00000',5,'APPROVED'),
        ('SUP-CR-GRAIN','AnyCompany Grain Cooperative','Any Town','CA','00000',5,'APPROVED'),
        ('SUP-CR-SUGAR','AnyCompany Sweeteners','Anytown','WA','00000',5,'APPROVED'),
        ('SUP-CR-PACK','AnyCompany Packaging Co','Any Town','CA','00000',4,'APPROVED'),
        ('SUP-CR-ADDITIVE','AnyCompany Food Additives','Anytown','WA','00000',5,'UNDER_EVALUATION'),
    ]
    for code, name, city, state, postal, lead_time, qual in suppliers_data:
        cursor.execute("""
            INSERT INTO suppliers (supplier_code, company_name, contact_name, email, phone, address_line1, city, state, postal_code, country, payment_terms, lead_time_days, is_active, qualification_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (supplier_code) DO NOTHING
        """, (code, name, 'Jane Doe', "sales@example.com", f"(202) 555-{random.randint(100,199):04d}", '100 Main Street', city, state, postal, 'USA', 'NET_30', lead_time, True, qual))  # nosec B311 - non-cryptographic randomness for fictitious demo seed data
    _seed_anchor_suppliers(cursor)
    logger.info(f"Seeded {len(suppliers_data)} suppliers + {len(ERP_CROSSWALK_ANCHOR_SUPPLIERS)} crosswalk anchors")


def seed_products(cursor):
    """Seed all 18 products (beverages + automotive + food)."""
    products = [
        ('BEV-COLA-500','Cola 500ml','Carbonated cola 500ml','Carbonated Soft Drinks','EA',0.45,1.29,0.52),
        ('BEV-COLA-1000','Cola 1L','Carbonated cola 1L','Carbonated Soft Drinks','EA',0.75,2.19,1.04),
        ('BEV-LEMON-500','Lemon-Lime 500ml','Lemon-lime 500ml','Carbonated Soft Drinks','EA',0.42,1.29,0.52),
        ('BEV-ORANGE-500','Orange Soda 500ml','Orange soda 500ml','Carbonated Soft Drinks','EA',0.43,1.29,0.52),
        ('JUC-APPLE-1000','Apple Juice 1L','Apple juice 1L','Juices','EA',1.20,3.49,1.04),
        ('JUC-ORANGE-1000','Orange Juice 1L','Orange juice 1L','Juices','EA',1.35,3.99,1.04),
        ('WAT-PURE-500','Purified Water 500ml','Water 500ml','Water','EA',0.25,0.99,0.52),
        ('WAT-PURE-1000','Purified Water 1L','Water 1L','Water','EA',0.40,1.49,1.04),
        ('AUTO-ENGBLK-V6','V6 Engine Block','Cast aluminum V6 engine block','Engine Components','EA',450.00,850.00,45.0),
        ('AUTO-ENGBLK-V8','V8 Engine Block','Cast iron V8 engine block','Engine Components','EA',620.00,1150.00,68.0),
        ('AUTO-TRANS-6SP','6-Speed Transmission Housing','Aluminum transmission housing','Transmission Components','EA',380.00,720.00,28.0),
        ('AUTO-BRAKE-ROTOR','Brake Rotor Assembly','Ventilated disc brake rotor','Brake Components','EA',45.00,95.00,8.5),
        ('AUTO-ELEC-ECU','Engine Control Unit','Electronic engine management module','Electrical Systems','EA',180.00,420.00,0.8),
        ('FOOD-CEREAL-CF','Corn Flakes Cereal 500g','Toasted corn flakes','Cereals','EA',1.20,3.99,0.52),
        ('FOOD-CEREAL-GR','Granola Clusters 400g','Honey oat granola clusters','Cereals','EA',1.80,4.99,0.42),
        ('FOOD-SNACK-CC','Corn Chips 300g','Crispy corn tortilla chips','Snack Foods','EA',0.90,2.99,0.32),
        ('FOOD-SNACK-PZ','Pretzel Sticks 250g','Baked pretzel sticks','Snack Foods','EA',0.75,2.49,0.27),
        ('FOOD-FLOUR-AP','All-Purpose Flour 2kg','Milled all-purpose wheat flour','Grain Products','EA',0.80,2.29,2.05),
    ]
    for sku, name, desc, cat, uom, cost, price, weight in products:
        cursor.execute("""
            INSERT INTO products (sku, product_name, description, category, unit_of_measure, standard_cost, list_price, weight_kg, is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (sku) DO NOTHING
        """, (sku, name, desc, cat, uom, cost, price, weight, True))
    logger.info(f"Seeded {len(products)} products")
    seed_plm_part_xref(cursor)


# ERP↔PLM engineering cross-reference: live automotive and machined products
# carry the PLM part number of their released engineering item
# (plm_engineering.products.item_id in the ADT seed database —
# lambda-adt-seed/005_plm_engineering.sql).
# AUTO-ELEC-ECU is DELIBERATELY absent (NULL cross-ref): its electronics are
# managed in a separate ECAD system — a realistic cross-reference gap.
# The MACH-* products are seeded by seed_machining_site, AFTER seed_products
# applies this map, so seed_machining_site re-applies it at its end.
PLM_PART_XREF = {
    'AUTO-ENGBLK-V8': 'PN-ENGBLK-V8-B',
    'AUTO-ENGBLK-V6': 'PN-ENGBLK-V6-A',
    'AUTO-TRANS-6SP': 'PN-TRANS-6SP-A',
    'AUTO-BRAKE-ROTOR': 'PN-BRKROTOR-A',
    # 'AUTO-ELEC-ECU': deliberately no PLM cross-reference
    'MACH-HSG-7742': 'PN-7742-REV-C',
    'MACH-SHFT-8810': 'PN-8810-REV-B',
    'MACH-BRKT-9901': 'PN-9901-REV-A',
    'MACH-MNFD-5520': 'PN-5520-REV-B',
}


def seed_plm_part_xref(cursor):
    """Backfill products.engineering_part_number (idempotent)."""
    # Idempotent column add — keeps get_schema_sql untouched and also
    # migrates databases created before the column existed.
    cursor.execute(
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS engineering_part_number VARCHAR(50)"
    )
    for sku, plm_pn in PLM_PART_XREF.items():
        cursor.execute(
            "UPDATE products SET engineering_part_number = %s WHERE sku = %s",
            (plm_pn, sku),
        )
    logger.info(f"Applied PLM engineering-part cross-reference to {len(PLM_PART_XREF)} products")


def seed_inventory_locations(cursor):
    """Seed all 14 inventory locations."""
    locations = [
        ('WH-MAIN','Main Warehouse','WAREHOUSE'),('WH-RAW','Raw Materials Storage','WAREHOUSE'),
        ('PROD-FLOOR','Production Floor','PRODUCTION'),('FG-WAREHOUSE','Finished Goods Warehouse','WAREHOUSE'),
        ('SHIPPING-DOCK','Shipping Dock','SHIPPING'),
        ('MKE-WH-RAW','Milwaukee Raw Materials','WAREHOUSE'),('MKE-PROD','Milwaukee Production Floor','PRODUCTION'),('MKE-FG','Milwaukee Finished Goods','WAREHOUSE'),
        ('DET-WH-RAW','Detroit Raw Materials','WAREHOUSE'),('DET-PROD','Detroit Production Floor','PRODUCTION'),('DET-FG','Detroit Finished Goods','WAREHOUSE'),
        ('CR-WH-RAW','Cedar Rapids Raw Materials','WAREHOUSE'),('CR-PROD','Cedar Rapids Production Floor','PRODUCTION'),('CR-FG','Cedar Rapids Finished Goods','WAREHOUSE'),
    ]
    for code, name, loc_type in locations:
        cursor.execute("INSERT INTO inventory_locations (location_code, location_name, location_type, is_active) VALUES (%s,%s,%s,%s) ON CONFLICT (location_code) DO NOTHING", (code, name, loc_type, True))
    logger.info(f"Seeded {len(locations)} inventory locations")


def seed_materials(cursor):
    """Seed 17 Milwaukee raw materials."""
    materials = [
        ('BTL-PET-500','500ml PET Bottle','RAW_MATERIAL','EA',0.08,500,2000),
        ('BTL-PET-1000','1L PET Bottle','RAW_MATERIAL','EA',0.12,300,1500),
        ('CAP-28MM','28mm Plastic Cap','RAW_MATERIAL','EA',0.02,500,2500),
        ('CAP-38MM','38mm Plastic Cap','RAW_MATERIAL','EA',0.03,300,1500),
        ('LBL-COLA','Cola Label','RAW_MATERIAL','EA',0.04,300,1500),
        ('LBL-LEMON','Lemon-Lime Label','RAW_MATERIAL','EA',0.04,200,1000),
        ('LBL-ORANGE','Orange Soda Label','RAW_MATERIAL','EA',0.04,200,1000),
        ('LBL-APPLE','Apple Juice Label','RAW_MATERIAL','EA',0.05,150,800),
        ('LBL-OJ','Orange Juice Label','RAW_MATERIAL','EA',0.05,150,800),
        ('LBL-WATER','Water Label','RAW_MATERIAL','EA',0.03,300,1500),
        ('CONC-COLA','Cola Concentrate','RAW_MATERIAL','L',8.50,50,200),
        ('CONC-LEMON','Lemon-Lime Concentrate','RAW_MATERIAL','L',7.80,30,150),
        ('CONC-ORANGE','Orange Concentrate','RAW_MATERIAL','L',8.20,30,150),
        ('CONC-APPLE','Apple Juice Concentrate','RAW_MATERIAL','L',12.50,20,100),
        ('CONC-OJ','Orange Juice Concentrate','RAW_MATERIAL','L',15.00,20,100),
        ('WATER-PURIFIED','Purified Water','RAW_MATERIAL','L',0.001,1000,5000),
        ('CO2-FOOD','Food Grade CO2','RAW_MATERIAL','KG',0.50,50,200),
    ]
    for code, name, itype, uom, cost, rp, rq in materials:
        cursor.execute("INSERT INTO inventory_items (item_code, item_name, description, item_type, unit_of_measure, unit_cost, reorder_point, reorder_quantity, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_code) DO NOTHING", (code, name, name, itype, uom, cost, rp, rq, True))
    logger.info("Seeded 17 Milwaukee raw materials")


def seed_multisite_materials(cursor):
    """Seed 26 raw materials for Detroit + Cedar Rapids."""
    materials = [
        # Detroit automotive
        ('CAST-ALU-V6','Aluminum V6 Casting','RAW_MATERIAL','EA',120.00,200,100),
        ('CAST-IRON-V8','Cast Iron V8 Blank','RAW_MATERIAL','EA',180.00,200,80),
        ('CAST-ALU-TRANS','Aluminum Transmission Casting','RAW_MATERIAL','EA',95.00,200,100),
        ('CAST-IRON-ROTOR','Cast Iron Rotor Blank','RAW_MATERIAL','EA',12.00,200,300),
        ('STEEL-BAR-4140','4140 Steel Bar Stock','RAW_MATERIAL','KG',2.50,200,1000),
        ('ELEC-PCB-ECU','ECU Circuit Board','RAW_MATERIAL','EA',85.00,200,150),
        ('ELEC-CONN-SET','Connector Harness Set','RAW_MATERIAL','EA',12.00,200,250),
        ('COAT-ZINC','Zinc Plating Solution','RAW_MATERIAL','L',8.50,200,200),
        ('COAT-EPOXY','Epoxy Powder Coat','RAW_MATERIAL','KG',15.00,200,100),
        ('COOL-FLUID','CNC Coolant Fluid','RAW_MATERIAL','L',4.20,200,500),
        ('TOOL-INSERT-CBN','CBN Cutting Insert','RAW_MATERIAL','EA',45.00,200,100),
        ('PKG-CRATE-LG','Large Shipping Crate','RAW_MATERIAL','EA',18.00,200,150),
        ('PKG-FOAM-PAD','Protective Foam Padding','RAW_MATERIAL','EA',3.50,200,500),
        # Cedar Rapids food
        ('GRAIN-CORN-YEL','Yellow Dent Corn','RAW_MATERIAL','KG',0.18,200,20000),
        ('GRAIN-WHEAT-HRW','Hard Red Winter Wheat','RAW_MATERIAL','KG',0.22,200,15000),
        ('GRAIN-OAT-ROLLED','Rolled Oats','RAW_MATERIAL','KG',0.45,200,10000),
        ('SUGAR-GRAN','Granulated Sugar','RAW_MATERIAL','KG',0.55,200,5000),
        ('SUGAR-HONEY','Liquid Honey','RAW_MATERIAL','KG',4.50,200,1000),
        ('SALT-FOOD','Food Grade Salt','RAW_MATERIAL','KG',0.12,200,2000),
        ('OIL-CORN','Corn Oil','RAW_MATERIAL','L',1.80,200,2000),
        ('ADD-MALT','Malt Extract','RAW_MATERIAL','KG',3.20,200,500),
        ('ADD-VITAMIN','Vitamin Premix','RAW_MATERIAL','KG',25.00,200,100),
        ('PKG-BAG-500','Cereal Bag 500g','RAW_MATERIAL','EA',0.06,200,20000),
        ('PKG-BAG-300','Snack Bag 300g','RAW_MATERIAL','EA',0.05,200,20000),
        ('PKG-BOX-CEREAL','Cereal Box','RAW_MATERIAL','EA',0.12,200,15000),
        ('PKG-CASE-12','Shipping Case 12-pack','RAW_MATERIAL','EA',0.35,200,10000),
    ]
    for code, name, itype, uom, cost, rp, rq in materials:
        cursor.execute("INSERT INTO inventory_items (item_code, item_name, description, item_type, unit_of_measure, unit_cost, reorder_point, reorder_quantity, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_code) DO NOTHING", (code, name, name, itype, uom, cost, rp, rq, True))
    logger.info(f"Seeded {len(materials)} multi-site raw materials")


def seed_finished_goods(cursor):
    """Seed 8 Milwaukee finished goods linked to products."""
    fg = [
        ('FG-COLA-500','Finished Cola 500ml',0.45,50,500,'BEV-COLA-500'),
        ('FG-COLA-1000','Finished Cola 1L',0.75,30,300,'BEV-COLA-1000'),
        ('FG-LEMON-500','Finished Lemon-Lime 500ml',0.42,40,400,'BEV-LEMON-500'),
        ('FG-ORANGE-500','Finished Orange Soda 500ml',0.43,40,400,'BEV-ORANGE-500'),
        ('FG-APPLE-1000','Finished Apple Juice 1L',1.20,20,200,'JUC-APPLE-1000'),
        ('FG-OJ-1000','Finished Orange Juice 1L',1.35,20,200,'JUC-ORANGE-1000'),
        ('FG-WATER-500','Finished Water 500ml',0.25,50,500,'WAT-PURE-500'),
        ('FG-WATER-1000','Finished Water 1L',0.40,30,300,'WAT-PURE-1000'),
    ]
    for code, name, cost, rp, rq, sku in fg:
        cursor.execute("""
            INSERT INTO inventory_items (item_code, item_name, description, item_type, unit_of_measure, unit_cost, reorder_point, reorder_quantity, product_id, is_active)
            SELECT %s,%s,%s,'FINISHED_GOOD','EA',%s,%s,%s,p.product_id,%s FROM products p WHERE p.sku = %s ON CONFLICT (item_code) DO NOTHING
        """, (code, name, name, cost, rp, rq, True, sku))
    logger.info("Seeded 8 Milwaukee finished goods")


def seed_multisite_finished_goods(cursor):
    """Seed 10 finished goods for Detroit + Cedar Rapids."""
    fg = [
        ('FG-ENGBLK-V6','Finished V6 Engine Block',450.00,10,50,'AUTO-ENGBLK-V6'),
        ('FG-ENGBLK-V8','Finished V8 Engine Block',620.00,8,40,'AUTO-ENGBLK-V8'),
        ('FG-TRANS-6SP','Finished 6-Speed Trans Housing',380.00,10,50,'AUTO-TRANS-6SP'),
        ('FG-BRAKE-ROTOR','Finished Brake Rotor',45.00,30,200,'AUTO-BRAKE-ROTOR'),
        ('FG-ECU','Finished Engine Control Unit',180.00,15,80,'AUTO-ELEC-ECU'),
        ('FG-CEREAL-CF','Finished Corn Flakes 500g',1.20,500,5000,'FOOD-CEREAL-CF'),
        ('FG-CEREAL-GR','Finished Granola 400g',1.80,300,3000,'FOOD-CEREAL-GR'),
        ('FG-SNACK-CC','Finished Corn Chips 300g',0.90,500,5000,'FOOD-SNACK-CC'),
        ('FG-SNACK-PZ','Finished Pretzel Sticks 250g',0.75,500,5000,'FOOD-SNACK-PZ'),
        ('FG-FLOUR-AP','Finished All-Purpose Flour 2kg',0.80,300,3000,'FOOD-FLOUR-AP'),
    ]
    for code, name, cost, rp, rq, sku in fg:
        cursor.execute("""
            INSERT INTO inventory_items (item_code, item_name, description, item_type, unit_of_measure, unit_cost, reorder_point, reorder_quantity, product_id, is_active)
            SELECT %s,%s,%s,'FINISHED_GOOD','EA',%s,%s,%s,p.product_id,%s FROM products p WHERE p.sku = %s ON CONFLICT (item_code) DO NOTHING
        """, (code, name, name, cost, rp, rq, True, sku))
    logger.info("Seeded 10 multi-site finished goods")


def seed_sites(cursor):
    """Seed 4 sites: BOTTLING_PLANT (legacy) + 3 active multi-site."""
    sites = [
        ('BOTTLING_PLANT', 'Main Bottling Plant', 'BOTTLING_PLANT', 'Main Facility'),
        ('MILWAUKEE_BOTTLING', 'Milwaukee Beverage Bottling', 'BOTTLING', 'Milwaukee, WI'),
        ('DETROIT_AUTOPARTS', 'Detroit Automotive Parts', 'MANUFACTURING', 'Detroit, MI'),
        ('CEDARRAPIDS_FOOD', 'Cedar Rapids Food Processing', 'FOOD_PROCESSING', 'Cedar Rapids, IA'),
    ]
    for code, name, stype, loc in sites:
        cursor.execute("INSERT INTO sites (site_code, site_name, site_type, location, is_active) VALUES (%s,%s,%s,%s,TRUE) ON CONFLICT (site_code) DO NOTHING", (code, name, stype, loc))
    logger.info("Seeded 4 sites")


def seed_product_site_routing(cursor):
    """Seed product category routing to sites. Milwaukee priority=1, BOTTLING_PLANT priority=2."""
    routing = [
        # Milwaukee beverages (priority 1)
        ('Carbonated Soft Drinks', 'MILWAUKEE_BOTTLING', 1),
        ('Juices', 'MILWAUKEE_BOTTLING', 1),
        ('Water', 'MILWAUKEE_BOTTLING', 1),
        # Legacy BOTTLING_PLANT (priority 2 — Milwaukee wins)
        ('Carbonated Soft Drinks', 'BOTTLING_PLANT', 2),
        ('Juices', 'BOTTLING_PLANT', 2),
        ('Water', 'BOTTLING_PLANT', 2),
        # Detroit automotive (priority 1)
        ('Engine Components', 'DETROIT_AUTOPARTS', 1),
        ('Transmission Components', 'DETROIT_AUTOPARTS', 1),
        ('Brake Components', 'DETROIT_AUTOPARTS', 1),
        ('Electrical Systems', 'DETROIT_AUTOPARTS', 1),
        # Cedar Rapids food (priority 1)
        ('Cereals', 'CEDARRAPIDS_FOOD', 1),
        ('Snack Foods', 'CEDARRAPIDS_FOOD', 1),
        ('Grain Products', 'CEDARRAPIDS_FOOD', 1),
        # Parent category fallbacks (priority 2)
        ('Automotive', 'DETROIT_AUTOPARTS', 2),
        ('Beverages', 'MILWAUKEE_BOTTLING', 2),
        ('Food', 'CEDARRAPIDS_FOOD', 2),
    ]
    for cat, site_code, priority in routing:
        cursor.execute("""
            INSERT INTO product_site_routing (product_category, site_id, priority, is_active)
            SELECT %s, site_id, %s, TRUE FROM sites WHERE site_code = %s
            ON CONFLICT (product_category, site_id) DO NOTHING
        """, (cat, priority, site_code))
    logger.info(f"Seeded {len(routing)} product site routing entries")


def seed_production_lines(cursor):
    """Seed 9 production lines (3 per site)."""
    lines = [
        ('MKE-L1','Milwaukee Line 1','MKE','Milwaukee Bottling','Beverages',120),
        ('MKE-L2','Milwaukee Line 2','MKE','Milwaukee Bottling','Beverages',100),
        ('MKE-L3','Milwaukee Line 3','MKE','Milwaukee Bottling','Beverages',110),
        ('DET-L1','Detroit Line 1','DET','Detroit AutoParts','Automotive',80),
        ('DET-L2','Detroit Line 2','DET','Detroit AutoParts','Automotive',60),
        ('DET-L3','Detroit Line 3','DET','Detroit AutoParts','Automotive',90),
        ('CR-L1','Cedar Rapids Line 1','CR','Cedar Rapids Food','Food',150),
        ('CR-L2','Cedar Rapids Line 2','CR','Cedar Rapids Food','Food',130),
        ('CR-L3','Cedar Rapids Line 3','CR','Cedar Rapids Food','Food',110),
    ]
    for lid, name, sid, sname, ptype, cap in lines:
        cursor.execute("INSERT INTO production_lines (line_id, line_name, site_id, site_name, product_type, capacity_per_hour) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (line_id) DO NOTHING", (lid, name, sid, sname, ptype, cap))
    logger.info("Seeded 9 production lines")


def seed_site_product_config(cursor):
    """Seed site product config."""
    configs = [('MKE','Milwaukee Bottling','Beverages'),('DET','Detroit AutoParts','Automotive'),('CR','Cedar Rapids Food','Food')]
    for sid, sname, cat in configs:
        cursor.execute("INSERT INTO site_product_config (site_id, site_name, product_category) VALUES (%s,%s,%s) ON CONFLICT (site_id, product_category) DO NOTHING", (sid, sname, cat))
    logger.info("Seeded site product config")


def seed_mes_line_status(cursor):
    """Seed initial per-line MES status so OEE / Line State cards aren't empty
    before the simulator completes its first cycle. Idempotent (ON CONFLICT).

    Site codes / line names match mes_work_orders (MILWAUKEE_BOTTLING/... × LINE1-3)
    so the simulator's update_line_status upsert reuses these rows."""
    # (site, line, machine_state, runtime_hours, line_speed, throughput)
    rows = [
        ('MILWAUKEE_BOTTLING', 'LINE1', 'RUNNING',    52.5, 118.0, 940.0),
        ('MILWAUKEE_BOTTLING', 'LINE2', 'RUNNING',    48.0, 112.0, 900.0),
        ('MILWAUKEE_BOTTLING', 'LINE3', 'IDLE',       41.0,   0.0,   0.0),
        ('DETROIT_AUTOPARTS',  'LINE1', 'RUNNING',    57.0,  78.0, 620.0),
        ('DETROIT_AUTOPARTS',  'LINE2', 'CHANGEOVER', 44.5,   0.0,   0.0),
        ('DETROIT_AUTOPARTS',  'LINE3', 'RUNNING',    50.0,  86.0, 700.0),
        ('CEDARRAPIDS_FOOD',   'LINE1', 'RUNNING',    60.0, 146.0, 980.0),
        ('CEDARRAPIDS_FOOD',   'LINE2', 'DOWN',       38.0,   0.0,   0.0),
        ('CEDARRAPIDS_FOOD',   'LINE3', 'RUNNING',    55.5, 128.0, 910.0),
    ]
    for site, line, state, runtime, speed, tput in rows:
        cursor.execute(
            "INSERT INTO mes_line_status (site, line, machine_state, runtime_hours, line_speed, throughput) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (site, line) DO NOTHING",
            (site, line, state, runtime, speed, tput))
    logger.info(f"Seeded {len(rows)} MES line status rows")


def seed_bom(cursor):
    """Seed 44 Milwaukee beverage BOMs."""
    bom_data = [
        ('BEV-COLA-500','BTL-PET-500',1.0,'EA'),('BEV-COLA-500','CAP-28MM',1.0,'EA'),('BEV-COLA-500','LBL-COLA',1.0,'EA'),('BEV-COLA-500','CONC-COLA',0.08,'L'),('BEV-COLA-500','WATER-PURIFIED',0.42,'L'),('BEV-COLA-500','CO2-FOOD',0.004,'KG'),
        ('BEV-COLA-1000','BTL-PET-1000',1.0,'EA'),('BEV-COLA-1000','CAP-38MM',1.0,'EA'),('BEV-COLA-1000','LBL-COLA',1.0,'EA'),('BEV-COLA-1000','CONC-COLA',0.16,'L'),('BEV-COLA-1000','WATER-PURIFIED',0.84,'L'),('BEV-COLA-1000','CO2-FOOD',0.008,'KG'),
        ('BEV-LEMON-500','BTL-PET-500',1.0,'EA'),('BEV-LEMON-500','CAP-28MM',1.0,'EA'),('BEV-LEMON-500','LBL-LEMON',1.0,'EA'),('BEV-LEMON-500','CONC-LEMON',0.08,'L'),('BEV-LEMON-500','WATER-PURIFIED',0.42,'L'),('BEV-LEMON-500','CO2-FOOD',0.004,'KG'),
        ('BEV-ORANGE-500','BTL-PET-500',1.0,'EA'),('BEV-ORANGE-500','CAP-28MM',1.0,'EA'),('BEV-ORANGE-500','LBL-ORANGE',1.0,'EA'),('BEV-ORANGE-500','CONC-ORANGE',0.08,'L'),('BEV-ORANGE-500','WATER-PURIFIED',0.42,'L'),('BEV-ORANGE-500','CO2-FOOD',0.004,'KG'),
        ('JUC-APPLE-1000','BTL-PET-1000',1.0,'EA'),('JUC-APPLE-1000','CAP-38MM',1.0,'EA'),('JUC-APPLE-1000','LBL-APPLE',1.0,'EA'),('JUC-APPLE-1000','CONC-APPLE',0.167,'L'),('JUC-APPLE-1000','WATER-PURIFIED',0.833,'L'),
        ('JUC-ORANGE-1000','BTL-PET-1000',1.0,'EA'),('JUC-ORANGE-1000','CAP-38MM',1.0,'EA'),('JUC-ORANGE-1000','LBL-OJ',1.0,'EA'),('JUC-ORANGE-1000','CONC-OJ',0.167,'L'),('JUC-ORANGE-1000','WATER-PURIFIED',0.833,'L'),
        ('WAT-PURE-500','BTL-PET-500',1.0,'EA'),('WAT-PURE-500','CAP-28MM',1.0,'EA'),('WAT-PURE-500','LBL-WATER',1.0,'EA'),('WAT-PURE-500','WATER-PURIFIED',0.5,'L'),
        ('WAT-PURE-1000','BTL-PET-1000',1.0,'EA'),('WAT-PURE-1000','CAP-38MM',1.0,'EA'),('WAT-PURE-1000','LBL-WATER',1.0,'EA'),('WAT-PURE-1000','WATER-PURIFIED',1.0,'L'),
    ]
    for sku, mat, qty, uom in bom_data:
        cursor.execute("""
            INSERT INTO bill_of_materials (product_id, component_item_id, quantity_required, unit_of_measure, scrap_factor, is_active)
            SELECT p.product_id, i.item_id, %s, %s, 0.05, TRUE FROM products p, inventory_items i WHERE p.sku = %s AND i.item_code = %s
            ON CONFLICT (product_id, component_item_id) DO NOTHING
        """, (qty, uom, sku, mat))
    logger.info("Seeded 44 Milwaukee BOM records")


def seed_multisite_bom(cursor):
    """Seed BOMs for Detroit automotive + Cedar Rapids food."""
    bom_data = [
        ('AUTO-ENGBLK-V6','CAST-ALU-V6',1.0,'EA',0.03),('AUTO-ENGBLK-V6','STEEL-BAR-4140',8.5,'KG',0.05),('AUTO-ENGBLK-V6','COOL-FLUID',0.5,'L',0.02),('AUTO-ENGBLK-V6','COAT-EPOXY',0.2,'KG',0.02),('AUTO-ENGBLK-V6','PKG-CRATE-LG',1.0,'EA',0.01),('AUTO-ENGBLK-V6','PKG-FOAM-PAD',2.0,'EA',0.01),
        ('AUTO-ENGBLK-V8','CAST-IRON-V8',1.0,'EA',0.03),('AUTO-ENGBLK-V8','STEEL-BAR-4140',12.0,'KG',0.05),('AUTO-ENGBLK-V8','COOL-FLUID',0.8,'L',0.02),('AUTO-ENGBLK-V8','COAT-EPOXY',0.3,'KG',0.02),('AUTO-ENGBLK-V8','PKG-CRATE-LG',1.0,'EA',0.01),('AUTO-ENGBLK-V8','PKG-FOAM-PAD',2.0,'EA',0.01),
        ('AUTO-TRANS-6SP','CAST-ALU-TRANS',1.0,'EA',0.03),('AUTO-TRANS-6SP','STEEL-BAR-4140',5.0,'KG',0.05),('AUTO-TRANS-6SP','COOL-FLUID',0.4,'L',0.02),('AUTO-TRANS-6SP','COAT-ZINC',0.15,'L',0.02),('AUTO-TRANS-6SP','PKG-CRATE-LG',1.0,'EA',0.01),('AUTO-TRANS-6SP','PKG-FOAM-PAD',2.0,'EA',0.01),
        ('AUTO-BRAKE-ROTOR','CAST-IRON-ROTOR',1.0,'EA',0.04),('AUTO-BRAKE-ROTOR','COOL-FLUID',0.3,'L',0.02),('AUTO-BRAKE-ROTOR','COAT-ZINC',0.1,'L',0.02),('AUTO-BRAKE-ROTOR','PKG-FOAM-PAD',1.0,'EA',0.01),
        ('AUTO-ELEC-ECU','ELEC-PCB-ECU',1.0,'EA',0.02),('AUTO-ELEC-ECU','ELEC-CONN-SET',1.0,'EA',0.02),('AUTO-ELEC-ECU','COAT-EPOXY',0.05,'KG',0.02),
        ('FOOD-CEREAL-CF','GRAIN-CORN-YEL',0.45,'KG',0.05),('FOOD-CEREAL-CF','SUGAR-GRAN',0.02,'KG',0.03),('FOOD-CEREAL-CF','SALT-FOOD',0.005,'KG',0.03),('FOOD-CEREAL-CF','ADD-MALT',0.01,'KG',0.02),('FOOD-CEREAL-CF','ADD-VITAMIN',0.002,'KG',0.02),('FOOD-CEREAL-CF','PKG-BAG-500',1.0,'EA',0.02),('FOOD-CEREAL-CF','PKG-BOX-CEREAL',1.0,'EA',0.02),
        ('FOOD-CEREAL-GR','GRAIN-OAT-ROLLED',0.25,'KG',0.05),('FOOD-CEREAL-GR','SUGAR-HONEY',0.05,'KG',0.03),('FOOD-CEREAL-GR','SUGAR-GRAN',0.03,'KG',0.03),('FOOD-CEREAL-GR','OIL-CORN',0.02,'L',0.03),('FOOD-CEREAL-GR','PKG-BAG-500',1.0,'EA',0.02),('FOOD-CEREAL-GR','PKG-BOX-CEREAL',1.0,'EA',0.02),
        ('FOOD-SNACK-CC','GRAIN-CORN-YEL',0.28,'KG',0.05),('FOOD-SNACK-CC','OIL-CORN',0.05,'L',0.03),('FOOD-SNACK-CC','SALT-FOOD',0.005,'KG',0.03),('FOOD-SNACK-CC','PKG-BAG-300',1.0,'EA',0.02),
        ('FOOD-SNACK-PZ','GRAIN-WHEAT-HRW',0.22,'KG',0.05),('FOOD-SNACK-PZ','SALT-FOOD',0.008,'KG',0.03),('FOOD-SNACK-PZ','ADD-MALT',0.01,'KG',0.03),('FOOD-SNACK-PZ','PKG-BAG-300',1.0,'EA',0.02),
        ('FOOD-FLOUR-AP','GRAIN-WHEAT-HRW',2.1,'KG',0.05),('FOOD-FLOUR-AP','ADD-VITAMIN',0.001,'KG',0.02),('FOOD-FLOUR-AP','PKG-BAG-500',1.0,'EA',0.02),
    ]
    for sku, mat, qty, uom, scrap in bom_data:
        cursor.execute("""
            INSERT INTO bill_of_materials (product_id, component_item_id, quantity_required, unit_of_measure, scrap_factor, is_active)
            SELECT p.product_id, i.item_id, %s, %s, %s, TRUE FROM products p, inventory_items i WHERE p.sku = %s AND i.item_code = %s
            ON CONFLICT (product_id, component_item_id) DO NOTHING
        """, (qty, uom, scrap, sku, mat))
    logger.info(f"Seeded {len(bom_data)} multi-site BOM records")


def seed_inventory_balances(cursor):
    """Seed Milwaukee raw material + FG balances at base locations."""
    balances = [
        ('BTL-PET-500','WH-RAW',5000),('BTL-PET-1000','WH-RAW',3000),
        ('CAP-28MM','WH-RAW',5000),('CAP-38MM','WH-RAW',3000),
        ('LBL-COLA','WH-RAW',3000),('LBL-LEMON','WH-RAW',2000),('LBL-ORANGE','WH-RAW',2000),
        ('LBL-APPLE','WH-RAW',1500),('LBL-OJ','WH-RAW',1500),('LBL-WATER','WH-RAW',3000),
        ('CONC-COLA','WH-RAW',500),('CONC-LEMON','WH-RAW',300),('CONC-ORANGE','WH-RAW',300),
        ('CONC-APPLE','WH-RAW',200),('CONC-OJ','WH-RAW',200),
        ('WATER-PURIFIED','WH-RAW',10000),('CO2-FOOD','WH-RAW',500),
        ('FG-COLA-500','FG-WAREHOUSE',300),('FG-COLA-1000','FG-WAREHOUSE',300),
        ('FG-LEMON-500','FG-WAREHOUSE',300),('FG-ORANGE-500','FG-WAREHOUSE',300),
        ('FG-APPLE-1000','FG-WAREHOUSE',200),('FG-OJ-1000','FG-WAREHOUSE',200),
        ('FG-WATER-500','FG-WAREHOUSE',300),('FG-WATER-1000','FG-WAREHOUSE',300),
    ]
    for item_code, loc_code, qty in balances:
        cursor.execute("""
            INSERT INTO inventory_balances (item_id, location_id, quantity_on_hand, quantity_allocated, last_transaction_date)
            SELECT i.item_id, l.location_id, %s, 0, CURRENT_TIMESTAMP FROM inventory_items i, inventory_locations l
            WHERE i.item_code = %s AND l.location_code = %s ON CONFLICT (item_id, location_id) DO NOTHING
        """, (qty, item_code, loc_code))
    logger.info("Seeded Milwaukee inventory balances")


def seed_multisite_raw_inventory(cursor):
    """Seed raw material inventory at Detroit + Cedar Rapids warehouses."""
    balances = [
        # Detroit raw materials at DET-WH-RAW (high quantities for sustained production)
        ('CAST-ALU-V6','DET-WH-RAW',500),('CAST-IRON-V8','DET-WH-RAW',500),
        ('CAST-ALU-TRANS','DET-WH-RAW',500),('CAST-IRON-ROTOR','DET-WH-RAW',600),
        ('STEEL-BAR-4140','DET-WH-RAW',2000),('ELEC-PCB-ECU','DET-WH-RAW',500),
        ('ELEC-CONN-SET','DET-WH-RAW',500),('COAT-ZINC','DET-WH-RAW',400),
        ('COAT-EPOXY','DET-WH-RAW',200),('COOL-FLUID','DET-WH-RAW',1000),
        ('TOOL-INSERT-CBN','DET-WH-RAW',200),('PKG-CRATE-LG','DET-WH-RAW',300),
        ('PKG-FOAM-PAD','DET-WH-RAW',600),
        # Cedar Rapids raw materials at CR-WH-RAW
        ('GRAIN-CORN-YEL','CR-WH-RAW',20000),('GRAIN-WHEAT-HRW','CR-WH-RAW',15000),
        ('GRAIN-OAT-ROLLED','CR-WH-RAW',10000),('SUGAR-GRAN','CR-WH-RAW',5000),
        ('SUGAR-HONEY','CR-WH-RAW',1000),('SALT-FOOD','CR-WH-RAW',2000),
        ('OIL-CORN','CR-WH-RAW',2000),('ADD-MALT','CR-WH-RAW',500),
        ('ADD-VITAMIN','CR-WH-RAW',100),('PKG-BAG-500','CR-WH-RAW',20000),
        ('PKG-BAG-300','CR-WH-RAW',20000),('PKG-BOX-CEREAL','CR-WH-RAW',15000),
        ('PKG-CASE-12','CR-WH-RAW',10000),
        # Milwaukee raw materials at MKE-WH-RAW (mirror of WH-RAW)
        ('BTL-PET-500','MKE-WH-RAW',5000),('BTL-PET-1000','MKE-WH-RAW',3000),
        ('CAP-28MM','MKE-WH-RAW',5000),('CAP-38MM','MKE-WH-RAW',3000),
        ('CONC-COLA','MKE-WH-RAW',500),('CONC-LEMON','MKE-WH-RAW',300),
        ('CONC-ORANGE','MKE-WH-RAW',300),('WATER-PURIFIED','MKE-WH-RAW',10000),
        ('CO2-FOOD','MKE-WH-RAW',500),
    ]
    for item_code, loc_code, qty in balances:
        cursor.execute("""
            INSERT INTO inventory_balances (item_id, location_id, quantity_on_hand, quantity_allocated, last_transaction_date)
            SELECT i.item_id, l.location_id, %s, 0, CURRENT_TIMESTAMP FROM inventory_items i, inventory_locations l
            WHERE i.item_code = %s AND l.location_code = %s ON CONFLICT (item_id, location_id) DO NOTHING
        """, (qty, item_code, loc_code))
    logger.info("Seeded multi-site raw material inventory balances")


def seed_multisite_fg_inventory(cursor):
    """Seed FG inventory at all 3 site warehouses."""
    fg_balances = [
        ('FG-ENGBLK-V6','DET-FG',300),('FG-ENGBLK-V8','DET-FG',250),('FG-TRANS-6SP','DET-FG',300),
        ('FG-BRAKE-ROTOR','DET-FG',500),('FG-ECU','DET-FG',400),
        ('FG-CEREAL-CF','CR-FG',1000),('FG-CEREAL-GR','CR-FG',600),('FG-SNACK-CC','CR-FG',1000),
        ('FG-SNACK-PZ','CR-FG',1000),('FG-FLOUR-AP','CR-FG',600),
        ('FG-COLA-500','MKE-FG',300),('FG-COLA-1000','MKE-FG',300),('FG-LEMON-500','MKE-FG',300),
        ('FG-ORANGE-500','MKE-FG',300),('FG-APPLE-1000','MKE-FG',300),('FG-OJ-1000','MKE-FG',300),
        ('FG-WATER-500','MKE-FG',300),('FG-WATER-1000','MKE-FG',300),
    ]
    for item_code, loc_code, qty in fg_balances:
        cursor.execute("""
            INSERT INTO inventory_balances (item_id, location_id, quantity_on_hand, quantity_allocated, last_transaction_date)
            SELECT i.item_id, l.location_id, %s, 0, CURRENT_TIMESTAMP FROM inventory_items i, inventory_locations l
            WHERE i.item_code = %s AND l.location_code = %s ON CONFLICT (item_id, location_id) DO NOTHING
        """, (qty, item_code, loc_code))
    logger.info("Seeded 18 FG inventory balances at site warehouses")


# (key, value, data_type, category, description, min, max, default): the rows
# seed_simulation_config inserts (ON CONFLICT DO NOTHING).
SIMULATION_CONFIG_ROWS = (
    ('orders_per_cycle_min','4','int','orders','Min orders per cycle',0,10,'4'),
    ('orders_per_cycle_max','8','int','orders','Max orders per cycle',0,10,'8'),
    ('order_quantity_min','10','int','orders','Min order quantity',10,500,'10'),
    ('order_quantity_max','50','int','orders','Max order quantity',10,500,'50'),
    ('product_cola_weight','1.0','float','orders','Cola demand weight',0.0,5.0,'1.0'),
    ('product_orange_weight','1.0','float','orders','Orange demand weight',0.0,5.0,'1.0'),
    ('product_lemon_weight','1.0','float','orders','Lemon demand weight',0.0,5.0,'1.0'),
    ('product_grape_weight','1.0','float','orders','Grape demand weight',0.0,5.0,'1.0'),
    ('production_batch_size_min','400','int','production','Min batch size',100,1000,'400'),
    ('production_batch_size_max','600','int','production','Max batch size',100,1000,'600'),
    ('fg_reorder_point','300','int','production','FG reorder point',0,1000,'300'),
    ('mes_line1_rate','120','int','mes','Line 1 rate',50,200,'120'),
    ('mes_line2_rate','120','int','mes','Line 2 rate',50,200,'120'),
    ('mes_line3_rate','100','int','mes','Line 3 rate',50,200,'100'),
    ('mes_production_duration_min','2','int','mes','Min production duration',1,10,'2'),
    ('mes_production_duration_max','4','int','mes','Max production duration',1,10,'4'),
    ('mes_downtime_probability','0.05','float','mes','Downtime probability',0.0,0.5,'0.05'),
    ('mes_downtime_duration_min','5','int','mes','Min downtime duration',1,120,'5'),
    ('mes_downtime_duration_max','60','int','mes','Max downtime duration',1,120,'60'),
    ('mes_quality_check_interval','30','int','mes','Quality check interval',10,120,'30'),
    ('mes_quality_pass_rate_min','0.998','float','mes','Min quality pass rate',0.5,1.0,'0.998'),
    ('mes_quality_pass_rate_max','1.0','float','mes','Max quality pass rate',0.5,1.0,'1.0'),
    ('mes_material_variance_min','1.02','float','mes','Min material variance',1.0,1.2,'1.02'),
    ('mes_material_variance_max','1.05','float','mes','Max material variance',1.0,1.2,'1.05'),
    ('mes_outage_enabled','false','boolean','mes','MES outage enabled',None,None,'false'),
    ('mes_outage_duration_minutes','0','int','mes','MES outage duration',0,480,'0'),
    ('procurement_lead_time_min','1','int','procurement','Min lead time',1,30,'1'),
    ('procurement_lead_time_max','3','int','procurement','Max lead time',1,30,'3'),
    ('procurement_delivery_reliability','1.0','float','procurement','Delivery reliability',0.5,1.0,'1.0'),
    # Delivery-time spread (minutes) applied to actual_delivery_date on receipt
    # so an on-time-delivery (OTD) view can show realistic early/late variation.
    # Seeded at 0 so every deployment (the workshop included) is byte-identical
    # to the pre-feature behaviour — actual_delivery_date stays the real receipt
    # time. The demo opts into a spread by applying the Normal Operations
    # scenario, which sets this to 120 (erp_simulation/scenarios.py); db-init
    # does not know demo-vs-workshop and never enables it on its own.
    ('procurement_delivery_variability_minutes','0','float','procurement','Delivery-time spread (min) for OTD',0.0,1440.0,'0'),
    ('shipping_delay_min','1','int','shipping','Min shipping delay',1,30,'1'),
    ('shipping_delay_max','3','int','shipping','Max shipping delay',1,30,'3'),
    ('shipping_carrier_freight_weight','1.0','float','shipping','AnyCompany Freight weight',0.0,5.0,'1.0'),
    ('shipping_carrier_logistics_weight','1.0','float','shipping','AnyCompany Logistics weight',0.0,5.0,'1.0'),
    ('shipping_carrier_parcel_weight','1.0','float','shipping','AnyCompany Parcel weight',0.0,5.0,'1.0'),
    ('finance_payment_probability','0.30','float','finance','Payment probability',0.0,1.0,'0.30'),
    ('dashboard_sparkline_events','60','int','dashboard','Sparkline events',10,200,'60'),
    ('dashboard_refresh_interval','5','int','dashboard','Refresh interval',1,60,'5'),
    ('simulation_cycle_seconds','60','int','simulation','Cycle interval',10,300,'60'),
)


def seed_simulation_config(cursor):
    """Seed simulation config with tuned values for multi-site production."""
    config_data = SIMULATION_CONFIG_ROWS
    for row in config_data:
        key, val, dtype, cat, desc, minv, maxv, default = row
        cursor.execute("""
            INSERT INTO simulation_config (config_key, config_value, data_type, category, description, min_value, max_value, default_value)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (config_key) DO NOTHING
        """, (key, val, dtype, cat, desc, minv, maxv, default))
    logger.info(f"Seeded {len(config_data)} simulation config records")


#: Config keys added after accounts were seeded. The additive upsert (an
#: in-place deploy) does not re-run seed_simulation_config, so these rows are
#: added there. A row ``scenarios.apply_scenario`` already inserted for such a
#: key (category 'scenario', no description or bounds, so the dashboard's
#: Advanced panel never lists it) gets the seed's metadata; its value stays.
#: A key's default must be inert (the delivery spread's 0 is the pre-feature
#: behavior), since the row lands on every existing account, the workshop's too.
ADDED_CONFIG_KEYS = ('procurement_delivery_variability_minutes',)


def add_new_config_rows(cursor):
    """Insert each ADDED_CONFIG_KEYS seed row, or repair a scenario placeholder."""
    rows = {row[0]: row for row in SIMULATION_CONFIG_ROWS}
    for key in ADDED_CONFIG_KEYS:
        cursor.execute("""
            INSERT INTO simulation_config (config_key, config_value, data_type, category, description, min_value, max_value, default_value)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (config_key) DO UPDATE
                SET data_type = EXCLUDED.data_type, category = EXCLUDED.category,
                    description = EXCLUDED.description, min_value = EXCLUDED.min_value,
                    max_value = EXCLUDED.max_value, default_value = EXCLUDED.default_value
                WHERE simulation_config.category = 'scenario'
        """, rows[key])


# Config defaults that moved after accounts were seeded: (key, the old default
# as it may be stored, the new default). The quality pass rate moved from
# 0.90-0.99, which put a reject on nearly every routine check, to 0.998-1.0
# (mes_simulator.DEFAULT_QUALITY_PASS_RATE_MIN / _MAX). The downtime cap moved
# from 30 minutes, where the old uniform draw piled up the longest events, to
# 60 (mes_simulator.DEFAULT_DOWNTIME_DURATION_MAX), inside the dashboard
# slider's seeded 1..120 bounds.
MOVED_CONFIG_DEFAULTS = (
    ('mes_quality_pass_rate_min', ('0.90', '0.9'), '0.998'),
    ('mes_quality_pass_rate_max', ('0.99',), '1.0'),
    ('mes_downtime_duration_max', ('30',), '60'),
)
# Scenarios that set a moved default themselves (key: their updated_by
# stamps). A row such a scenario wrote at the old default is that scenario's
# old setting, so it moves with the default. Normal Operations sets the
# downtime cap (erp_simulation/scenarios.py), so an account put in Normal
# Operations on an older build holds '30' stamped 'scenario:normal'. Any
# other stamp, and any value other than the old default, stays.
MOVED_CONFIG_SCENARIO_STAMPS = {
    'mes_downtime_duration_max': ('scenario:normal',),
}


def migrate_moved_config_defaults(cursor):
    """Move each UNTOUCHED row of MOVED_CONFIG_DEFAULTS to its new default,
    and each row a MOVED_CONFIG_SCENARIO_STAMPS scenario wrote at the old
    default, then move any remaining old ``default_value`` on its own.

    Untouched means the stored value and default are both still the old
    default and no writer stamped ``updated_by`` (the seed leaves it NULL; a
    scenario stamps it). A value someone set stays as it is, and a re-run
    changes nothing. The dashboard's editor does not stamp ``updated_by``, so a
    dashboard edit that set exactly the old default cannot be told apart from
    the seed and is moved too. ``default_value`` is seed-owned for these keys
    (only db-init writes it; a scenario's upsert never does), so the last
    statement moves it even where the value stays: otherwise the dashboard's
    resets (``SET config_value = default_value``) would bring the old rate
    back. Returns the rows whose value moved."""
    moved = 0
    for key, old_values, new in MOVED_CONFIG_DEFAULTS:
        cursor.execute("""
            UPDATE simulation_config
            SET config_value = %s, default_value = %s, updated_at = NOW(),
                updated_by = 'db-init: moved default'
            WHERE config_key = %s AND config_value = ANY(%s) AND default_value = ANY(%s)
              AND updated_by IS NULL
        """, (new, new, key, list(old_values), list(old_values)))
        moved += max(0, getattr(cursor, 'rowcount', 0) or 0)
        stamps = MOVED_CONFIG_SCENARIO_STAMPS.get(key)
        if stamps:
            cursor.execute("""
                UPDATE simulation_config
                SET config_value = %s, updated_at = NOW(), updated_by = 'db-init: moved default'
                WHERE config_key = %s AND config_value = ANY(%s) AND updated_by = ANY(%s)
            """, (new, key, list(old_values), list(stamps)))
            moved += max(0, getattr(cursor, 'rowcount', 0) or 0)
        cursor.execute("""
            UPDATE simulation_config SET default_value = %s
            WHERE config_key = %s AND default_value = ANY(%s)
        """, (new, key, list(old_values)))
    logger.info(f"Moved {moved} untouched simulation config defaults")
    return moved


# mes_downtime_events.duration_minutes: a downtime's length in minutes to two
# decimal places (the CREATE TABLE above; mirrored in
# erp_simulation/schema/08_mes_tracking.sql). It was INTEGER, whole minutes,
# which tied the longest random stops; the simulator now records them to the
# second, and two whole-second lengths never round to the same two-decimal
# minute (a second is 0.0167 min), so ordering by this column or by
# end_time - start_time is the same.
DOWNTIME_DURATION_TYPE = 'numeric(8,2)'

# Moves an existing account's duration_minutes to DOWNTIME_DURATION_TYPE, once
# (already that type, or no such table: nothing happens). PostgreSQL (15, the
# deployed Aurora major, and 16) changes a stored generated column's type in
# place, keeping its position, expression and comment, but casts the stored
# value instead of recomputing it; so the rows whose real length is not a
# whole number of minutes (a 7 min 30 s repair stored as 8) are then
# recomputed from their own timestamps (SET end_time = end_time: the
# timestamps stay as they are). It refuses while a view reads the column, and
# a view keeps the column type it was created with, so every view that reads
# the column, and every view over one of those, is dropped and recreated from
# its own definition (pg_get_viewdef) with its options, comments, owner and
# access list. CREATE VIEW applies the connecting role's default privileges,
# so each recreated view whose access list then differs from the captured one
# has every grant revoked and the captured ones given back, grant options
# included, and is checked to hold exactly the captured list (aclexplode,
# owner's own entries and PUBLIC included). The owner is set back only when it
# is not the connecting role (on a deployed account every view over the column
# is db-init's or the admin route's, both owned by the connecting administrator login,
# so this never happens there). A dependent it cannot recreate exactly raises
# before anything has changed: a materialized view, a view with its own
# trigger or rule, a column-level grant, a grant made by a role other than the
# owner, a schema the connecting role cannot create views in, or an owner the
# connecting role cannot give the view back to (Aurora's administrator login is not a
# superuser: it needs to be a member of that role with its privileges, able
# to SET ROLE to it on PostgreSQL 16, and the role needs CREATE on the
# schema). One statement, in the caller's transaction.
#
# A view's effective access list, ordered: every (grantor, grantee, privilege,
# grant option) of its ACL, or of the owner's default one when it has none
# (grantee null is PUBLIC). ``k`` is the view's pg_class row.
_VIEW_ACL_SQL = """COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                          'grantor', pg_get_userbyid(a.grantor),
                          'grantee', CASE WHEN a.grantee = 0 THEN NULL ELSE pg_get_userbyid(a.grantee) END,
                          'privilege', a.privilege_type, 'grantable', a.is_grantable)
                      ORDER BY pg_get_userbyid(a.grantor),
                               CASE WHEN a.grantee = 0 THEN '' ELSE pg_get_userbyid(a.grantee) END,
                               a.privilege_type, a.is_grantable)
                 FROM aclexplode(COALESCE(k.relacl, acldefault('r', k.relowner))) a), '[]'::jsonb)"""

MIGRATE_DOWNTIME_DURATION_SQL = """
DO $migrate$
DECLARE
    tbl regclass := to_regclass('public.mes_downtime_events');
    col smallint;
    current_type text;
    as_superuser boolean;
    views jsonb;
    v jsonb;
    g record;
    c jsonb;
    rel text;
    acl_now jsonb;
BEGIN
    IF tbl IS NULL THEN
        RETURN;
    END IF;
    SELECT attnum, format_type(atttypid, atttypmod) INTO col, current_type
      FROM pg_attribute WHERE attrelid = tbl AND attname = 'duration_minutes' AND NOT attisdropped;
    IF col IS NULL OR current_type = 'numeric(8,2)' THEN
        RETURN;
    END IF;
    SELECT rolsuper INTO as_superuser FROM pg_roles WHERE rolname = current_user;
    WITH RECURSIVE dependent(oid, depth) AS (
        SELECT r.ev_class, 1
          FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
         WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.refobjid = tbl AND d.refobjsubid = col AND r.ev_class <> tbl
        UNION
        SELECT r.ev_class, p.depth + 1
          FROM dependent p
          JOIN pg_depend d ON d.refclassid = 'pg_class'::regclass AND d.refobjid = p.oid
                          AND d.classid = 'pg_rewrite'::regclass
          JOIN pg_rewrite r ON r.oid = d.objid
         WHERE r.ev_class <> p.oid
    )
    SELECT jsonb_agg(jsonb_build_object(
               'schema', n.nspname, 'name', k.relname,
               'definition', pg_get_viewdef(k.oid), 'options', to_jsonb(k.reloptions),
               'owner', pg_get_userbyid(k.relowner), 'comment', obj_description(k.oid, 'pg_class'),
               'acl', {view_acl},
               'column_comments', COALESCE((
                   SELECT jsonb_agg(jsonb_build_object('column', att.attname,
                                                       'comment', col_description(k.oid, att.attnum)))
                     FROM pg_attribute att
                    WHERE att.attrelid = k.oid AND att.attnum > 0 AND NOT att.attisdropped
                      AND col_description(k.oid, att.attnum) IS NOT NULL), '[]'::jsonb),
               'unsupported', CASE
                   WHEN k.relkind = 'm' THEN 'is a materialized view'
                   WHEN k.relkind <> 'v' THEN 'is not a plain view'
                   WHEN EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid = k.oid) THEN 'has its own trigger'
                   WHEN EXISTS (SELECT 1 FROM pg_rewrite w WHERE w.ev_class = k.oid AND w.rulename <> '_RETURN')
                       THEN 'has its own rule'
                   WHEN EXISTS (SELECT 1 FROM pg_attribute att WHERE att.attrelid = k.oid AND att.attacl IS NOT NULL)
                       THEN 'has a column-level grant'
                   WHEN EXISTS (SELECT 1 FROM aclexplode(k.relacl) a WHERE a.grantor <> k.relowner)
                       THEN 'has a grant made by a role other than its owner'
                   WHEN NOT as_superuser AND NOT has_schema_privilege(n.oid, 'CREATE')
                       THEN format('is in schema %s, where %s cannot create views', n.nspname, current_user)
                   WHEN NOT as_superuser AND pg_get_userbyid(k.relowner) <> current_user
                        AND NOT (pg_has_role(k.relowner, 'MEMBER') AND pg_has_role(k.relowner, 'USAGE')
                                 AND has_schema_privilege(k.relowner, n.oid, 'CREATE')
                                 AND CASE WHEN current_setting('server_version_num')::int >= 160000
                                          THEN pg_has_role(k.relowner, 'SET') ELSE true END)
                       THEN format('is owned by %s, which %s cannot make its owner again',
                                   pg_get_userbyid(k.relowner), current_user)
               END)
           ORDER BY x.depth DESC, n.nspname, k.relname)
      INTO views
      FROM (SELECT oid, max(depth) AS depth FROM dependent GROUP BY oid) x
      JOIN pg_class k ON k.oid = x.oid JOIN pg_namespace n ON n.oid = k.relnamespace;
    views := COALESCE(views, '[]'::jsonb);
    FOR v IN SELECT value FROM jsonb_array_elements(views) LOOP
        IF v->>'unsupported' IS NOT NULL THEN
            RAISE EXCEPTION 'mes_downtime_events.duration_minutes is read by %.%, which this migration cannot recreate exactly: it %; change it by hand',
                v->>'schema', v->>'name', v->>'unsupported';
        END IF;
    END LOOP;
    FOR v IN SELECT value FROM jsonb_array_elements(views) LOOP
        EXECUTE format('DROP VIEW %I.%I', v->>'schema', v->>'name');
    END LOOP;
    ALTER TABLE public.mes_downtime_events ALTER COLUMN duration_minutes TYPE NUMERIC(8,2);
    UPDATE public.mes_downtime_events SET end_time = end_time
     WHERE end_time IS NOT NULL
       AND duration_minutes IS DISTINCT FROM round(EXTRACT(EPOCH FROM (end_time - start_time)) / 60, 2);
    FOR v IN SELECT e.value FROM jsonb_array_elements(views) WITH ORDINALITY e(value, i) ORDER BY e.i DESC LOOP
        rel := format('%I.%I', v->>'schema', v->>'name');
        EXECUTE format('CREATE VIEW %s%s AS %s', rel,
                       CASE WHEN jsonb_typeof(v->'options') = 'array' THEN
                           ' WITH (' || (SELECT string_agg(o, ', ') FROM jsonb_array_elements_text(v->'options') o) || ')'
                       ELSE '' END,
                       rtrim(rtrim(v->>'definition'), ';'));
        IF v->>'comment' IS NOT NULL THEN
            EXECUTE format('COMMENT ON VIEW %s IS %L', rel, v->>'comment');
        END IF;
        FOR c IN SELECT value FROM jsonb_array_elements(v->'column_comments') LOOP
            EXECUTE format('COMMENT ON COLUMN %s.%I IS %L', rel, c->>'column', c->>'comment');
        END LOOP;
        IF v->>'owner' <> current_user THEN
            EXECUTE format('ALTER VIEW %s OWNER TO %I', rel, v->>'owner');
        END IF;
        SELECT {view_acl} INTO acl_now FROM pg_class k WHERE k.oid = rel::regclass;
        IF acl_now IS DISTINCT FROM v->'acl' THEN
            FOR g IN SELECT s.grantee FROM (SELECT DISTINCT e->>'grantee' AS grantee
                                              FROM jsonb_array_elements(acl_now) e) s
                      ORDER BY s.grantee IS NOT DISTINCT FROM v->>'owner', s.grantee LOOP
                EXECUTE format('REVOKE ALL ON %s FROM %s CASCADE', rel,
                               COALESCE(quote_ident(g.grantee), 'PUBLIC'));
            END LOOP;
            FOR g IN SELECT e->>'grantee' AS grantee, (e->>'grantable')::boolean AS grantable,
                            string_agg(e->>'privilege', ', ' ORDER BY e->>'privilege') AS privileges
                       FROM jsonb_array_elements(v->'acl') e
                      GROUP BY 1, 2 ORDER BY 1, 2 LOOP
                EXECUTE format('GRANT %s ON %s TO %s%s', g.privileges, rel,
                               COALESCE(quote_ident(g.grantee), 'PUBLIC'),
                               CASE WHEN g.grantable THEN ' WITH GRANT OPTION' ELSE '' END);
            END LOOP;
            SELECT {view_acl} INTO acl_now FROM pg_class k WHERE k.oid = rel::regclass;
            IF acl_now IS DISTINCT FROM v->'acl' THEN
                RAISE EXCEPTION 'could not give % back exactly its access list: it had %, now %',
                    rel, v->'acl', acl_now;
            END IF;
        END IF;
    END LOOP;
    RAISE NOTICE 'mes_downtime_events.duration_minutes: % -> numeric(8,2), % view(s) recreated',
        current_type, jsonb_array_length(views);
END
$migrate$;
""".replace("{view_acl}", _VIEW_ACL_SQL)  # nosec B608 - a constant DO block; the only substitution is another module constant


def migrate_downtime_duration_type(cursor):
    """Give an existing account's mes_downtime_events.duration_minutes two
    decimal places (MIGRATE_DOWNTIME_DURATION_SQL); idempotent."""
    cursor.execute(MIGRATE_DOWNTIME_DURATION_SQL)
    logger.info(f"mes_downtime_events.duration_minutes is {DOWNTIME_DURATION_TYPE}")


def seed_item_suppliers(cursor):
    """Seed item-supplier relationships for all sites."""
    # Map items to their primary supplier codes
    item_supplier_map = [
        # Milwaukee
        ('BTL-PET-500','SUP-MKE-GLASS',True),('BTL-PET-500','SUPP0001',False),('BTL-PET-500','SUPP0002',False),
        ('BTL-PET-1000','SUP-MKE-GLASS',True),('BTL-PET-1000','SUPP0001',False),('BTL-PET-1000','SUPP0002',False),
        ('CAP-28MM','SUPP0003',True),('CAP-28MM','SUPP0004',False),('CAP-28MM','SUP-MKE-PACK',False),
        ('CAP-38MM','SUPP0003',True),('CAP-38MM','SUPP0004',False),('CAP-38MM','SUP-MKE-PACK',False),
        ('CONC-COLA','SUPP0007',True),('CONC-COLA','SUP-MKE-FLAVOR',False),('CONC-COLA','SUPP0009',False),
        ('CONC-LEMON','SUPP0007',True),('CONC-LEMON','SUP-MKE-FLAVOR',False),
        ('CONC-ORANGE','SUPP0007',True),('CONC-ORANGE','SUP-MKE-FLAVOR',False),
        ('WATER-PURIFIED','SUPP0010',True),('WATER-PURIFIED','SUPP0012',False),
        ('CO2-FOOD','SUPP0011',True),('CO2-FOOD','SUPP0010',False),
        # Detroit
        ('CAST-ALU-V6','SUP-DET-CAST',True),('CAST-ALU-V6','SUP-DET-STEEL',False),('CAST-ALU-V6','SUP-DET-COAT',False),
        ('CAST-IRON-V8','SUP-DET-CAST',True),('CAST-IRON-V8','SUP-DET-STEEL',False),('CAST-IRON-V8','SUP-DET-COAT',False),
        ('CAST-ALU-TRANS','SUP-DET-CAST',True),('CAST-ALU-TRANS','SUP-DET-STEEL',False),
        ('CAST-IRON-ROTOR','SUP-DET-CAST',True),('CAST-IRON-ROTOR','SUP-DET-STEEL',False),
        ('STEEL-BAR-4140','SUP-DET-STEEL',True),('STEEL-BAR-4140','SUP-DET-CAST',False),
        ('ELEC-PCB-ECU','SUP-DET-ELEC',True),('ELEC-PCB-ECU','SUP-DET-STEEL',False),
        ('ELEC-CONN-SET','SUP-DET-ELEC',True),('ELEC-CONN-SET','SUP-DET-COAT',False),
        ('COAT-ZINC','SUP-DET-COAT',True),('COAT-ZINC','SUP-DET-STEEL',False),
        ('COAT-EPOXY','SUP-DET-COAT',True),('COAT-EPOXY','SUP-DET-STEEL',False),
        ('COOL-FLUID','SUP-DET-STEEL',True),('COOL-FLUID','SUP-DET-COAT',False),
        ('PKG-CRATE-LG','SUP-DET-STEEL',True),('PKG-FOAM-PAD','SUP-DET-STEEL',True),
        # Cedar Rapids
        ('GRAIN-CORN-YEL','SUP-CR-GRAIN',True),('GRAIN-CORN-YEL','SUP-CR-SUGAR',False),
        ('GRAIN-WHEAT-HRW','SUP-CR-GRAIN',True),('GRAIN-WHEAT-HRW','SUP-CR-SUGAR',False),
        ('GRAIN-OAT-ROLLED','SUP-CR-GRAIN',True),('GRAIN-OAT-ROLLED','SUP-CR-SUGAR',False),
        ('SUGAR-GRAN','SUP-CR-SUGAR',True),('SUGAR-GRAN','SUP-CR-GRAIN',False),
        ('SUGAR-HONEY','SUP-CR-SUGAR',True),('SUGAR-HONEY','SUP-CR-GRAIN',False),
        ('SALT-FOOD','SUP-CR-GRAIN',True),('OIL-CORN','SUP-CR-GRAIN',True),
        ('ADD-MALT','SUP-CR-ADDITIVE',True),('ADD-VITAMIN','SUP-CR-ADDITIVE',True),
        ('PKG-BAG-500','SUP-CR-PACK',True),('PKG-BAG-300','SUP-CR-PACK',True),
        ('PKG-BOX-CEREAL','SUP-CR-PACK',True),('PKG-CASE-12','SUP-CR-PACK',True),
    ]
    for item_code, sup_code, is_pref in item_supplier_map:
        cursor.execute("""
            INSERT INTO item_suppliers (item_id, supplier_id, is_preferred, lead_time_days)
            SELECT ii.item_id, s.supplier_id, %s, s.lead_time_days
            FROM inventory_items ii, suppliers s
            WHERE ii.item_code = %s AND s.supplier_code = %s
            ON CONFLICT (item_id, supplier_id) DO NOTHING
        """, (is_pref, item_code, sup_code))
    logger.info(f"Seeded {len(item_supplier_map)} item-supplier relationships")


def seed_material_lots_all_sites(cursor):
    """Seed APPROVED material lots for ALL raw materials at ALL sites.

    This is the CRITICAL function that ensures production can start immediately.
    Each raw material gets at least one APPROVED lot at its site warehouse.
    """
    # Detroit lots
    det_items = ['CAST-ALU-V6','CAST-IRON-V8','CAST-ALU-TRANS','CAST-IRON-ROTOR','STEEL-BAR-4140',
                 'ELEC-PCB-ECU','ELEC-CONN-SET','COAT-ZINC','COAT-EPOXY','COOL-FLUID',
                 'TOOL-INSERT-CBN','PKG-CRATE-LG','PKG-FOAM-PAD']
    for item_code in det_items:
        lot_number = f"LOT-DET-{item_code.replace('-','')}-SEED"
        cursor.execute("""
            INSERT INTO material_lots (lot_number, item_id, supplier_id, received_date, received_quantity, remaining_quantity, unit_of_measure, quality_status, current_location_id)
            SELECT %s, ii.item_id,
                   COALESCE((SELECT isup.supplier_id FROM item_suppliers isup WHERE isup.item_id = ii.item_id LIMIT 1), (SELECT s.supplier_id FROM suppliers s WHERE s.supplier_code = 'SUP-DET-CAST' LIMIT 1)),
                   NOW() - INTERVAL '3 days', 10000, 10000, ii.unit_of_measure, 'APPROVED',
                   (SELECT l.location_id FROM inventory_locations l WHERE l.location_code = 'DET-WH-RAW')
            FROM inventory_items ii WHERE ii.item_code = %s
            ON CONFLICT (lot_number) DO NOTHING
        """, (lot_number, item_code))

    # Cedar Rapids lots
    cr_items = ['GRAIN-CORN-YEL','GRAIN-WHEAT-HRW','GRAIN-OAT-ROLLED','SUGAR-GRAN','SUGAR-HONEY',
                'SALT-FOOD','OIL-CORN','ADD-MALT','ADD-VITAMIN','PKG-BAG-500','PKG-BAG-300',
                'PKG-BOX-CEREAL','PKG-CASE-12']
    for item_code in cr_items:
        lot_number = f"LOT-CR-{item_code.replace('-','')}-SEED"
        cursor.execute("""
            INSERT INTO material_lots (lot_number, item_id, supplier_id, received_date, received_quantity, remaining_quantity, unit_of_measure, quality_status, current_location_id)
            SELECT %s, ii.item_id,
                   COALESCE((SELECT isup.supplier_id FROM item_suppliers isup WHERE isup.item_id = ii.item_id LIMIT 1), (SELECT s.supplier_id FROM suppliers s WHERE s.supplier_code = 'SUP-CR-GRAIN' LIMIT 1)),
                   NOW() - INTERVAL '3 days', 50000, 50000, ii.unit_of_measure, 'APPROVED',
                   (SELECT l.location_id FROM inventory_locations l WHERE l.location_code = 'CR-WH-RAW')
            FROM inventory_items ii WHERE ii.item_code = %s
            ON CONFLICT (lot_number) DO NOTHING
        """, (lot_number, item_code))

    # Milwaukee lots
    mke_items = ['BTL-PET-500','BTL-PET-1000','CAP-28MM','CAP-38MM','CONC-COLA','CONC-LEMON',
                 'CONC-ORANGE','CONC-APPLE','CONC-OJ','WATER-PURIFIED','CO2-FOOD',
                 'LBL-COLA','LBL-LEMON','LBL-ORANGE','LBL-APPLE','LBL-OJ','LBL-WATER']
    for item_code in mke_items:
        lot_number = f"LOT-MKE-{item_code.replace('-','')}-SEED"
        cursor.execute("""
            INSERT INTO material_lots (lot_number, item_id, supplier_id, received_date, received_quantity, remaining_quantity, unit_of_measure, quality_status, current_location_id)
            SELECT %s, ii.item_id,
                   COALESCE((SELECT isup.supplier_id FROM item_suppliers isup WHERE isup.item_id = ii.item_id LIMIT 1), (SELECT s.supplier_id FROM suppliers s WHERE s.supplier_code = 'SUP-MKE-GLASS' LIMIT 1)),
                   NOW() - INTERVAL '3 days', 5000, 5000, ii.unit_of_measure, 'APPROVED',
                   (SELECT l.location_id FROM inventory_locations l WHERE l.location_code = 'MKE-WH-RAW')
            FROM inventory_items ii WHERE ii.item_code = %s
            ON CONFLICT (lot_number) DO NOTHING
        """, (lot_number, item_code))

    logger.info(f"Seeded APPROVED material lots: {len(det_items)} Detroit, {len(cr_items)} Cedar Rapids, {len(mke_items)} Milwaukee")


# =============================================================================
# INDIANAPOLIS — fourth simulated site (ADR-0083)
# =============================================================================
# Indianapolis Precision Machining is the site behind the B-2847 / CMX-441
# story that lambda-adt-seed/ tells statically (MES line 3, the
# cmms_indianapolis schema modeled after IBM Maximo, historian). This block
# makes it a REAL site of the ONE corporate ERP: MACHINING customers order
# Machined Components, orders route
# to it, the MES builds them on IND lines, its products cross-reference PLM,
# and its downtime reaches cmms_indianapolis through the MES→CMMS bridge
# (erp_simulation/mes_simulator.py).
INDIANAPOLIS_SITE_CODE = 'INDIANAPOLIS_MACHINING'
INDIANAPOLIS_SITE_NAME = 'Indianapolis Precision Machining'
INDIANAPOLIS_LINE_PREFIX = 'IND'
INDIANAPOLIS_CATEGORY = 'Machined Components'

INDIANAPOLIS_RAW_ITEMS = [
    # (item_code, name, uom, unit_cost, reorder_point, reorder_qty, on_hand)
    ('BAR-AL7075-150', 'Aluminum Bar Stock 7075-T6 1.5in RD', 'KG', 9.80, 200, 800, 2400),
    ('ROD-ST4140-100', 'Steel Rod 4140 1.0in RD', 'KG', 2.90, 200, 1200, 3000),
    ('SHT-TI64-125', 'Titanium Sheet Ti-6Al-4V 0.125in', 'KG', 68.00, 200, 150, 600),
    ('BSH-BRZ-C93200', 'Bronze Bushing C93200 1.0in OD', 'EA', 6.40, 200, 400, 1200),
    ('COOL-SYN-IND', 'Synthetic Machining Coolant', 'L', 5.10, 200, 500, 1500),
    ('INS-CARB-IND', 'Carbide Cutting Insert', 'EA', 28.00, 200, 200, 600),
    ('PKG-CRATE-SM', 'Small Shipping Crate', 'EA', 9.50, 200, 200, 600),
]


def seed_machining_site(cursor):
    """Seed Indianapolis as a full simulator site (idempotent, ON CONFLICT).

    Runs AFTER the three-site seed so it can reuse the same tables; every
    helper is ON CONFLICT DO NOTHING so re-running the seed is safe. Ends with
    the same APPROVED-lot check verify_schema applies to the legacy sites.
    """
    _seed_indianapolis_reference(cursor)
    _seed_indianapolis_products(cursor)
    _seed_indianapolis_routing(cursor)
    _seed_indianapolis_lines(cursor)
    _seed_indianapolis_bom_and_inventory(cursor)
    _seed_indianapolis_supply(cursor)
    _seed_machining_customers(cursor)
    _allow_machining_quality_checks(cursor)
    _ensure_site_event_columns(cursor)
    cursor.execute("""
        SELECT COUNT(ml.lot_id) FROM material_lots ml
        JOIN inventory_locations il ON il.location_id = ml.current_location_id
        WHERE il.location_code LIKE 'IND-%' AND ml.quality_status = 'APPROVED'
    """)
    lot_count = cursor.fetchone()[0]
    if lot_count == 0:
        logger.warning(f"No APPROVED lots for site {INDIANAPOLIS_SITE_CODE}")
    logger.info(f"Seeded Indianapolis site ({INDIANAPOLIS_SITE_CODE}): {lot_count} APPROVED lots")
    # seed_products applied the PLM cross-reference before the MACH-* products
    # existed; re-apply it now that they do (idempotent UPDATEs).
    seed_plm_part_xref(cursor)


def get_machining_views_sql():
    """View schema for the Indianapolis LIVE MES (ADR-0083).

    Distinct from the static ``mes_line3`` seed schema in the ADT seed
    database (the frozen workshop story); this schema exposes what the
    simulator builds on the IND lines. Run AFTER get_views_sql(); built by the
    same per-site generator as mes_milwaukee / mes_detroit / mes_cedarrapids.
    """
    return "\n" + _site_mes_views_sql(
        'mes_indianapolis', INDIANAPOLIS_SITE_CODE, INDIANAPOLIS_LINE_PREFIX)


# MACHINING customers of the one corporate ERP (ordered Machined Components).
MACHINING_CUSTOMERS = [
    # (customer_code, company_name, contact_name, phone)
    ('CUST-MCH-0001', 'AnyCompany Drivetrain Assembly', 'Jane Doe', '(202) 555-0161'),
    ('CUST-MCH-0002', 'Example Corp Aerospace Structures', 'John Stiles', '(202) 555-0162'),
    ('CUST-MCH-0003', 'AnyCompany Precision Parts', 'Mary Major', '(202) 555-0163'),
    ('CUST-MCH-0004', 'Example Corp Industrial Gear', 'Carlos Salazar', '(202) 555-0164'),
]

# mes_quality_checks.check_type values: the six legacy checks + DIMENSIONAL
# (machined-part SPC checks). Mirrored in erp_simulation/schema/08_mes_tracking.sql.
MES_QUALITY_CHECK_TYPES = (
    'VISUAL', 'WEIGHT', 'FILL_LEVEL', 'CAP_TORQUE', 'LABEL_PLACEMENT', 'LEAK_TEST', 'DIMENSIONAL',
)

# Per-site simulation_events MES columns added for Indianapolis (the three
# legacy sites are in the CREATE TABLE). Mirrored in 11_simulation_events.sql.
SITE_EVENT_COLUMNS = ('mes_ind_scheduled', 'mes_ind_running', 'mes_ind_completed')


def _seed_machining_customers(cursor):
    """Four MACHINING-segment customers (idempotent, ON CONFLICT)."""
    # Databases built from erp_simulation/schema before the segment column.
    cursor.execute(
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS customer_segment VARCHAR(50) DEFAULT 'BEVERAGE'")
    for code, company, contact, phone in MACHINING_CUSTOMERS:
        email = f"{contact.lower().replace(' ', '.')}@example.com"
        cursor.execute("""
            INSERT INTO customers (customer_code, company_name, contact_name, email, phone, billing_address_line1, billing_city, billing_state, billing_postal_code, billing_country, shipping_address_line1, shipping_city, shipping_state, shipping_postal_code, shipping_country, payment_terms, credit_limit, customer_segment, is_active)
            VALUES (%s,%s,%s,%s,%s,'100 Main Street','Anytown','WA','00000','USA','100 Main Street','Anytown','WA','00000','USA','NET_30',250000,'MACHINING',TRUE)
            ON CONFLICT (customer_code) DO NOTHING
        """, (code, company, contact, email, phone))
    logger.info(f"Seeded {len(MACHINING_CUSTOMERS)} MACHINING customers")


_QC_CHECK_CONSTRAINT = 'mes_quality_checks_check_type_check'


def _allow_machining_quality_checks(cursor):
    """Widen the mes_quality_checks.check_type CHECK to MES_QUALITY_CHECK_TYPES.

    Skipped when the current constraint already allows DIMENSIONAL, so a
    re-run takes no ACCESS EXCLUSIVE lock and never re-validates the table.
    """
    cursor.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'mes_quality_checks'::regclass AND conname = %s",
        (_QC_CHECK_CONSTRAINT,))
    row = cursor.fetchone()
    if row and 'DIMENSIONAL' in str(row[0]):
        return
    allowed = ", ".join(f"'{t}'" for t in MES_QUALITY_CHECK_TYPES)
    cursor.execute(
        f"ALTER TABLE mes_quality_checks DROP CONSTRAINT IF EXISTS {_QC_CHECK_CONSTRAINT}; "
        f"ALTER TABLE mes_quality_checks ADD CONSTRAINT {_QC_CHECK_CONSTRAINT} "
        f"CHECK (check_type IN ({allowed}))")  # nosec B608 - constants from this module, never input


def _ensure_site_event_columns(cursor):
    """Idempotently add the Indianapolis per-site simulation_events MES columns."""
    for column in SITE_EVENT_COLUMNS:
        cursor.execute(_ddl(
            "ALTER TABLE simulation_events ADD COLUMN IF NOT EXISTS {} INTEGER DEFAULT 0", column))


def _seed_indianapolis_reference(cursor):
    """Site row, warehouses and suppliers for Indianapolis."""
    cursor.execute(
        "INSERT INTO sites (site_code, site_name, site_type, location, is_active) "
        "VALUES (%s,%s,'MANUFACTURING','Indianapolis, IN',TRUE) ON CONFLICT (site_code) DO NOTHING",
        (INDIANAPOLIS_SITE_CODE, INDIANAPOLIS_SITE_NAME))
    locations = [
        ('IND-WH-RAW', 'Indianapolis Raw Materials', 'WAREHOUSE'),
        ('IND-PROD', 'Indianapolis Production Floor', 'PRODUCTION'),
        ('IND-FG', 'Indianapolis Finished Goods', 'WAREHOUSE'),
    ]
    for code, name, loc_type in locations:
        cursor.execute(
            "INSERT INTO inventory_locations (location_code, location_name, location_type, is_active) "
            "VALUES (%s,%s,%s,TRUE) ON CONFLICT (location_code) DO NOTHING", (code, name, loc_type))
    suppliers = [
        ('SUP-IND-METALS', 'AnyCompany Metals Service Center', 'Anytown', 'WA', 7, 'APPROVED'),
        ('SUP-IND-TOOLING', 'AnyCompany Cutting Tools', 'Any Town', 'CA', 5, 'APPROVED'),
    ]
    for code, name, city, state, lead_time, qual in suppliers:
        cursor.execute("""
            INSERT INTO suppliers (supplier_code, company_name, contact_name, email, phone, address_line1, city, state, postal_code, country, payment_terms, lead_time_days, is_active, qualification_status)
            VALUES (%s,%s,'Jane Doe','sales@example.com','(202) 555-0150','100 Main Street',%s,%s,'00000','USA','NET_30',%s,TRUE,%s)
            ON CONFLICT (supplier_code) DO NOTHING
        """, (code, name, city, state, lead_time, qual))


def _seed_indianapolis_products(cursor):
    """Machined products, raw materials and finished goods for Indianapolis.

    Product SKUs carry the part-family numbers (7742 / 8810 / 9901 / 5520) and the
    names of the PLM engineering items they cross-reference (PLM_PART_XREF;
    lambda-adt-seed/005_plm_engineering.sql), so ERP and PLM describe the
    same parts.
    """
    products = [
        ('MACH-HSG-7742', 'Precision Shaft 7742', 'High-tolerance machined precision shaft', 185.00, 340.00, 3.2),
        ('MACH-SHFT-8810', 'Drive Coupling 8810', 'Machined flexible drive coupling', 96.00, 180.00, 4.6),
        ('MACH-BRKT-9901', 'Aerospace Bracket 9901', '5-axis milled structural mounting bracket', 410.00, 760.00, 0.9),
        ('MACH-MNFD-5520', 'Hydraulic Manifold Block 5520', '5-axis milled hydraulic manifold block', 265.00, 495.00, 2.1),
    ]
    for sku, name, desc, cost, price, weight in products:
        cursor.execute("""
            INSERT INTO products (sku, product_name, description, category, unit_of_measure, standard_cost, list_price, weight_kg, is_active)
            VALUES (%s,%s,%s,%s,'EA',%s,%s,%s,TRUE) ON CONFLICT (sku) DO NOTHING
        """, (sku, name, desc, INDIANAPOLIS_CATEGORY, cost, price, weight))
    for code, name, uom, cost, rp, rq, _on_hand in INDIANAPOLIS_RAW_ITEMS:
        cursor.execute(
            "INSERT INTO inventory_items (item_code, item_name, description, item_type, unit_of_measure, unit_cost, reorder_point, reorder_quantity, is_active) "
            "VALUES (%s,%s,%s,'RAW_MATERIAL',%s,%s,%s,%s,TRUE) ON CONFLICT (item_code) DO NOTHING",
            (code, name, name, uom, cost, rp, rq))
    fg = [
        ('FG-HSG-7742', 'Finished Precision Shaft 7742', 185.00, 10, 40, 'MACH-HSG-7742'),
        ('FG-SHFT-8810', 'Finished Drive Coupling 8810', 96.00, 20, 80, 'MACH-SHFT-8810'),
        ('FG-BRKT-9901', 'Finished Aerospace Bracket 9901', 410.00, 5, 20, 'MACH-BRKT-9901'),
        ('FG-MNFD-5520', 'Finished Hydraulic Manifold Block 5520', 265.00, 8, 30, 'MACH-MNFD-5520'),
    ]
    for code, name, cost, rp, rq, sku in fg:
        cursor.execute("""
            INSERT INTO inventory_items (item_code, item_name, description, item_type, unit_of_measure, unit_cost, reorder_point, reorder_quantity, product_id, is_active)
            SELECT %s,%s,%s,'FINISHED_GOOD','EA',%s,%s,%s,p.product_id,TRUE FROM products p WHERE p.sku = %s ON CONFLICT (item_code) DO NOTHING
        """, (code, name, name, cost, rp, rq, sku))


def _seed_indianapolis_routing(cursor):
    """Route the machined-components category (and its parent) to Indianapolis."""
    routing = [(INDIANAPOLIS_CATEGORY, 1), ('Machining', 2)]
    for cat, priority in routing:
        cursor.execute("""
            INSERT INTO product_site_routing (product_category, site_id, priority, is_active)
            SELECT %s, site_id, %s, TRUE FROM sites WHERE site_code = %s
            ON CONFLICT (product_category, site_id) DO NOTHING
        """, (cat, priority, INDIANAPOLIS_SITE_CODE))


def _seed_indianapolis_lines(cursor):
    """Three IND production lines (Line 3 carries the CMX-441 5-axis mill)."""
    lines = [
        ('IND-L1', 'Indianapolis Line 1', 40), ('IND-L2', 'Indianapolis Line 2', 40),
        ('IND-L3', 'Indianapolis Line 3', 35),
    ]
    for lid, name, cap in lines:
        cursor.execute(
            "INSERT INTO production_lines (line_id, line_name, site_id, site_name, product_type, capacity_per_hour) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (line_id) DO NOTHING",
            (lid, name, INDIANAPOLIS_LINE_PREFIX, 'Indianapolis Machining', INDIANAPOLIS_CATEGORY, cap))
    cursor.execute(
        "INSERT INTO site_product_config (site_id, site_name, product_category) VALUES (%s,%s,%s) "
        "ON CONFLICT (site_id, product_category) DO NOTHING",
        (INDIANAPOLIS_LINE_PREFIX, 'Indianapolis Machining', INDIANAPOLIS_CATEGORY))
    status_rows = [
        ('LINE1', 'RUNNING', 46.0, 38.0, 32.0), ('LINE2', 'IDLE', 39.5, 0.0, 0.0),
        ('LINE3', 'RUNNING', 51.0, 33.0, 28.0),
    ]
    for line, state, runtime, speed, tput in status_rows:
        cursor.execute(
            "INSERT INTO mes_line_status (site, line, machine_state, runtime_hours, line_speed, throughput) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (site, line) DO NOTHING",
            (INDIANAPOLIS_SITE_CODE, line, state, runtime, speed, tput))


def _seed_indianapolis_bom_and_inventory(cursor):
    """BOMs plus raw and FG balances at the IND warehouses."""
    bom_data = [
        ('MACH-HSG-7742', 'BAR-AL7075-150', 4.2, 'KG', 0.06), ('MACH-HSG-7742', 'BSH-BRZ-C93200', 2.0, 'EA', 0.01),
        ('MACH-HSG-7742', 'COOL-SYN-IND', 0.4, 'L', 0.02), ('MACH-HSG-7742', 'INS-CARB-IND', 0.1, 'EA', 0.02),
        ('MACH-HSG-7742', 'PKG-CRATE-SM', 1.0, 'EA', 0.01),
        ('MACH-SHFT-8810', 'ROD-ST4140-100', 6.0, 'KG', 0.05), ('MACH-SHFT-8810', 'COOL-SYN-IND', 0.5, 'L', 0.02),
        ('MACH-SHFT-8810', 'INS-CARB-IND', 0.15, 'EA', 0.02), ('MACH-SHFT-8810', 'PKG-CRATE-SM', 1.0, 'EA', 0.01),
        ('MACH-BRKT-9901', 'SHT-TI64-125', 1.4, 'KG', 0.08), ('MACH-BRKT-9901', 'COOL-SYN-IND', 0.3, 'L', 0.02),
        ('MACH-BRKT-9901', 'INS-CARB-IND', 0.2, 'EA', 0.03), ('MACH-BRKT-9901', 'PKG-CRATE-SM', 1.0, 'EA', 0.01),
        ('MACH-MNFD-5520', 'BAR-AL7075-150', 3.6, 'KG', 0.07), ('MACH-MNFD-5520', 'COOL-SYN-IND', 0.4, 'L', 0.02),
        ('MACH-MNFD-5520', 'INS-CARB-IND', 0.2, 'EA', 0.03), ('MACH-MNFD-5520', 'PKG-CRATE-SM', 1.0, 'EA', 0.01),
    ]
    for sku, mat, qty, uom, scrap in bom_data:
        cursor.execute("""
            INSERT INTO bill_of_materials (product_id, component_item_id, quantity_required, unit_of_measure, scrap_factor, is_active)
            SELECT p.product_id, i.item_id, %s, %s, %s, TRUE FROM products p, inventory_items i WHERE p.sku = %s AND i.item_code = %s
            ON CONFLICT (product_id, component_item_id) DO NOTHING
        """, (qty, uom, scrap, sku, mat))
    balances = [(code, 'IND-WH-RAW', on_hand) for code, *_rest, on_hand in INDIANAPOLIS_RAW_ITEMS]
    balances += [('FG-HSG-7742', 'IND-FG', 120), ('FG-SHFT-8810', 'IND-FG', 200), ('FG-BRKT-9901', 'IND-FG', 60),
                 ('FG-MNFD-5520', 'IND-FG', 80)]
    for item_code, loc_code, qty in balances:
        cursor.execute("""
            INSERT INTO inventory_balances (item_id, location_id, quantity_on_hand, quantity_allocated, last_transaction_date)
            SELECT i.item_id, l.location_id, %s, 0, CURRENT_TIMESTAMP FROM inventory_items i, inventory_locations l
            WHERE i.item_code = %s AND l.location_code = %s ON CONFLICT (item_id, location_id) DO NOTHING
        """, (qty, item_code, loc_code))


def _seed_indianapolis_supply(cursor):
    """Item-supplier links and APPROVED lots so IND production can start."""
    tooling = {'INS-CARB-IND', 'COOL-SYN-IND'}
    for code, *_rest in INDIANAPOLIS_RAW_ITEMS:
        sup_code = 'SUP-IND-TOOLING' if code in tooling else 'SUP-IND-METALS'
        cursor.execute("""
            INSERT INTO item_suppliers (item_id, supplier_id, is_preferred, lead_time_days)
            SELECT ii.item_id, s.supplier_id, TRUE, s.lead_time_days
            FROM inventory_items ii, suppliers s WHERE ii.item_code = %s AND s.supplier_code = %s
            ON CONFLICT (item_id, supplier_id) DO NOTHING
        """, (code, sup_code))
        lot_number = f"LOT-IND-{code.replace('-', '')}-SEED"
        cursor.execute("""
            INSERT INTO material_lots (lot_number, item_id, supplier_id, received_date, received_quantity, remaining_quantity, unit_of_measure, quality_status, current_location_id)
            SELECT %s, ii.item_id,
                   COALESCE((SELECT isup.supplier_id FROM item_suppliers isup WHERE isup.item_id = ii.item_id LIMIT 1), (SELECT s.supplier_id FROM suppliers s WHERE s.supplier_code = 'SUP-IND-METALS' LIMIT 1)),
                   NOW() - INTERVAL '3 days', 8000, 8000, ii.unit_of_measure, 'APPROVED',
                   (SELECT l.location_id FROM inventory_locations l WHERE l.location_code = 'IND-WH-RAW')
            FROM inventory_items ii WHERE ii.item_code = %s
            ON CONFLICT (lot_number) DO NOTHING
        """, (lot_number, code))


# =============================================================================
# MES EQUIPMENT REGISTRY: one work center per production line per site
# =============================================================================
# (site, line, equipment_id, equipment_name, equipment_type). ``line`` is the
# mes_work_orders.line / mes_line_status.line value the line's work runs
# under. The Indianapolis LINE3 name is the text of assets.description for
# the same machine in lambda-adt-seed/011_cmms_indianapolis.sql. The legacy
# LINE1/LINE2 names are the corporate CMMS assets.asset_name of the line's
# filler, CNC machining center or extruder (lambda-adt-seed/004); every other
# name is used by no other seeded system. Exposed per site as the
# ``equipment`` view of each site MES schema (_site_mes_views_sql).
MES_EQUIPMENT = (
    ('MILWAUKEE_BOTTLING', 'LINE1', 'MKE-L1-M1', 'Rotary Filler Station A', 'FILLER'),
    ('MILWAUKEE_BOTTLING', 'LINE2', 'MKE-L2-M1', 'Rotary Filler Station B', 'FILLER'),
    ('MILWAUKEE_BOTTLING', 'LINE3', 'MKE-L3-M1', 'Rotary Filler Station C', 'FILLER'),
    ('DETROIT_AUTOPARTS', 'LINE1', 'DET-L1-M1', 'CNC Machining Center L1-A', 'CNC_MACHINE'),
    ('DETROIT_AUTOPARTS', 'LINE2', 'DET-L2-M1', 'CNC Machining Center L2-A', 'CNC_MACHINE'),
    ('DETROIT_AUTOPARTS', 'LINE3', 'DET-L3-M1', 'CNC Machining Center L3-A', 'CNC_MACHINE'),
    ('CEDARRAPIDS_FOOD', 'LINE1', 'CR-L1-M1', 'Twin-Screw Extruder L1', 'EXTRUDER'),
    ('CEDARRAPIDS_FOOD', 'LINE2', 'CR-L2-M1', 'Twin-Screw Extruder L2', 'EXTRUDER'),
    ('CEDARRAPIDS_FOOD', 'LINE3', 'CR-L3-M1', 'Twin-Screw Extruder L3', 'EXTRUDER'),
    (INDIANAPOLIS_SITE_CODE, 'LINE1', 'L1-M1', 'CNC Turning Center 310', 'CNC_LATHE'),
    (INDIANAPOLIS_SITE_CODE, 'LINE2', 'L2-M1', 'Horizontal Machining Center 220', 'CNC_MILL'),
    (INDIANAPOLIS_SITE_CODE, 'LINE3', 'L3-M1', '5-Axis Milling Center CMX-441', 'CNC_MILL'),
)

MES_EQUIPMENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mes_equipment (
    site VARCHAR(50) NOT NULL,
    line VARCHAR(50) NOT NULL,
    equipment_id VARCHAR(20) NOT NULL,
    equipment_name VARCHAR(120) NOT NULL,
    equipment_type VARCHAR(50),
    status VARCHAR(20) DEFAULT 'ACTIVE',
    PRIMARY KEY (site, equipment_id),
    UNIQUE (site, equipment_name)
);
"""


def seed_mes_equipment(cursor):
    """The MES equipment registry (MES_EQUIPMENT): the table if it is missing,
    then its rows, ON CONFLICT DO NOTHING. Idempotent, so the Create seed and
    the seed-only Update (seed_catalogue_upsert) both run it; a row an
    account already holds is never changed."""
    cursor.execute(MES_EQUIPMENT_TABLE_SQL)
    for site, line, equipment_id, name, equipment_type in MES_EQUIPMENT:
        cursor.execute(
            "INSERT INTO mes_equipment (site, line, equipment_id, equipment_name, equipment_type) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (site, line, equipment_id, name, equipment_type))
    logger.info(f"Seeded {len(MES_EQUIPMENT)} MES equipment registry rows")


def seed_customer_segments(cursor):
    """Customer segments are already set during seed_customers (inline). This is a no-op."""
    pass


def seed_additional_products(cursor):
    """Seed additional products for the demo ERP dashboard integration."""
    additional = [
        ('FWL-500','Flavored Water Lemon 500ml','Water','EA',1.20,1.60),
        ('FWB-500','Flavored Water Berry 500ml','Water','EA',1.20,1.60),
        ('DC-330','Diet Cola 330ml','Carbonated Soft Drinks','EA',1.40,1.85),
        ('GA-330','Ginger Ale 330ml','Carbonated Soft Drinks','EA',1.30,1.70),
        ('VW-500','Vitamin Water 500ml','Water','EA',1.80,2.30),
        ('PS-350','Protein Shake 350ml','Juices','EA',2.50,3.50),
        ('BRA-001','Brake Rotor Assembly','Brake Components','EA',45.00,89.99),
        ('TGS-001','Transmission Gear Set','Transmission Components','EA',120.00,249.99),
        ('EGK-001','Engine Gasket Kit','Engine Components','EA',25.00,54.99),
        ('CF-500','Corn Flakes 500g','Cereals','EA',1.50,3.29),
        ('GB-6PK','Granola Bar 6pk','Snack Foods','EA',2.00,4.49),
        ('OM-1L','Oat Milk 1L','Grain Products','EA',1.80,3.99),
    ]
    for sku, name, cat, uom, cost, price in additional:
        cursor.execute("""
            INSERT INTO products (sku, product_name, description, category, unit_of_measure, standard_cost, list_price, is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT (sku) DO UPDATE SET category = EXCLUDED.category
        """, (sku, name, name, cat, uom, cost, price))
    logger.info(f"Seeded {len(additional)} additional products")


def seed_parent_category_routing(cursor):
    """Parent category routing is already handled in seed_product_site_routing. No-op."""
    pass


# Customer segment -> product categories it may order (customer_product_categories
# view). Unknown segments fall back to BEVERAGE. erp_simulation/segments.py holds
# the SAME content for the order simulator (parity-tested); the Lambda never
# imports erp_simulation, so the literal lives here too.
SEGMENT_CATEGORIES = {
    'BEVERAGE': ['Carbonated Soft Drinks', 'Juices', 'Water'],
    'AUTOMOTIVE': ['Engine Components', 'Transmission Components', 'Brake Components', 'Electrical Systems'],
    'FOOD': ['Cereals', 'Snack Foods', 'Grain Products'],
    'MACHINING': ['Machined Components'],
}

# public tables exposed 1:1 as erp_data.<table> views (the corporate ERP).
_ERP_DATA_VIEW_TABLES = (
    'customers', 'suppliers', 'products', 'customer_orders', 'order_line_items',
    'purchase_orders', 'po_line_items', 'po_receipts', 'invoices', 'invoice_line_items',
    'payments', 'inventory_items', 'inventory_balances', 'inventory_locations',
    'inventory_transactions', 'inventory_transfers', 'shipments', 'shipment_items',
    'material_lots', 'lot_warehouse_movements', 'lot_mes_tracking', 'lot_production_usage',
    'bill_of_materials', 'item_suppliers', 'product_site_routing',
)

# (view schema, mes_work_orders.site, production_lines.site_id) per legacy site.
_LEGACY_SITE_MES_VIEWS = (
    ('mes_milwaukee', 'MILWAUKEE_BOTTLING', 'MKE'),
    ('mes_detroit', 'DETROIT_AUTOPARTS', 'DET'),
    ('mes_cedarrapids', 'CEDARRAPIDS_FOOD', 'CR'),
)


def _erp_data_views_sql():
    """erp_data schema: one pass-through view per corporate ERP table."""
    lines = ["CREATE SCHEMA IF NOT EXISTS erp_data;\n"]
    lines += [f"CREATE OR REPLACE VIEW erp_data.{t} AS SELECT * FROM public.{t};\n"  # nosec B608 - constants from this module, never input
              for t in _ERP_DATA_VIEW_TABLES]
    return "".join(lines)


def _site_mes_views_sql(schema, site_code, line_prefix):
    """One site's MES view schema over the shared public MES tables."""
    wo_join = "JOIN public.mes_work_orders w ON {a}.work_order_id = w.work_order_id"
    joined = [('production_events', 'mes_production_events', 'e'),
              ('quality_checks', 'mes_quality_checks', 'q'),
              ('downtime_events', 'mes_downtime_events', 'd'),
              ('material_consumption', 'mes_material_consumption', 'm')]
    lines = [f"CREATE SCHEMA IF NOT EXISTS {schema};\n",
             f"CREATE OR REPLACE VIEW {schema}.work_orders AS SELECT * FROM public.mes_work_orders WHERE site = '{site_code}';\n"]  # nosec B608 - constants from this module, never input
    lines += [f"CREATE OR REPLACE VIEW {schema}.{view} AS SELECT {a}.* FROM public.{table} {a} "
              f"{wo_join.format(a=a)} WHERE w.site = '{site_code}';\n" for view, table, a in joined]  # nosec B608 - constants from this module, never input
    lines += [f"CREATE OR REPLACE VIEW {schema}.production_lines AS SELECT * FROM public.production_lines WHERE site_id = '{line_prefix}';\n",  # nosec B608 - constants from this module, never input
              f"CREATE OR REPLACE VIEW {schema}.line_status AS SELECT * FROM public.mes_line_status WHERE site = '{site_code}';\n",  # nosec B608 - constants from this module, never input
              _site_runs_view_sql(schema, site_code),
              _site_equipment_view_sql(schema, site_code)]
    return "".join(lines)


def _site_equipment_view_sql(schema, site_code):
    """The site's equipment registry (mes_equipment, seed_mes_equipment): the
    line, then the machine's id, name, type and status. No site column: the
    schema is the site. Mirrors agents/demo/site_views.py (pinned by
    tests/unit/test_admin_site_views.py)."""
    return (f"CREATE OR REPLACE VIEW {schema}.equipment AS SELECT "
            f"line, equipment_id, equipment_name, equipment_type, status "
            f"FROM public.mes_equipment WHERE site = '{site_code}';\n")  # nosec B608 - constants from this module


def _site_runs_view_sql(schema, site_code):
    """The ERP production order the MES pulls, for the runs this site's work
    orders execute: the run id (named as the work order's production_run_id),
    the run number the ERP records as shipment_items.batch_id, then sku,
    planned quantity, status, actual times and line (appended only, so
    CREATE OR REPLACE VIEW succeeds over the original 2-column view). Mirrors
    agents/demo/site_views.py (pinned by tests/unit/test_admin_site_views.py)."""
    return (f"CREATE OR REPLACE VIEW {schema}.production_runs AS SELECT "
            f"pr.run_id AS production_run_id, pr.run_number, p.sku, pr.planned_quantity, pr.status, "
            f"pr.actual_start_time, pr.actual_end_time, pr.line FROM public.production_runs pr "
            f"JOIN public.products p ON p.product_id = pr.product_id "
            f"WHERE pr.run_id IN (SELECT w.production_run_id FROM public.mes_work_orders w "
            f"WHERE w.site = '{site_code}');\n")  # nosec B608 - constants from this module


def _customer_categories_view_sql():
    """customer_product_categories: segment -> allowed categories (SEGMENT_CATEGORIES)."""
    def _array(categories):
        return "ARRAY[" + ", ".join(f"'{c}'" for c in categories) + "]"
    whens = "".join(f"        WHEN '{segment}' THEN {_array(categories)}\n"
                    for segment, categories in SEGMENT_CATEGORIES.items())
    return (
        "CREATE OR REPLACE VIEW customer_product_categories AS\n"
        "SELECT c.customer_id, c.customer_code, c.company_name, c.customer_segment,\n"
        "    CASE c.customer_segment\n"
        f"{whens}"
        f"        ELSE {_array(SEGMENT_CATEGORIES['BEVERAGE'])}\n"
        "    END AS allowed_categories\n"
        "FROM customers c WHERE c.is_active = TRUE;\n"  # nosec B608 - constants from this module, never input
    )


def get_views_sql():
    """Return SQL to create view-based schemas for demo onboarding."""
    blocks = [_erp_data_views_sql()]
    blocks += [_site_mes_views_sql(*site) for site in _LEGACY_SITE_MES_VIEWS]
    blocks.append(_customer_categories_view_sql())
    return "\n" + "\n".join(blocks)
