# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""demo Database Seed Lambda — runs SQL files from bundled postgres/ directory.

It is the ``onEvent`` function of the custom resources Provider behind
``AdtSeedResource`` (stacks/adt_seed_data_stack.py), and the app's Reset All
(``POST /api/onboard/reseed``, agents/onboard.py) invokes it directly. A seed
file that fails is RAISED (SeedLoadError), not returned: the Provider reports
the custom resource FAILED only when its handler raises and treats any
returned value as success, so a raise is what makes a Create fail the stack
instead of reporting CREATE_COMPLETE over missing or partial story data.
"""
import json
import os
import glob
import boto3

# The function's environment variable holding the Version of the SQL it
# ships: the stack sets it to adt_seed_version(), the value it also gives the
# custom resource as Version (stacks/adt_seed_data_stack.py). Unset or empty
# (a function deployed before the stack set it), every Update runs, as before.
SEED_VERSION_ENV = "ADT_SEED_VERSION"


class SeedLoadError(RuntimeError):
    """A seed run in which a SQL file failed (or none was found). Raised to
    the Provider, which reports the custom resource FAILED with this
    message."""


# The most a failure message may take of the custom resource response.
# CloudFormation refuses a response body over 4096 bytes. The Provider sends
# FAILED as the JSON {Status, Reason, StackId, RequestId, PhysicalResourceId,
# LogicalResourceId}, its Reason being "Error: <this message>\n\nLogs:
# /aws/lambda/<function>\n\n" and then at most 10 Node.js stack frames of the
# framework's own code (the db-init fail-loud change ran that framework,
# aws-cdk-lib 2.261.0, on node and measured them:
# erp_simulation/tests/test_db_init_reset_failures.py, the same 1024-byte
# limit in lambda-db-init/handler.py). With the longest identifiers
# CloudFormation allows and 10 frames of twice the longest it measured, the
# rest of that body is 2801 bytes (tests/test_adt_seed_handler.py builds it),
# and 2801 + 1024 < 4096.
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


def get_db_credentials():
    client = boto3.client("secretsmanager")
    secret = json.loads(
        client.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"]
    )
    return secret["username"], secret["password"]


def get_connection():
    """An autocommit connection to the seed database: each file then runs as
    its own implicit transaction (a file that fails leaves none of its
    statements; tests/test_adt_seed_handler.py measures it)."""
    # Imported here, not at module import: the app's local seeder
    # (agentic_digital_thread/tests/integration/seed_pg.py) loads this file
    # by path for apply_seed_files, where the layer is not importable.
    import pgdriver  # from the function's layer

    user, password = get_db_credentials()
    conn = pgdriver.connect(
        host=os.environ["DB_HOST"],
        port=5432,
        dbname=os.environ["DB_NAME"],
        user=user,
        password=password,
    )
    conn.autocommit = True
    return conn


def apply_seed_files(cur, sql_files):
    """Run every file whole, in the given order. A file that fails is logged
    and the rest still run (each is re-runnable and commits on its own, so
    everything that can land does, and one run names every failing file);
    then SeedLoadError names the failed files and the first error.
    tests/integration/seed_pg.py applies its files through this too."""
    failed = []
    for sql_file in sql_files:
        fname = os.path.basename(sql_file)
        print(f"Executing {fname}...")
        try:
            with open(sql_file, 'r', encoding="utf-8") as f:
                sql = f.read()
            cur.execute(sql)
            print(f"  {fname} executed successfully")
        except Exception as e:
            print(f"  ERROR in {fname}: {e}")
            failed.append((fname, f"{type(e).__name__}: {str(e).strip()}"))
    if failed:
        first_name, first_error = failed[0]
        message = (f"Demo seed failed: {len(failed)} of {len(sql_files)} SQL files failed: "
                   f"{', '.join(name for name, _ in failed)}. First error, in {first_name}: {first_error}")
        # The log keeps the whole message; the raised one is cut to fit the
        # custom resource response (_bounded).
        print(message)
        raise SeedLoadError(_bounded(message))


def update_is_for_this_seed(event):
    """True unless the event is a CloudFormation Update (it has a StackId)
    asking for a Version other than this code's ADT_SEED_VERSION.

    False for the Update CloudFormation sends while rolling back a failed
    Update: it carries the PREVIOUS Version, and the function still ships the
    new SQL files, which would fail the same way and leave the stack in
    UPDATE_ROLLBACK_FAILED. True when ADT_SEED_VERSION is unset or empty (the
    old behaviour), and for the Reset All invoke (no StackId, no Version),
    which always re-runs every file."""
    running = os.environ.get(SEED_VERSION_ENV)
    requested = (event.get("ResourceProperties") or {}).get("Version")
    if event.get("RequestType") != "Update" or not running or not requested or not event.get("StackId"):
        return True
    return requested == running


def handler(event, context):
    print(f"Event: {json.dumps(event)}")
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "vf-seed")}

    if not update_is_for_this_seed(event):
        # Nothing opened or changed; reported as success (the Provider sends
        # SUCCESS for a return). The rows the failed forward Update wrote stay.
        requested = event["ResourceProperties"]["Version"]
        print(f"Seed skipped: the update asks for Version {requested!r} "
              f"(this code: {os.environ.get(SEED_VERSION_ENV)!r}), "
              "an update rollback reaching the new code; nothing changed")
        return {"PhysicalResourceId": "vf-seed", "Data": {"Status": "Skipped"}}

    # Run all SQL files in order
    sql_dir = os.path.join(os.environ.get('LAMBDA_TASK_ROOT', '/var/task'))
    sql_files = sorted(glob.glob(os.path.join(sql_dir, '*.sql')))
    print(f"Found {len(sql_files)} SQL files")
    if not sql_files:
        message = f"Demo seed failed: no SQL files in {sql_dir}"
        print(message)
        raise SeedLoadError(_bounded(message))

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            apply_seed_files(cur, sql_files)
        finally:
            cur.close()
    finally:
        conn.close()
    print("demo seed data completed")
    return {"PhysicalResourceId": "vf-seed", "Data": {"Status": "Complete"}}
