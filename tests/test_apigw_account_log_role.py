# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The account API Gateway CloudWatch role setter (stacks/_apigw_log_role.py),
outside the workshop: set our role only when the setting is empty or
unusable, never clear or replace a working one, and retain our role.

The lifecycle tests run the inline handler against a fake account. The synth
tests check where the setter lives, that every REST API stack depends on it,
that its role is retained, and that a workshop synth has none of it.
"""
from __future__ import annotations

import sys
import types

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks import _apigw_log_role, _nag_suppressions, _sample_hardening
from aws_cdk import aws_apigateway as apigw

ACCOUNT = "111122223333"
OWNER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/ADTNetworkStack-ApiGatewayAccountLogRole-A"
OWNER_ROLE_2 = f"arn:aws:iam::{ACCOUNT}:role/ADTNetworkStack-ApiGatewayAccountLogRole-B"
#: A role another ADT stack's own setter creates (CDK name
#: <stack>-<prefix>ApiGatewayLogRole<hash>).
OTHER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/MockStack-MockApiGatewayLogRole-C"
THEIRS = f"arn:aws:iam::{ACCOUNT}:role/team/their-apigw-logs"
TRUST = [{"Effect": "Allow", "Principal": {"Service": "apigateway.amazonaws.com"},
          "Action": "sts:AssumeRole"}]


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Account:
    """One account and region: the API Gateway setting and the IAM roles that
    exist (name -> logs allowed?). UpdateAccount rejects a role that does not
    exist, as API Gateway does."""

    def __init__(self, current: str = "", roles: dict | None = None):
        self.current = current
        self.roles = {r.rsplit("/", 1)[-1]: True for r in (OWNER_ROLE, OWNER_ROLE_2, OTHER_ROLE)}
        self.roles.update(roles or {})
        self.patches: list[str] = []

    def delete_role(self, arn: str) -> None:
        self.roles.pop(arn.rsplit("/", 1)[-1], None)

    def delete_stack_roles(self, retained=()):
        """CloudFormation deleting the owner's stack: its role goes unless
        retained."""
        if OWNER_ROLE not in retained:
            self.delete_role(OWNER_ROLE)

    def usable(self, arn: str) -> bool:
        """Can an API that logs to CloudWatch through the setting still do so?"""
        return bool(arn) and self.roles.get(arn.rsplit("/", 1)[-1], False)

    def apigateway(self):
        account = self

        class ApiGateway:
            def get_account(self):
                return {"cloudwatchRoleArn": account.current} if account.current else {}

            def update_account(self, patchOperations):
                value = patchOperations[0]["value"]
                if value and value.rsplit("/", 1)[-1] not in account.roles:
                    raise ClientError("BadRequestException")
                account.patches.append(value)
                account.current = value
        return ApiGateway()

    def iam(self):
        account = self

        class Iam:
            def get_role(self, RoleName):
                if RoleName not in account.roles:
                    raise ClientError("NoSuchEntity")
                return {"Role": {"AssumeRolePolicyDocument": {"Statement": TRUST}}}

            def simulate_principal_policy(self, PolicySourceArn, ActionNames):
                ok = account.roles[PolicySourceArn.rsplit("/", 1)[-1]]
                return {"EvaluationResults": [
                    {"EvalDecision": "allowed" if ok else "implicitDeny"} for _ in ActionNames]}
        return Iam()


def _load(monkeypatch, source: str, account: Account):
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda name: account.apigateway() if name == "apigateway" else account.iam()
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    ns: dict = {}
    exec(compile(source, "handler", "exec"), ns)  # nosec B102 - the stacks' own inline handlers under test
    monkeypatch.setattr(ns["time"], "sleep", lambda _s: None)
    return ns["on_event"]


class Resource:
    """One custom resource's CloudFormation lifecycle against *account*."""

    def __init__(self, monkeypatch, source, account, role):
        self.on_event = _load(monkeypatch, source, account)
        self.role = role
        self.pid = None

    def create(self):
        self.pid = self.on_event({"RequestType": "Create",
                                  "ResourceProperties": {"RoleArn": self.role}}, None)["PhysicalResourceId"]
        return self.pid

    def create_lost_response(self):
        """A Create whose changes land but whose response never reaches
        CloudFormation, which then retries the Create with no physical id."""
        self.on_event({"RequestType": "Create", "ResourceProperties": {"RoleArn": self.role}}, None)

    def update(self, new_role):
        event = {"RequestType": "Update", "PhysicalResourceId": self.pid,
                 "ResourceProperties": {"RoleArn": new_role},
                 "OldResourceProperties": {"RoleArn": self.role}}
        new_pid = self.on_event(event, None)["PhysicalResourceId"]
        old = (self.pid, self.role)
        self.role = new_role
        return new_pid, old

    def delete(self, pid=None, role=None):
        self.on_event({"RequestType": "Delete", "PhysicalResourceId": pid or self.pid,
                       "ResourceProperties": {"RoleArn": role or self.role}}, None)


@pytest.fixture
def make(monkeypatch):
    def factory(account: Account):
        return Resource(monkeypatch, _apigw_log_role.HANDLER_SOURCE, account, OWNER_ROLE)
    return factory


def test_an_empty_setting_gets_our_role(make):
    account = Account()
    owner = make(account)
    assert owner.create() == _apigw_log_role.PHYSICAL_ID
    assert account.current == OWNER_ROLE and account.patches == [OWNER_ROLE]


@pytest.mark.parametrize("name", [
    "their-apigw-logs", "ApiGatewayLogRoleForTeam", "TeamApiGatewayLogRole",
    "MockStack-MockApiGatewayLogRole-C", "ADTNetworkStack-ApiGatewayAccountLogRole-OLD",
])
def test_a_usable_role_is_kept_whoever_made_it(make, name):
    arn = f"arn:aws:iam::{ACCOUNT}:role/{name}"
    account = Account(current=arn, roles={name: True})
    owner = make(account)
    owner.create()
    owner.update(OWNER_ROLE)
    assert account.current == arn and account.patches == []


@pytest.mark.parametrize("roles", [{}, {"their-apigw-logs": False}], ids=["deleted", "no-logs"])
def test_an_unusable_role_is_replaced_by_ours(make, roles):
    account = Account(current=THEIRS, roles=roles)
    owner = make(account)
    owner.create()
    assert account.current == OWNER_ROLE


def test_an_update_resets_a_setting_that_went_empty_or_unusable(make):
    account = Account()
    owner = make(account)
    owner.create()
    account.current = ""
    owner.update(OWNER_ROLE)
    assert account.current == OWNER_ROLE


@pytest.mark.parametrize("start", ["", THEIRS])
def test_delete_never_touches_the_setting_and_our_role_is_retained(make, start):
    account = Account(current=start, roles={"their-apigw-logs": True})
    owner = make(account)
    owner.create()
    before, patches = account.current, list(account.patches)
    owner.delete()
    account.delete_stack_roles(retained={OWNER_ROLE})  # CloudFormation's delete: our role is RETAIN
    assert account.current == before and account.patches == patches
    assert account.usable(account.current)


def test_retries_do_the_same_thing(make):
    account = Account()
    owner = make(account)
    owner.create_lost_response()
    assert owner.create() == _apigw_log_role.PHYSICAL_ID
    assert account.patches == [OWNER_ROLE]


def test_the_handler_records_no_ownership_and_has_no_delete_branch_that_writes():
    src = _apigw_log_role.HANDLER_SOURCE
    assert "OWNED" not in src and "KEPT" not in src and "_put_back" not in src
    delete_branch = src.split('if event["RequestType"] == "Delete":', 1)[1].split("return", 1)[0]
    assert "_patch" not in delete_branch


def _network(context: dict):
    """A network stack, two stacks with a REST API stage and one without."""
    app = cdk.App(context=context)
    env = cdk.Environment(account=ACCOUNT, region="us-east-1")
    network = cdk.Stack(app, "ADTNetworkStack", env=env)
    apis = []
    for i in range(2):
        stack = cdk.Stack(app, f"Api{i}", env=env)
        api = apigw.RestApi(stack, "Api", deploy=True)
        api.root.add_method("GET", apigw.MockIntegration())
        apis.append(stack)
    plain = cdk.Stack(app, "Plain", env=env)
    setting = _sample_hardening.own_account_log_role(network)
    _nag_suppressions.apply_network_suppressions(network)
    return network, apis, plain, setting


def test_the_owner_lives_in_the_network_stack_and_rest_api_stacks_depend_on_it():
    network, apis, plain, setting = _network({})
    assert setting is not None
    for stack in apis:
        assert network in stack.dependencies
    assert network not in plain.dependencies
    t = Template.from_stack(network).to_json()
    customs = [k for k, v in t["Resources"].items()
               if v["Type"] == "AWS::CloudFormation::CustomResource"]
    assert customs == [k for k in customs if k.startswith("ApiGatewayAccountLogRoleSetting")]
    assert len(customs) == 1
    codes = [v["Properties"]["Code"].get("ZipFile") for v in t["Resources"].values()
             if v["Type"] == "AWS::Lambda::Function"]
    assert _apigw_log_role.HANDLER_SOURCE in codes
    assert not [v for v in t["Resources"].values() if v["Type"] == "AWS::ApiGateway::Account"]


def test_the_owner_role_is_retained_on_its_own_path():
    network, _, _, _ = _network({})
    t = Template.from_stack(network).to_json()
    roles = [v for k, v in t["Resources"].items() if v["Type"] == "AWS::IAM::Role"
             and k.startswith(_apigw_log_role.ID_PREFIX)
             and not k.startswith(_apigw_log_role.ID_PREFIX + "Fn")
             and not k.startswith(_apigw_log_role.ID_PREFIX + "Provider")]
    assert len(roles) == 1
    (role,) = roles
    assert role["DeletionPolicy"] == "Retain" and role["UpdateReplacePolicy"] == "Retain"
    assert role["Properties"]["Path"] == _apigw_log_role.ROLE_PATH


def test_a_workshop_synth_has_no_owner_and_adds_no_dependency():
    network, apis, _, setting = _network({"with_workshop": "true"})
    assert setting is None
    for stack in apis:
        assert network not in stack.dependencies
    t = Template.from_stack(network).to_json()
    assert not any(k.startswith(_apigw_log_role.ID_PREFIX) for k in t.get("Resources", {}))


def test_owner_suppressions_are_scoped_to_its_resources():
    network, _, _, _ = _network({})
    t = Template.from_stack(network).to_json()
    for logical, res in t["Resources"].items():
        rules = {r["id"] for r in res.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])}
        if logical.startswith(_apigw_log_role.ID_PREFIX):
            if res["Type"] == "AWS::IAM::Role":
                assert "AwsSolutions-IAM4" in rules
        else:
            assert not rules & {"AwsSolutions-IAM4", "AwsSolutions-IAM5"}, logical
