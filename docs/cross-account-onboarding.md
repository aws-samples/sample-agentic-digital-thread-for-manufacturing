<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Onboard read-only data from another AWS account

This guide shows how an ADT deployment running in the **app** account onboards
**real** data from a separate **data** account by assuming a least-privilege,
**read-only** AWS Identity and Access Management (IAM) role through AWS Security
Token Service (AWS STS). The pattern is generic—nothing here is hardcoded into the
application; the role ARN and external ID are supplied as onboarding connection
config, and the app-side `sts:AssumeRole` grant is a deploy-time context value.

> **Note: the external ID is not a secret.** The IAM User Guide says
> ["AWS does not treat the external ID as a secret"](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html):
> anyone allowed to view the role can see it. Its job is to bind each
> AssumeRole call to the right customer (confused-deputy protection), so use a
> value that is unique per customer and pass it on every AssumeRole call. Keeping
> it out of logs, tickets, chat and source control is good hygiene, but it is not
> the access-control boundary: the role's trust policy is. To change it, redeploy
> the template with a new value. AWS CloudFormation stores the parameter with
> `NoEcho`, and the app does not log it (`agents/aws_session.py`).

---

## Architecture

The app account runs ADT on Amazon Elastic Container Service (Amazon ECS) and
Amazon Bedrock AgentCore Runtime. The data account's role reads Amazon Athena,
the AWS Glue Data Catalog, Amazon S3 Tables (governed by AWS Lake Formation), the
Amazon Simple Storage Service (Amazon S3) results bucket and AWS IoT SiteWise.

```
   app account (111122223333)                     data account (444455556666)
   ───────────────────────────                     ────────────────────────────────
   ADT ECS task role (AdtTaskRole)                   IAM role
   ADT AgentCore runtime role   ── sts:AssumeRole ─▶ ADTCrossAccountDataReadOnly
        │  (ExternalId required)                        │ (read-only managed policies)
        │                                               ├─ Athena  (workgroup <ATHENA_WORKGROUP>)
        ▼                                               ├─ Glue    (catalog metadata)
   temporary credentials ───── run queries / reads ─────────▶ ├─ S3 Tables (federated, read)
                                                        ├─ Lake Formation GetDataAccess
                                                        ├─ S3      (Athena results bucket)
                                                        └─ IoT SiteWise (asset reads)
                                                              │
                                        federated aws:s3tables │ (Lake Formation governed)
                                                              ▼
                                             S3 Tables lake — account <S3_TABLES_ACCOUNT_ID>
                                             (s3tablescatalog/<S3_TABLE_BUCKET>,
                                              db <S3_TABLES_NAMESPACE> — S3-Tables-account)
```

The app-side role assumes `ADTCrossAccountDataReadOnly` in the data account
(passing the `ExternalId`), receives temporary read-only credentials, and uses
them to run Amazon Athena queries and read AWS IoT SiteWise assets. The ADT app's
shared credential helper (`agentic_digital_thread/agents/aws_session.py`) performs the
AssumeRole transparently and refreshes the temporary credentials on expiry.

---

## Create the data-account role

- **Role ARN:** `arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly`
- **Account and Region:** data account `444455556666` in AWS Region `us-east-1`
- **Trust:** app account principal `arn:aws:iam::111122223333:root`, action
  `sts:AssumeRole`, and condition `StringEquals sts:ExternalId`
- **External ID:** set at deployment time and rotate by redeploying
- **CloudFormation template:**
  [`stacks/cross-account/adt-data-readonly-role.yaml`](../stacks/cross-account/adt-data-readonly-role.yaml)
- **Maximum session:** 1 hour (`MaxSessionDuration: 3600`)

### Deploy or update the role

```bash
# From the repo root, into the data account. Always confirm the account first:
aws sts get-caller-identity --profile <DATA_PROFILE>   # MUST be 444455556666

aws cloudformation deploy \
  --template-file stacks/cross-account/adt-data-readonly-role.yaml \
  --stack-name ADTCrossAccountDataReadOnly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    TrustedAccountId=111122223333 \
    DataAccountId=444455556666 \
    S3TablesAccountId='<S3_TABLES_ACCOUNT_ID>' \
    AthenaWorkgroup='<ATHENA_WORKGROUP>' \
    AthenaResultsBucket='amzn-s3-demo-athena-results' \
    GlueDatabaseName='<GLUE_DATABASE>' \
    S3TablesCatalogPath='s3tablescatalog/<S3_TABLE_BUCKET>' \
    S3TablesDatabaseName='<S3_TABLES_NAMESPACE>' \
    ExternalId='<EXTERNAL_ID>' \
  --profile <DATA_PROFILE> --region us-east-1
```

The template is parameterized for the trusted account, external ID, Athena
workgroup, results bucket, AWS Glue database, Amazon S3 Tables account, and one plain
Amazon S3 data bucket. Supply the
values for the target environment when deploying the role. `TrustedAccountId`
(the ADT account) and `ExternalId` are required and have no default.
`DataAccountId` defaults to the account you deploy the role in, which is the
data account; setting it to that account is the same as leaving it out. Set
`S3TablesAccountId`, `S3TablesCatalogPath` and `S3TablesDatabaseName` only when
your tables are in Amazon S3 Tables. `S3TablesAccountId` is the account that owns
the S3 Tables catalog: this account's ID when the tables are here, another
account in the example above. Left empty, the role gets no S3 Tables access (the
template leaves out its `S3TablesRead` policy). With it set, the role reads that
account's AWS Glue S3 Tables catalog; Athena then reads the tables through Lake
Formation (see [Lake Formation grant](#lake-formation-grant-required-for-the-federated-athena-path)).
Set `S3TableBucketName` as well only when the S3 Tables catalog uses IAM access
control instead of Lake Formation: it names the one table bucket (the bucket in
`S3TablesCatalogPath`), and the role then gets the `s3tables:` reads Athena uses
on that bucket and its tables (`GetTableBucket`, `GetNamespace`,
`ListNamespaces`, `ListTables`, `GetTable`, `GetTableData`), never `bucket/*`. Left
empty, the role gets no `s3tables:` permission.

For tables over a plain Amazon S3 bucket, set `DataBucketName` to that bucket's
name (for example `DataBucketName='amzn-s3-demo-bucket'`) instead of editing the
role: the role then reads that bucket and its objects (`s3:GetObject`,
`s3:ListBucket`, `s3:GetBucketLocation`) and nothing else. Left empty, the role gets
no data-bucket access (the template leaves out its `DataBucketRead` policy). The
parameter takes one bucket name, never a wildcard or a prefix; more buckets or
prefixes still mean adding statements to the role by hand. A bucket encrypted with
a customer managed AWS KMS key needs two more grants, both added by hand: the role
needs `kms:Decrypt` on that key, and the key policy must allow the role (or
delegate to IAM policies in the key's account). Grant only `kms:Decrypt`; keep
encrypt and other write actions out of this read-only role.

### Updating an existing role stack

Read this before you redeploy the template over a role stack that already exists.

- **Omitted parameters keep their previous values.** `aws cloudformation deploy`
  sends every existing parameter you leave out of `--parameter-overrides` as
  `UsePreviousValue`, so a stack keeps its stored `DataAccountId`,
  `S3TablesAccountId` and other values unless you pass new ones. A parameter
  the stack did not have before, such as `DataBucketName` or
  `S3TableBucketName`, takes its default (empty: no grant). To see what the
  stack holds now, run
  `aws cloudformation describe-stacks --stack-name ADTCrossAccountDataReadOnly --query 'Stacks[0].Parameters'`.
- **Check for hand edits first.** Run
  `aws cloudformation detect-stack-drift --stack-name ADTCrossAccountDataReadOnly`,
  then `aws cloudformation describe-stack-resource-drifts --stack-name ADTCrossAccountDataReadOnly`
  once detection completes. A drifted `CrossAccountDataReadOnlyRole` can mean a
  change to its trust policy, maximum session duration or description, and a
  drifted `*Policy` resource means a hand edit to one of the template's managed
  policies. Move those edits into a parameter or a separately managed policy
  (see the next bullets) before you update.
- **A hand-added statement in a template-owned policy is replaced on update.**
  The update rewrites the template's managed policies (`AthenaReadExecutePolicy`,
  `GlueCatalogReadPolicy` and the others in the table below) from the template.
  If you added a data-bucket statement by hand, set
  `DataBucketName=<that bucket>` in the same change set, or Athena loses access
  to it.
- **Stacks deployed before the policies became managed policies.** Earlier
  versions of this template held the same statements as inline policies on the
  role. An update creates the managed policies, attaches them to the role and
  removes the inline policies, with the same permissions. Review the change set
  (last bullet) before you execute it.
- **An S3 Tables catalog that uses IAM access control needs `S3TableBucketName`
  in the same change set.** Earlier versions of this template granted the
  `s3tables:` reads whenever `S3TablesAccountId` was set. This version grants
  them only when `S3TableBucketName` is set too, so an update that leaves it out
  removes them and Athena loses access to the tables. Set it to the bucket named
  in `S3TablesCatalogPath`. If the catalog uses Lake Formation, leave it empty.
- **Keep custom grants out of the template's policies.** Put multi-bucket,
  prefix-scoped or other custom grants in a separately managed policy (for
  example a customer managed policy of your own attached to the role), not in a
  policy the template owns.
- **Review the change set before you execute it.** Run the same
  `aws cloudformation deploy` command with `--no-execute-changeset`, inspect the
  change set it creates (`aws cloudformation describe-change-set`), then
  execute it.

---

## Scope the least-privilege policy

Configure the policy for the target environment:

- **Athena workgroup** `<ATHENA_WORKGROUP>`—configure its results location,
  for example `s3://amzn-s3-demo-athena-results/results/`, and review its bytes-
  scanned cutoff and workgroup-enforcement settings.
- **Data catalog** `AwsDataCatalog` (type `GLUE`) in account `444455556666`.
- **Database** `<GLUE_DATABASE>` is a Glue **federated** database:
  - `FederatedDatabase.Identifier` = `arn:aws:s3tables:us-east-1:<S3_TABLES_ACCOUNT_ID>:bucket/*`
  - `ConnectionType` = `aws:s3tables`
  - `TargetDatabase` = `<S3_TABLES_ACCOUNT_ID>:s3tablescatalog/<S3_TABLE_BUCKET>` / `<S3_TABLES_NAMESPACE>`
  - `ownerAccountId` = `<S3_TABLES_ACCOUNT_ID>` (the S3 Tables data-lake account)
  - Table `<TABLE_NAME>` represents an Apache Iceberg table in that namespace.
- **Governance:** if AWS Lake Formation governs `<GLUE_DATABASE>`, grant the
  assumed role the required `DESCRIBE` and `SELECT` permissions as described
  below.

### Least-privilege policy rationale

| Statement | Actions | Scope | Why |
|---|---|---|---|
| `AthenaReadExecute` | `StartQueryExecution`/`StopQueryExecution`/`GetQueryExecution`/`GetQueryResults`/`GetQueryResultsStream`/`GetWorkGroup`/`ListQueryExecutions` | `workgroup/<ATHENA_WORKGROUP>` | Run + read queries in the one workgroup only. |
| `AthenaCatalogMetadataRead` | `GetDataCatalog`/`ListDatabases`/`GetDatabase`/`ListTableMetadata`/`GetTableMetadata` | `datacatalog/AwsDataCatalog` | Catalog browsing during onboarding discovery. |
| `AthenaListDataCatalogs` | `ListDataCatalogs` | `*` | No resource-level support. |
| `GlueCatalogRead` | `GetDatabase*`/`GetTable*`/`GetPartition*`/`BatchGetPartition`/`GetCatalog*`/`SearchTables` | data-account catalog + `database/<GLUE_DATABASE>` (+ `table/.../*`) | Read-only Glue metadata in the data account. |
| `S3TablesRead` (only when `S3TablesAccountId` is set) | the same Glue reads; plus, only when `S3TableBucketName` is set, `s3tables:GetTableBucket`/`GetNamespace`/`ListNamespaces`/`ListTables`/`GetTable`/`GetTableData` | the owning (S3-Tables-account) account catalog `<S3_TABLES_ACCOUNT_ID>` (top-level `catalog`, `catalog/s3tablescatalog/<S3_TABLE_BUCKET>`, its `database/.../*` + `table/.../*/*`, and the bare `<S3_TABLES_NAMESPACE>` forms); the s3tables reads on `arn:aws:s3tables:us-east-1:<S3_TABLES_ACCOUNT_ID>:bucket/<S3_TABLE_BUCKET>` (+ `/table/*`) only | The federated database's `glue:GetTables` resolves against the **owning** account catalog, not the data-account catalog, so that catalog must be in scope. Under Lake Formation access control Athena reads the tables through Lake Formation credential vending, so no s3tables permission is granted. Under IAM access control the Athena User Guide requires s3tables permissions on the calling role; `S3TableBucketName` grants only the reads on that one table bucket. With `S3TablesAccountId` empty the template leaves this policy out. |
| `LakeFormationDataAccess` | `lakeformation:GetDataAccess` | `*` | LF credential vending for governed/federated reads (no resource-level support). **This is the path Athena actually uses** to read the federated table data. |
| `DataBucketRead` (only when `DataBucketName` is set) | `GetObject`/`ListBucket`/`GetBucketLocation` | `arn:aws:s3:::<DATA_BUCKET>` (+ `/*`) | Read-only access to the one plain S3 bucket the tables are over. With `DataBucketName` empty the template leaves this policy out. |
| `S3ResultsReadWrite` | `GetObject`/**`PutObject`**/**`AbortMultipartUpload`**/`GetBucketLocation`/`ListBucket`/multipart list | `amzn-s3-demo-athena-results` (+ `/*`) | Read **and write** Athena result objects. Athena requires the executing role to **write** query output to the workgroup's results bucket (pre-checked before planning). This is a write to the **scratch/results bucket only**—no data SOURCE is mutated; the role stays read-only over all data. |
| `SiteWiseRead` | `Describe*`/`List*`/`GetAssetPropertyValue`/`GetAssetPropertyValueHistory`/`GetAssetPropertyAggregates`/`BatchGetAssetPropertyValue*` | `*` | SiteWise asset-discovery/read actions have no resource-level support. |

No `*:*`, no admin, and no write or delete on any data source; the only writes
are Athena query output to the results bucket (`S3ResultsReadWrite`). The three
unavoidable `*` resources (`ListDataCatalogs`, `GetDataAccess`, SiteWise reads)
are actions AWS does not support resource-scoping for, each annotated in the
template.

---

## Lake Formation grant (required for the federated Athena path)

Because `<GLUE_DATABASE>` is **Lake Formation governed** and the table
data is a **federated S3 Tables** source owned by a **third** account
(`<S3_TABLES_ACCOUNT_ID>`), the IAM policy in this template is **necessary but not
sufficient** to read table *data*. One additional grant in the **data account**
is required—grant the assumed role `DESCRIBE`/`SELECT` on the database and
tables:

```bash
aws lakeformation grant-permissions --profile <DATA_PROFILE> --region us-east-1 \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly \
  --resource '{"Database":{"CatalogId":"444455556666","Name":"<GLUE_DATABASE>"}}' \
  --permissions DESCRIBE
aws lakeformation grant-permissions --profile <DATA_PROFILE> --region us-east-1 \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly \
  --resource '{"Table":{"CatalogId":"444455556666","DatabaseName":"<GLUE_DATABASE>","TableWildcard":{}}}' \
  --permissions SELECT DESCRIBE
```

With this grant plus the template's IAM policy, Athena can read the federated
table without an additional owner-account change. The Athena SQL path reads the federated Iceberg data through **Lake Formation
credential vending** (`lakeformation:GetDataAccess` plus the grants above),
**not** through the role's direct s3tables identity permissions. Two template
details make this work:

1. **Glue catalog scope (`GlueCatalogRead` and `S3TablesRead`).** The federated
   database's Glue metadata (`glue:GetTables`/`GetDatabase`/…) resolves against
   the **owning** (S3-Tables-account) account catalog `<S3_TABLES_ACCOUNT_ID>`, not
   the data-account catalog. The template therefore scopes read-only Glue to both
   accounts: the data-account catalog (`GlueCatalogRead`) plus, in `S3TablesRead`,
   the owning account's top-level `catalog`, its
   `catalog/s3tablescatalog/<S3_TABLE_BUCKET>` sub-catalog, and that
   bucket's `database/.../*` + `table/.../*/*` (plus the bare
   `<S3_TABLES_NAMESPACE>` forms).
2. **Athena results-bucket write (`S3ResultsReadWrite`).** Athena requires the
   **executing** role to **write** query output to the workgroup's results
   location and pre-checks that write before planning, so read-only access to
   the results bucket is insufficient. The template grants `s3:PutObject` +
   `s3:AbortMultipartUpload` scoped to the results bucket only; no data source
   is mutated.

**Direct `s3tables:*` API calls are a separate path.** Without an owner-side
S3 Tables share, direct s3tables calls (or a direct Glue Iceberg-REST /
Iceberg client such as PyIceberg or Spark) are denied by the **resource-based**
policy in the owning account. ADT's onboarding does **not** use that path, so
this does not block onboarding; if direct API access is ever needed, the exact
owner-side ask is documented in the next section.

**IoT SiteWise needs no extra grants.** Cross-account SiteWise reads work
through the assumed role's identity policy alone (`iotsitewise:Describe*`/
`List*` plus the asset-property reads)—no resource policy or Lake Formation
grant in any other account is involved.

### Verify with a smoke test

```bash
# Assume + smoke-test (from the app side, or any principal the trust allows):
CREDS=$(aws sts assume-role \
  --role-arn arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly \
  --role-session-name adt-xacct-test \
  --external-id '<EXTERNAL_ID>' --profile <APP_PROFILE> --region us-east-1 \
  --query Credentials --output json)
# export AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN from $CREDS, then:
aws athena start-query-execution --work-group <ATHENA_WORKGROUP> \
  --query-string 'SELECT <COLUMN_NAME> FROM <GLUE_DATABASE>.<TABLE_NAME> LIMIT 5' \
  --region us-east-1
```

If metadata lists but `SELECT` returns `AccessDenied`/`s3tables`/Lake Formation
errors, confirm the Lake Formation grant above is applied to the role. Widen
Lake Formation grants when needed—never IAM `*`.

---

## Configure the S3 Tables owner account for direct API access (optional)

> **Not required for ADT onboarding.** The Athena SQL path and SiteWise both
> work with no action from the data-lake owner. Only request the grants below
> if a consumer needs to call the `s3tables:*` API **directly** (or use the
> Glue Iceberg REST endpoint / a direct Iceberg client such as PyIceberg/Spark)
> against `<S3_TABLE_BUCKET>`—a path ADT does not use. Use the following
> steps only when that direct access is required.

The owner (`<S3_TABLES_ACCOUNT_ID>`) must configure an S3 Tables share that allows
`arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly` (or, more broadly,
account `444455556666`) to read namespaces/tables in bucket `<S3_TABLE_BUCKET>`.
S3 Tables cross-account access is evaluated against **both** a resource-based
policy **and** (when LF-integrated) Lake Formation grants—so both are needed.

**1. S3 Tables table-bucket resource policy** (run by the owner in `<S3_TABLES_ACCOUNT_ID>`):

```bash
# Owner account <S3_TABLES_ACCOUNT_ID>, us-east-1. Confirm identity first:
aws sts get-caller-identity   # MUST be <S3_TABLES_ACCOUNT_ID>

cat > /tmp/table-bucket-policy.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowADTReadOnlyRoleReadS3Tables",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly"
      },
      "Action": [
        "s3tables:GetTableBucket",
        "s3tables:ListNamespaces",
        "s3tables:GetNamespace",
        "s3tables:ListTables",
        "s3tables:GetTable",
        "s3tables:GetTableData",
        "s3tables:GetTableMetadataLocation"
      ],
      "Resource": [
        "arn:aws:s3tables:us-east-1:<S3_TABLES_ACCOUNT_ID>:bucket/<S3_TABLE_BUCKET>",
        "arn:aws:s3tables:us-east-1:<S3_TABLES_ACCOUNT_ID>:bucket/<S3_TABLE_BUCKET>/table/*"
      ]
    }
  ]
}
JSON

aws s3tables put-table-bucket-policy \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<S3_TABLES_ACCOUNT_ID>:bucket/<S3_TABLE_BUCKET> \
  --resource-policy file:///tmp/table-bucket-policy.json \
  --region us-east-1
```

> `put-table-bucket-policy` **replaces** the whole bucket policy. If a policy
> already exists, the owner must `get-table-bucket-policy` first and **merge**
> this statement in (do not clobber the existing statements).

**2. Lake Formation cross-account grant** (owner `<S3_TABLES_ACCOUNT_ID>`, on the
`s3tablescatalog/<S3_TABLE_BUCKET>` catalog's `<S3_TABLES_NAMESPACE>` database +
tables) to the consuming account/role. Grant with `--grant-option` omitted
(direct grant to the external principal):

```bash
# Database-level DESCRIBE
aws lakeformation grant-permissions --region us-east-1 \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly \
  --resource '{"Database":{"CatalogId":"<S3_TABLES_ACCOUNT_ID>:s3tablescatalog/<S3_TABLE_BUCKET>","Name":"<S3_TABLES_NAMESPACE>"}}' \
  --permissions DESCRIBE
# Table-level SELECT + DESCRIBE (all tables in the namespace)
aws lakeformation grant-permissions --region us-east-1 \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly \
  --resource '{"Table":{"CatalogId":"<S3_TABLES_ACCOUNT_ID>:s3tablescatalog/<S3_TABLE_BUCKET>","DatabaseName":"<S3_TABLES_NAMESPACE>","TableWildcard":{}}}' \
  --permissions SELECT DESCRIBE
```

(If the owner prefers account-level sharing, grant to
`DataLakePrincipalIdentifier=444455556666` and then the data account's own LF
admin re-grants to the specific role—the RAM/LF cross-account resource share
must also be accepted in the data account.)

**Verify direct API access** from the app side by using the assumed role:

```bash
# Assume the role (app profile + ExternalId), export creds, then:
aws s3tables list-namespaces \
  --table-bucket-arn arn:aws:s3tables:us-east-1:<S3_TABLES_ACCOUNT_ID>:bucket/<S3_TABLE_BUCKET> \
  --region us-east-1
# BEFORE the owner grant: AccessDeniedException "no resource-based policy allows
#   s3tables:ListNamespaces".
# AFTER: returns the namespaces (incl. <S3_TABLES_NAMESPACE>) — direct s3tables
#   API access now works.
```

---

## Onboarding connection config (what to enter in the ADT UI)

The ADT app supports cross-account access on any AWS-native connector through
three optional keys on the `connection_config` (`role_arn`, `external_id`,
`region`), resolved by `agents/aws_session.py`. `external_id` is routed through
the secret path (never stored cleartext on the graph node); `role_arn`/`region`
are stored as node config.

**Athena source:**
```json
{
  "workgroup":   "<ATHENA_WORKGROUP>",
  "database":    "<GLUE_DATABASE>",
  "catalog":     "AwsDataCatalog",
  "region":      "us-east-1",
  "role_arn":    "arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly",
  "external_id": "<EXTERNAL_ID>"
}
```

**SiteWise (Direct) source:**
```json
{
  "connection_string": "iotsitewise://iotsitewise.us-east-1.amazonaws.com",
  "region":            "us-east-1",
  "role_arn":          "arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly",
  "external_id":       "<EXTERNAL_ID>"
}
```

Omitting `role_arn` uses ambient credentials (the ADT task role)—the historical
same-account behavior, unchanged.

---

## Deploy the app-side assume-role grant

The ADT Amazon ECS task role (CloudFormation construct ID `AdtTaskRole`—a retained
pre-rename name; see the retained-names note in [`glossary.md`](glossary.md))
and the AgentCore runtime role
(`RuntimeRole`) need permission to `sts:AssumeRole` the data-account role. This is wired
as an **optional deploy-time context value**—`crossAccountDataRoleArn`—so the
ARN is **not hardcoded** and the grant is a **no-op when unset**:

```bash
cd agentic_digital_thread
./deploy.sh --profile <APP_PROFILE> --region us-east-1 \
  --seed \
  --cross-account-role arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly
```

(`deploy.sh` threads the flag through as `-c crossAccountDataRoleArn=...`; pass
that context value directly if you run `cdk deploy` yourself). When set, each role
gets a single scoped statement:

```json
{ "Effect": "Allow", "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly" }
```

Implementation: `stacks/adt_compute_stack.py` (AdtTaskRole) and
`stacks/agentcore_stack.py` (RuntimeRole) each read
`self.node.try_get_context("crossAccountDataRoleArn")` and add the statement only
when it is provided.

---

## Dynamic onboarding: what must exist ahead of time (deploy-once checklist)

ADT onboards systems **dynamically through the UI**—but cross-account access
has two one-time prerequisites that CANNOT be created at onboarding time. This
is deliberate: creating them at runtime would require the app to modify its own
IAM (a self-escalation anti-pattern). With the checklist below, everything
after the one-time setup is per-system form input—no further deploys.

| # | Who | When | What |
|---|-----|------|------|
| 1 | ADT operator | **Once ever** | Deploy with `--cross-account-role convention` (or an explicit ARN list—see the modes below). The value is **sticky** (persisted in SSM `/adt/cross-account-data-role-arn`), so later deploys keep the grant without repeating the flag. |
| 2 | Each data-account owner | **Once per data account** | Deploy the standard role template (`stacks/cross-account/adt-data-readonly-role.yaml`)—it pins trust to YOUR ADT account and requires the ExternalId. This step is irreducible: only the data account can grant trust into itself. |
| 3 | Anyone onboarding | **Per system—no infra** | Enter the role ARN + ExternalId (+ Athena/SiteWise details) in the onboarding UI. With `convention` mode, any number of systems across any number of trusting data accounts onboard with **zero further deploys**. |

**If prerequisite 1 is missing** (or the deployed grant doesn't cover the
role), discovery fails with an STS `AccessDenied` on the app side:

```
AssumeRole failed for role_arn=arn:aws:iam::<data-acct>:role/ADTCrossAccount...
... is not authorized to perform: sts:AssumeRole ...
```

(visible in the app's ECS logs). Fix: redeploy once with
`--cross-account-role convention` (or add the ARN to the explicit list). If
onboarding hangs at "Connecting…" with no visible error, check the app logs for
this signature first. The onboarding preflight now surfaces this failure as
an `assume_role_denied` `blocked` verdict **before** discovery runs—see
[`adr/0053-preflight-drift-tombstones-and-cost-honesty.md`](adr/0053-preflight-drift-tombstones-and-cost-honesty.md).

## Granting ADT permission to assume data-account roles

The `crossAccountDataRoleArn` context value (same key as above, backward
compatible) supports **three modes**, parsed by `stacks/_cross_account.py`:

| Value | Grant on the ADT task role + AgentCore runtime role | When to use |
|-------|-----------------------------------------------------|-------------|
| *(empty / absent)* | **No grant.** Byte-identical to a deployment that never configured cross-account data—the base sample stays wildcard-free. | Default. No cross-account data. |
| `arn:...,arn:...`—one or more **comma-separated role ARNs** | One `sts:AssumeRole` statement listing exactly those ARNs (a single ARN behaves exactly as before). | Stricter environments: every reachable data-account role is enumerated at deploy time; adding a data account requires a redeploy. |
| the literal keyword `convention` (case-insensitive) | One `sts:AssumeRole` statement on the naming-convention wildcard `arn:aws:iam::*:role/ADTCrossAccount*`. | Self-service onboarding: any data account that deploys the standard trust-pinned role template becomes onboardable **with no ADT redeploy**. |

Examples:

```bash
# Explicit list (no spaces around the commas):
./deploy.sh ... --cross-account-role \
  arn:aws:iam::444455556666:role/ADTCrossAccountDataReadOnly,arn:aws:iam::444455556666:role/ADTCrossAccountSiteWise

# Self-service convention mode:
./deploy.sh ... --cross-account-role convention
```

Malformed entries (anything that is not `arn:aws:iam::<account>:role/<name>`)
fail the synth with a `ValueError` rather than silently granting the wrong
resource.

**Security justification.** The caller-side grant is defense-in-depth, not the
primary access control. The real gate is in each **data account**: the target
role's **trust policy** is pinned to the app account, **and** it requires the
customer's `sts:ExternalId`. An assume attempt, even under the `convention`
wildcard, against a role that does not trust this account (or without the
correct ExternalId) fails at STS. `convention` mode therefore only widens which
*already-trusting* roles ADT may assume; it trades enumerated resource scoping
for self-service onboarding and is **opt-in only, never the default**. Prefer
the explicit ARN list in stricter environments, and combine either mode with
the trust-tightening step below.

---

## Tightening the trust (post-deploy hardening)

The template trusts the app **account root** (`:root`) at bootstrap so the app
can be deployed before the exact consuming role ARNs are known. Once the ADT app
is deployed, **tighten the trust** to the concrete deployed role ARNs (the
CloudFormation outputs `RuntimeRoleArn` and the ADT task role ARN):

1. Get the deployed ARNs from the app-side stacks (`ADTComputeStack` /
   `ADTAgentCoreStack` outputs, or `aws iam get-role`).
2. Redeploy the data-account template with a tightened trust—replace the `:root`
   principal with the explicit role ARNs. For example, edit the
   `AssumeRolePolicyDocument` principal to:
   ```yaml
   AWS:
     - arn:aws:iam::111122223333:role/<AdtTaskRole-physical-name>
     - arn:aws:iam::111122223333:role/<RuntimeRole-physical-name>
   ```
   (keep the `sts:ExternalId` condition). Account-root trust only *delegates*
   the decision to the app account's IAM—the assuming principal there still
   needs the `sts:AssumeRole` grant above—but scoping to the exact role ARNs is
   defense-in-depth and recommended for any non-throwaway use.

---

## Security notes

- **Read-only.** The role grants only read/execute actions; no create/update/
  delete anywhere.
- **Confused-deputy protection.** The `sts:ExternalId` condition means a
  caller that knows the role ARN but not this customer's external ID cannot
  assume the role.
- **ExternalId is unique per customer, not a secret.** AWS does not treat it as
  one ([IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html)).
  Give each customer its own value and pass it on every AssumeRole call. Keeping
  it out of logs and source control is good hygiene (this repository does not
  commit the value); the trust policy is what grants access.
- **Temporary credentials.** Sessions are 1 hour; the app refreshes
  automatically using `RefreshableCredentials`.
- **Per-user enforcement happens at the system agent, not the assumed role.**
  The cross-account data path always runs under the assumed role's service
  identity. The optional per-user identity claim (`SYSTEM_REQUIRED_GROUP`—see
  the README security table) is an allow/deny gate at the system-agent
  boundary, evaluated before any query runs; it never changes which IAM
  principal reads the data.
- **Demonstration sample.** As with the rest of this repo, review and adapt
  before any non-demonstration use.
