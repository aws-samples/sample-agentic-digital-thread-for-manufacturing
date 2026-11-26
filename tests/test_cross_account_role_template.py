# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The cross-account read-only role template's account and bucket parameters.

``stacks/cross-account/adt-data-readonly-role.yaml`` deploys INTO the data
account and scopes its trust policy and its Athena, AWS Glue and Amazon S3
Tables grants by three account IDs:

* ``TrustedAccountId`` (the ADT account) is required: no default, because a
  default would be a placeholder account the role trusts.
* ``DataAccountId`` defaults to ``''``, which resolves to the stack's own
  account (``AWS::AccountId``), the data account.
* ``S3TablesAccountId`` defaults to ``''``, which means NO S3 Tables access:
  no statement grants anything in S3 Tables or on the owning account's
  ``s3tablescatalog`` Glue paths. Set, those grants are scoped to that account.
  Resolving an empty value to the stack account instead would hand the role
  read access to every table bucket in the account it is deployed in.

No account parameter has a 12-digit (placeholder) default. Every documented
deploy command passes ``TrustedAccountId``, and every place that tells a
customer how to deploy the template names it.

Direct ``s3tables:`` reads (the IAM access-control mode of the S3 Tables
catalog) need ``S3TableBucketName`` as well: one table bucket, read-only
actions, scoped to that bucket and its tables, never ``bucket/*``. Empty (the
default, and the right value under Lake Formation access control) means no
``s3tables:`` grant at all.

``DataBucketName`` (one plain Amazon S3 bucket of source data) defaults to
``''``, which means no data-bucket access: the only plain S3 grant is then the
Athena query-results bucket. Set, the role reads exactly that bucket and its
objects, with read-only actions, never a wildcard bucket.

Offline: files are read as text. The template's short-form tags (``!Sub``,
``!Ref``, ``!If``, ...) are loaded as their long forms and the role's
properties are evaluated by a small resolver, so the checks read the policy
the role would actually get for given parameter values.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "stacks" / "cross-account" / "adt-data-readonly-role.yaml"
TEMPLATE_NAME = "adt-data-readonly-role.yaml"
ROLE = "CrossAccountDataReadOnlyRole"
REQUIRED = "TrustedAccountId"
ACCOUNT_PARAMETERS = (REQUIRED, "DataAccountId", "S3TablesAccountId")
STACK_ACCOUNT = "<AccountId>"  # how the resolver renders AWS::AccountId
OWNER_ACCOUNT = "444455556666"
TRUSTED = "111122223333"
DATA_BUCKET = "amzn-s3-demo-data-bucket"
RESULTS_BUCKET = "amzn-s3-demo-athena-results"
BUCKET_PARAMETER = "DataBucketName"
BUCKET_PATTERN = "^([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])?$"
#: What the data bucket may grant: Athena's source-data reads, nothing that writes.
DATA_BUCKET_READ_ACTIONS = {"s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"}
#: The statement that may touch the results bucket (it writes query output).
RESULTS_SID = "AthenaResultsBucketReadWrite"
#: The guides that tell a plain-S3 user what to set.
_BUCKET_GUIDES = ("docs/cross-account-onboarding.md", "docs/deployment.md", "docs/onboarding.md")
TABLE_BUCKET = "example-table-bucket"
TABLE_BUCKET_PARAMETER = "S3TableBucketName"
TABLE_BUCKET_PATTERN = "^([a-z0-9][a-z0-9-]{1,61}[a-z0-9])?$"
#: The s3tables reads Athena uses on a table (Athena User Guide, "Register S3
#: table bucket catalogs and query Tables from Athena", IAM option), read-only.
S3TABLES_READ_ACTIONS = {"s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:ListNamespaces",
                         "s3tables:ListTables", "s3tables:GetTable", "s3tables:GetTableData"}
#: Distinctive values, so an S3 Tables grant is recognisable in a resolved ARN.
S3_TABLES_VALUES = {"S3TablesCatalogPath": "s3tablescatalog/<TABLE_BUCKET>",
                    "S3TablesDatabaseName": "<S3_TABLES_DB>"}

#: Where deploy instructions for the template live (tests are not instructions).
_SURFACE_GLOBS = (
    ("docs", "**/*.md"),
    (".", "README.md"),
    ("agentic_digital_thread/agents", "**/*.py"),
    ("agentic_digital_thread/frontend/src", "**/*.ts"),
    ("agentic_digital_thread/frontend/src", "**/*.tsx"),
)
_SKIP_PARTS = {"node_modules", "test", "tests", "__pycache__", "dist"}
#: The help surfaces known today; the scan must find at least these.
_KNOWN_SURFACES = {
    "docs/deployment.md",
    "docs/onboarding.md",
    "docs/cross-account-onboarding.md",
    "agentic_digital_thread/frontend/src/views/onboard/components/ObModals.tsx",
    "agentic_digital_thread/agents/onboard_agent.py",
}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that turns CloudFormation short-form tags into long form."""


def _cfn_tag(loader, suffix, node):
    key = suffix if suffix in ("Ref", "Condition") else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {key: value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _template() -> dict:
    return yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)  # nosec B506 - SafeLoader subclass


_NO_VALUE = object()
_INTRINSICS = {"Ref", "Condition", "Fn::Equals", "Fn::Not", "Fn::If", "Fn::Sub"}


class _Resolver:
    """Evaluate the intrinsics the template uses, for given parameter values."""

    def __init__(self, template: dict, values: dict):
        self.conditions = template.get("Conditions") or {}
        defaults = {name: str(p.get("Default", f"<{name}>"))
                    for name, p in (template.get("Parameters") or {}).items()}
        self.values = {**defaults, **values}

    def ref(self, name: str):
        if name == "AWS::NoValue":
            return _NO_VALUE
        if name.startswith("AWS::"):
            return f"<{name[len('AWS::'):]}>"  # no ':' inside, so ARN fields split cleanly
        return self.values[name]

    def condition(self, name: str) -> bool:
        return bool(self.eval(self.conditions[name]))

    def sub(self, template: str, variables: dict) -> str:
        def one(match):
            name = match.group(1)
            return str(self.eval(variables[name]) if name in variables else self.ref(name))
        return re.sub(r"\$\{([^}!]+)\}", one, template)

    def eval(self, node):
        if isinstance(node, list):
            return [v for v in (self.eval(n) for n in node) if v is not _NO_VALUE]
        if not isinstance(node, dict):
            return node
        if len(node) == 1 and next(iter(node)) in _INTRINSICS:
            (key, arg), = node.items()
            if key == "Ref":
                return self.ref(arg)
            if key == "Condition":
                return self.condition(arg)
            if key == "Fn::Equals":
                return self.eval(arg[0]) == self.eval(arg[1])
            if key == "Fn::Not":
                return not self.eval(arg[0])
            if key == "Fn::If":
                return self.eval(arg[1] if self.condition(arg[0]) else arg[2])
            if isinstance(arg, str):
                return self.sub(arg, {})
            return self.sub(arg[0], arg[1])
        evaluated = {k: self.eval(v) for k, v in node.items()}
        return {k: v for k, v in evaluated.items() if v is not _NO_VALUE}


def _managed_policies(template: dict) -> dict:
    """The template's managed policies by logical id (``<PolicyName>Policy``)."""
    return {lid: r for lid, r in template["Resources"].items()
            if r.get("Type") == "AWS::IAM::ManagedPolicy"}


def _policy_nodes(template: dict) -> list:
    """The unresolved policy documents, for a negative control to edit in place."""
    return [r["Properties"]["PolicyDocument"] for r in _managed_policies(template).values()]


def _role_policies(template: dict, **values) -> list:
    """The role's policies, resolved for ``values`` (others: their Default):
    each managed policy whose ``Condition`` holds, as ``PolicyName`` (its
    logical id without the ``Policy`` suffix) and ``PolicyDocument``."""
    params = {REQUIRED: TRUSTED, **S3_TABLES_VALUES, **values}
    resolver = _Resolver(template, params)
    return [{"PolicyName": lid[:-len("Policy")],
             "PolicyDocument": resolver.eval(r["Properties"]["PolicyDocument"])}
            for lid, r in _managed_policies(template).items()
            if "Condition" not in r or resolver.condition(r["Condition"])]


def _statements(policies: list) -> list:
    return [(p["PolicyName"], s) for p in policies for s in p["PolicyDocument"].get("Statement", [])]


def _resources(statement: dict) -> list:
    resource = statement.get("Resource", [])
    return [resource] if isinstance(resource, str) else list(resource)


def _account(arn: str) -> str:
    return arn.split(":")[4]


def _is_s3_tables_grant(arn: str) -> bool:
    """An ARN in S3 Tables, or on the owning account's S3 Tables Glue catalog."""
    return (":s3tables:" in arn or "s3tablescatalog" in arn
            or S3_TABLES_VALUES["S3TablesDatabaseName"] in arn)


def _shape_problems(policies: list) -> list:
    """No policy without statements, no statement without resources, no ARN
    with an empty account field (an unguarded empty parameter)."""
    problems = [f"{p['PolicyName']}: empty Statement" for p in policies
                if not p["PolicyDocument"].get("Statement")]
    for name, st in _statements(policies):
        resources = _resources(st)
        if not resources:
            problems.append(f"{name}/{st.get('Sid')}: empty Resource")
        for arn in resources:
            if arn != "*" and ":s3:::" not in arn and not _account(arn):
                problems.append(f"{name}/{st.get('Sid')}: no account in {arn}")
    return problems


def _empty_s3_tables_problems(template: dict) -> list:
    """What the role grants in S3 Tables when ``S3TablesAccountId`` is empty."""
    policies = _role_policies(template, S3TablesAccountId="")
    grants = [f"{name}/{st.get('Sid')}: {arn}" for name, st in _statements(policies)
              for arn in _resources(st) if _is_s3_tables_grant(arn)]
    return grants + _shape_problems(policies)


def _actions(statement: dict) -> set:
    action = statement.get("Action", [])
    return {action} if isinstance(action, str) else set(action)


def _plain_s3_grants(policies: list) -> list:
    """(sid, actions, resource) for every grant on a plain S3 ARN, and every s3:
    action granted on ``*``."""
    grants = []
    for _, st in _statements(policies):
        actions = _actions(st)
        for arn in _resources(st):
            if ":s3:::" in arn or (arn == "*" and any(a.startswith("s3:") for a in actions)):
                grants.append((st.get("Sid"), actions, arn))
    return grants


def _bucket_arns(bucket: str) -> set:
    return {f"arn:<Partition>:s3:::{bucket}", f"arn:<Partition>:s3:::{bucket}/*"}


def _empty_data_bucket_problems(template: dict) -> list:
    """What the role grants on plain S3 besides the results bucket when
    ``DataBucketName`` is empty."""
    policies = _role_policies(template, DataBucketName="")
    problems = [f"{sid}: {arn}" for sid, _, arn in _plain_s3_grants(policies)
                if not (sid == RESULTS_SID and arn in _bucket_arns(RESULTS_BUCKET))]
    return problems + _shape_problems(policies)


def _set_data_bucket_problems(template: dict) -> list:
    """With ``DataBucketName`` set: the grants beyond the results statement are
    exactly that bucket and its objects, with read-only actions, and they
    include s3:GetObject and s3:ListBucket."""
    policies = _role_policies(template, DataBucketName=DATA_BUCKET)
    problems = list(_shape_problems(policies))
    data = [(sid, actions, arn) for sid, actions, arn in _plain_s3_grants(policies) if sid != RESULTS_SID]
    resources = {arn for _, _, arn in data}
    if resources != _bucket_arns(DATA_BUCKET):
        problems.append(f"data-bucket resources {sorted(resources)}")
    granted = set().union(*(actions for _, actions, _ in data)) if data else set()
    if granted - DATA_BUCKET_READ_ACTIONS:
        problems.append(f"not read-only: {sorted(granted - DATA_BUCKET_READ_ACTIONS)}")
    if not {"s3:GetObject", "s3:ListBucket"} <= granted:
        problems.append(f"missing reads: {sorted({'s3:GetObject', 's3:ListBucket'} - granted)}")
    for _, st in _statements(policies):
        if st.get("Sid") == RESULTS_SID and set(_resources(st)) != _bucket_arns(RESULTS_BUCKET):
            problems.append(f"{RESULTS_SID} resources {_resources(st)}")
    return problems


def _s3tables_grants(policies: list) -> list:
    """(sid, actions, resource) for every statement that grants an s3tables:
    action or names an s3tables ARN."""
    grants = []
    for _, st in _statements(policies):
        actions = _actions(st)
        for arn in _resources(st):
            if ":s3tables:" in arn or any(a.startswith("s3tables:") for a in actions):
                grants.append((st.get("Sid"), actions, arn))
    return grants


def _table_bucket_arns(account: str, bucket: str) -> set:
    base = f"arn:<Partition>:s3tables:<Region>:{account}:bucket/{bucket}"
    return {base, f"{base}/table/*"}


def _set_table_bucket_problems(template: dict) -> list:
    """With S3TablesAccountId and S3TableBucketName set: the s3tables grants are
    exactly that bucket and its tables, read-only, and include the table reads."""
    policies = _role_policies(template, S3TablesAccountId=OWNER_ACCOUNT, S3TableBucketName=TABLE_BUCKET)
    problems = list(_shape_problems(policies))
    grants = _s3tables_grants(policies)
    resources = {arn for _, _, arn in grants}
    if resources != _table_bucket_arns(OWNER_ACCOUNT, TABLE_BUCKET):
        problems.append(f"s3tables resources {sorted(resources)}")
    granted = set().union(*(a for _, a, _ in grants)) if grants else set()
    s3tables = {a for a in granted if a.startswith("s3tables:")}
    if s3tables - S3TABLES_READ_ACTIONS:
        problems.append(f"not a read Athena uses: {sorted(s3tables - S3TABLES_READ_ACTIONS)}")
    if not {"s3tables:GetTable", "s3tables:GetTableData"} <= s3tables:
        problems.append(f"missing table reads: {sorted({'s3tables:GetTable', 's3tables:GetTableData'} - s3tables)}")
    return problems


def _find_statement(node, sid: str):
    if isinstance(node, dict):
        if node.get("Sid") == sid:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            found = _find_statement(value, sid)
            if found is not None:
                return found
    return None


def _surface_files():
    for base, pattern in _SURFACE_GLOBS:
        for path in sorted((ROOT / base).glob(pattern)):
            rel = path.relative_to(ROOT)
            if path.is_file() and not (_SKIP_PARTS & set(rel.parts[:-1])) and ".test." not in path.name:
                yield rel.as_posix(), path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Policies: customer managed policies on the role, never inline
# --------------------------------------------------------------------------- #

_POLICY_FIXTURE = ROOT / "tests" / "fixtures" / "cross_account_role_policies.json"


def test_the_role_has_no_inline_policies():
    assert "Policies" not in _template()["Resources"][ROLE]["Properties"]


def test_each_managed_policy_attaches_to_exactly_the_role():
    policies = _managed_policies(_template())
    assert len(policies) == 7
    for lid, policy in policies.items():
        assert lid.endswith("Policy"), lid
        props = policy["Properties"]
        assert props["Roles"] == [{"Ref": ROLE}], lid
        assert not {"Users", "Groups"} & set(props), lid


def test_the_policies_grant_exactly_what_the_inline_policies_granted():
    """The fixture holds the inline policies of the template before the move,
    resolved for six parameter sets: same names, same statements."""
    import json

    fixture = json.loads(_POLICY_FIXTURE.read_text(encoding="utf-8"))
    template = _template()
    for case, values in fixture["parameters"].items():
        got = {p["PolicyName"]: p["PolicyDocument"] for p in _role_policies(template, **values)}
        assert got == fixture["policies"][case], case


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #

def test_the_account_parameters_are_the_account_id_ones():
    """Guard the guard: a new account-ID parameter must join ACCOUNT_PARAMETERS."""
    params = _template()["Parameters"]
    accounts = {name for name, p in params.items() if "[0-9]{12}" in str(p.get("AllowedPattern", ""))}
    assert accounts == set(ACCOUNT_PARAMETERS)


def test_no_account_parameter_has_a_twelve_digit_default():
    params = _template()["Parameters"]
    placeholders = {name: params[name]["Default"] for name in ACCOUNT_PARAMETERS
                    if re.fullmatch(r"[0-9]{12}", str(params[name].get("Default", "")))}
    assert placeholders == {}


def test_the_trusted_account_is_required():
    assert "Default" not in _template()["Parameters"][REQUIRED]


@pytest.mark.parametrize("name", ("DataAccountId", "S3TablesAccountId"))
def test_the_data_side_accounts_default_to_empty_and_accept_empty(name):
    param = _template()["Parameters"][name]
    assert param.get("Default") == ""
    assert param.get("AllowedPattern") == "^([0-9]{12})?$"


# --------------------------------------------------------------------------- #
# DataAccountId: empty means the stack account
# --------------------------------------------------------------------------- #

def test_an_empty_data_account_scopes_the_data_grants_to_the_stack_account():
    template = _template()
    for value, expected in (("", STACK_ACCOUNT), (OWNER_ACCOUNT, OWNER_ACCOUNT)):
        policies = _role_policies(template, DataAccountId=value)
        data_arns = [arn for name, st in _statements(policies)
                     if name in ("AthenaReadExecute", "GlueCatalogRead") for arn in _resources(st)
                     if arn != "*" and not _is_s3_tables_grant(arn)]
        assert data_arns, "no Athena/Glue data-account grants"
        assert {_account(a) for a in data_arns} == {expected}, (value, data_arns)


# --------------------------------------------------------------------------- #
# S3TablesAccountId: empty means no S3 Tables access
# --------------------------------------------------------------------------- #

def test_an_empty_s3_tables_account_grants_nothing_in_s3_tables():
    assert _empty_s3_tables_problems(_template()) == []


def test_a_set_s3_tables_account_scopes_every_s3_tables_grant_to_it():
    policies = _role_policies(_template(), S3TablesAccountId=OWNER_ACCOUNT, S3TableBucketName=TABLE_BUCKET)
    assert _shape_problems(policies) == []
    grants = [arn for _, st in _statements(policies) for arn in _resources(st) if _is_s3_tables_grant(arn)]
    assert any(":s3tables:" in a for a in grants), grants
    assert any(a.endswith(":catalog/s3tablescatalog") for a in grants), grants
    assert {_account(a) for a in grants} == {OWNER_ACCOUNT}, grants


def test_the_s3_tables_account_alone_grants_glue_catalog_reads_and_no_s3tables_action():
    """Lake Formation access control: the owning account's Glue catalog is
    readable, and no s3tables: action is granted on anything."""
    policies = _role_policies(_template(), S3TablesAccountId=OWNER_ACCOUNT, S3TableBucketName="")
    assert _shape_problems(policies) == []
    assert _s3tables_grants(policies) == []
    glue = [arn for _, st in _statements(policies) for arn in _resources(st) if "s3tablescatalog" in arn]
    assert glue and {_account(a) for a in glue} == {OWNER_ACCOUNT}, glue


def test_a_table_bucket_without_the_s3_tables_account_grants_nothing():
    policies = _role_policies(_template(), S3TablesAccountId="", S3TableBucketName=TABLE_BUCKET)
    assert _shape_problems(policies) == []
    assert _s3tables_grants(policies) == []


def test_the_table_bucket_defaults_to_empty_and_accepts_only_a_table_bucket_name():
    param = _template()["Parameters"][TABLE_BUCKET_PARAMETER]
    assert param.get("Default") == ""
    assert param.get("AllowedPattern") == TABLE_BUCKET_PATTERN
    pattern = re.compile(TABLE_BUCKET_PATTERN)
    for ok in ("", TABLE_BUCKET, "abc", "a1-b2"):
        assert pattern.fullmatch(ok), ok
    for bad in ("*", "example-*", "bucket/table", "with.dot", "under_score", "Upper", "ab", "-lead",
                "trail-", "x" * 64):
        assert not pattern.fullmatch(bad), bad


def test_a_set_table_bucket_grants_athena_reads_on_exactly_that_table_bucket():
    assert _set_table_bucket_problems(_template()) == []


def test_negative_control_a_wildcard_table_bucket_is_caught():
    template = copy.deepcopy(_template())
    statement = _find_statement(_policy_nodes(template), "S3TablesReadOnly")
    assert statement is not None, "no S3TablesReadOnly statement"
    statement["Resource"].append({"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${S3TablesAccountId}:bucket/*"})
    assert _set_table_bucket_problems(template) != []


def test_negative_control_an_s3tables_write_action_is_caught():
    template = copy.deepcopy(_template())
    statement = _find_statement(_policy_nodes(template), "S3TablesReadOnly")
    assert statement is not None, "no S3TablesReadOnly statement"
    statement["Action"].append("s3tables:PutTableData")
    assert _set_table_bucket_problems(template) != []


def test_an_empty_s3_tables_account_removes_exactly_the_owner_account_grants():
    """Empty = the set-value policy minus everything scoped to the owner
    account: nothing else changes, and nothing appears instead."""
    template = _template()

    def flat(policies):
        return {(name, st.get("Sid"), arn) for name, st in _statements(policies) for arn in _resources(st)}

    def in_owner_account(arn):
        fields = arn.split(":")
        return len(fields) > 4 and fields[4] == OWNER_ACCOUNT

    empty = flat(_role_policies(template, S3TablesAccountId=""))
    owned = flat(_role_policies(template, S3TablesAccountId=OWNER_ACCOUNT))
    assert empty == {g for g in owned if not in_owner_account(g[2])}


def test_negative_control_an_unconditioned_s3_tables_arn_is_caught():
    """Guard the guard: one S3 Tables ARN outside the condition must be reported."""
    template = copy.deepcopy(_template())
    statement = _find_statement(_policy_nodes(template), "GlueCatalogReadOnly")
    assert statement is not None
    statement["Resource"].append(
        {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${S3TablesAccountId}:bucket/*"})
    assert _empty_s3_tables_problems(template) != []


# --------------------------------------------------------------------------- #
# DataBucketName: empty means no data-bucket access
# --------------------------------------------------------------------------- #

def test_the_data_bucket_defaults_to_empty_and_accepts_only_a_bucket_name():
    param = _template()["Parameters"][BUCKET_PARAMETER]
    assert param.get("Default") == ""
    assert param.get("AllowedPattern") == BUCKET_PATTERN
    pattern = re.compile(BUCKET_PATTERN)
    for ok in ("", DATA_BUCKET, "my.data-bucket1", "abc"):
        assert pattern.fullmatch(ok), ok
    for bad in ("*", "amzn-s3-demo-*", "data-bucket/prefix", "Upper", "ab", "-lead", "trail-", "x" * 64,
                "arn:aws:s3:::data"):
        assert not pattern.fullmatch(bad), bad


def test_an_empty_data_bucket_grants_no_s3_data_read():
    """Empty: the only plain S3 grant is the Athena results-bucket statement."""
    assert _empty_data_bucket_problems(_template()) == []


def test_a_set_data_bucket_grants_read_only_on_exactly_that_bucket():
    assert _set_data_bucket_problems(_template()) == []


def test_an_empty_data_bucket_removes_exactly_the_data_bucket_grants():
    template = _template()

    def flat(policies):
        return {(name, st.get("Sid"), arn) for name, st in _statements(policies) for arn in _resources(st)}

    empty = flat(_role_policies(template, DataBucketName=""))
    full = flat(_role_policies(template, DataBucketName=DATA_BUCKET))
    assert empty == {g for g in full if g[2] not in _bucket_arns(DATA_BUCKET)}


def test_negative_control_a_wildcard_bucket_arn_is_caught():
    """Guard the guard: a wildcard bucket in the data-bucket statement must be reported."""
    template = copy.deepcopy(_template())
    statement = _find_statement(_policy_nodes(template), "DataBucketReadOnly")
    assert statement is not None, "no DataBucketReadOnly statement"
    statement["Resource"].append({"Fn::Sub": "arn:${AWS::Partition}:s3:::*"})
    assert _set_data_bucket_problems(template) != []


def test_negative_control_a_write_action_is_caught():
    template = copy.deepcopy(_template())
    statement = _find_statement(_policy_nodes(template), "DataBucketReadOnly")
    assert statement is not None, "no DataBucketReadOnly statement"
    statement["Action"].append("s3:PutObject")
    assert _set_data_bucket_problems(template) != []


def test_negative_control_an_unconditioned_data_bucket_grant_is_caught():
    template = copy.deepcopy(_template())
    statement = _find_statement(_policy_nodes(template), "GlueCatalogReadOnly")
    assert statement is not None
    statement["Resource"].append({"Fn::Sub": "arn:${AWS::Partition}:s3:::${DataBucketName}/*"})
    assert _empty_data_bucket_problems(template) != []


@pytest.mark.parametrize("rel", _BUCKET_GUIDES)
def test_the_guides_name_the_data_bucket_parameter(rel):
    assert re.search(rf"\b{BUCKET_PARAMETER}\b", (ROOT / rel).read_text(encoding="utf-8")), rel


# --------------------------------------------------------------------------- #
# Deploy instructions
# --------------------------------------------------------------------------- #

def test_every_documented_deploy_command_passes_the_trusted_account():
    blocks = []
    for rel, text in _surface_files():
        for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
            if TEMPLATE_NAME in block and "aws cloudformation" in block:
                blocks.append(rel)
                assert re.search(rf"\b{REQUIRED}=", block), f"{rel}: deploy command omits {REQUIRED}"
    assert blocks, "no documented deploy command found"


def test_every_help_surface_names_the_trusted_account():
    found = set()
    for rel, text in _surface_files():
        if TEMPLATE_NAME in text:
            found.add(rel)
            assert re.search(rf"\b{REQUIRED}\b", text), f"{rel} names the template but not {REQUIRED}"
    assert _KNOWN_SURFACES <= found, sorted(_KNOWN_SURFACES - found)
