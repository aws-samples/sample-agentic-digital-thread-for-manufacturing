# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ADT Identity Center Stack

Provisions:
- IAM Identity Center permission set with read-only Bedrock + SiteWise access
- Identity Center user 'adt' for demo access
- Account assignment linking act user to permission set

NOTE: IAM Identity Center must be enabled in the account before deploying this stack.
The instance ARN and identity store ID are looked up at deploy time.

NOTE: MFA must be disabled manually in the Identity Center console:
  Settings → Authentication → Multi-factor authentication → Configure → "Never"
  This cannot be automated via CDK/CLI.

  ⚠️ WARNING — this setting applies to the ENTIRE IAM Identity Center instance
  (every user in the account/organization), not just the demo user. Only do
  this in a dedicated, disposable demo account. To revert, set the same
  option back to "Every time they sign in (always-on)" or your previous
  policy after the demo.

Demo credentials: act / <demo-password>
Start URL: https://<identity-store-id>.awsapps.com/start
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    custom_resources as cr,
    aws_iam as iam,
    aws_lambda as lambda_,
    Duration,
)
from aws_cdk import aws_sso as sso
from constructs import Construct


class ADTIdentityCenterStack(Stack):
    """Identity Center permission set + demo user for ACT demo portal access."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Identity Center IDs — set via CDK context or cdk.json
        self.INSTANCE_ARN = self.node.try_get_context("identity_center_instance_arn")
        self.IDENTITY_STORE_ID = self.node.try_get_context("identity_center_store_id")
        if not self.INSTANCE_ARN or not self.IDENTITY_STORE_ID:
            raise ValueError(
                "CDK context 'identity_center_instance_arn' and 'identity_center_store_id' are required. "
                "Set them in cdk.json or pass via -c identity_center_instance_arn=arn:aws:sso:::instance/ssoins-XXXX"
            )

        permission_set = self._create_permission_set()
        self._create_sso_user(permission_set)
        self._create_app_tiles()
        self._create_outputs(permission_set)

    def _create_permission_set(self):
        """Read-only Bedrock + SiteWise + AgentCore permission set for reviewers."""
        return sso.CfnPermissionSet(
            self, "ACTDemoPermissionSet",
            instance_arn=self.INSTANCE_ARN,
            name="ACTDemoReadOnly",
            description="Read-only access to Bedrock AgentCore and SiteWise for ACT demo",
            session_duration="PT8H",
            managed_policies=[
                "arn:aws:iam::aws:policy/AmazonBedrockReadOnly",
                "arn:aws:iam::aws:policy/AWSIoTSiteWiseReadOnlyAccess",
            ],
            inline_policy={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    # This is a READ-ONLY console permission set for demo reviewers
                    # (the demo app itself invokes AgentCore via the ECS task role,
                    # not via this human-facing set). Scope the AgentCore data plane
                    # to read-only List*/Get* verbs rather than the full service.
                    # List*/Get* span resources, so Resource is "*".
                    "Action": [
                        "bedrock-agentcore:List*",
                        "bedrock-agentcore:Get*",
                        "bedrock-agentcore-control:List*",
                        "bedrock-agentcore-control:Get*",
                        "bedrock-agentcore-control:Describe*",
                    ],
                    "Resource": "*",
                }],
            },
        )

    def _create_sso_user(self, permission_set) -> None:
        """Identity Center user 'adt' via custom resource (CfnUser is not in CDK)."""
        user_lambda = lambda_.Function(
            self, "CreateIdentityCenterUserLambda",
            function_name="adt-create-sso-user",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            code=lambda_.Code.from_inline(self._get_user_lambda_code()),
            environment={
                "IDENTITY_STORE_ID": self.IDENTITY_STORE_ID,
                "INSTANCE_ARN": self.INSTANCE_ARN,
                "ACCOUNT_ID": self.account,
                "PERMISSION_SET_ARN": permission_set.attr_permission_set_arn,
            },
        )
        self._grant_user_lambda(user_lambda, permission_set)

        user_provider = cr.Provider(self, "SSOUserProvider", on_event_handler=user_lambda)
        from aws_cdk import CustomResource
        CustomResource(
            self, "ACTSSOUser",
            service_token=user_provider.service_token,
            properties={
                "Username": "adt",
                "DisplayName": "ACT Demo User",
                "Email": "adt@example.com",
            },
        )

    def _create_app_tiles(self) -> None:
        """Start-page tiles for the ADT and ERP dashboards.

        URLs come from other stacks' outputs; override at deploy time with
        ``-c adt_dashboard_url=<url> -c erp_dashboard_url=<url>``.
        """
        adt_dashboard_url = (
            self.node.try_get_context("adt_dashboard_url")
            or "https://<YOUR_ADT_DASHBOARD_DOMAIN>"
        )
        erp_dashboard_url = (
            self.node.try_get_context("erp_dashboard_url")
            or "https://<YOUR_ERP_DASHBOARD_DOMAIN>"
        )

        for app_name, app_desc, app_url in [
            ("ADT Dashboard", "Agentic Digital Thread Dashboard", adt_dashboard_url),
            ("ERP Dashboard", "AnyCompany Beverage ERP Simulation Dashboard", erp_dashboard_url),
        ]:
            app = sso.CfnApplication(
                self, f"App{app_name.replace(' ', '')}",
                instance_arn=self.INSTANCE_ARN,
                name=app_name,
                description=app_desc,
                application_provider_arn="arn:aws:sso::aws:applicationProvider/custom",
                portal_options=sso.CfnApplication.PortalOptionsConfigurationProperty(
                    sign_in_options=sso.CfnApplication.SignInOptionsProperty(
                        origin="APPLICATION",
                        application_url=app_url,
                    ),
                    visibility="ENABLED",
                ),
                status="ENABLED",
            )
            sso.CfnApplicationAssignment(
                self, f"AppAssign{app_name.replace(' ', '')}",
                application_arn=app.attr_application_arn,
                principal_id="${ActSSOUserId}",  # resolved at deploy via custom resource output
                principal_type="USER",
            )

    def _create_outputs(self, permission_set) -> None:
        CfnOutput(
            self, "IdentityCenterStartUrl",
            value=f"https://{self.IDENTITY_STORE_ID}.awsapps.com/start",
            description="Identity Center portal start URL — bookmark this for demo access",
        )
        CfnOutput(
            self, "PermissionSetArn",
            value=permission_set.attr_permission_set_arn,
            description="ACTDemoReadOnly permission set ARN",
        )

    def _grant_user_lambda(self, user_lambda, permission_set) -> None:
        """Least-privilege grants for the SSO user custom-resource Lambda."""
        # identitystore user CRUD is scoped to this identity store (CreateUser,
        # DeleteUser and ListUsers all accept the Identitystore resource type).
        identity_store_arn = (
            f"arn:{self.partition}:identitystore::{self.account}:"
            f"identitystore/{self.IDENTITY_STORE_ID}"
        )
        user_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "identitystore:CreateUser",
                "identitystore:DeleteUser",
                "identitystore:ListUsers",
            ],
            resources=[identity_store_arn],
        ))
        # Account assignment is scoped to this instance, this account, and the
        # permission set it provisions (the resource types
        # CreateAccountAssignment / DeleteAccountAssignment accept). The SDK
        # client is "sso-admin" but the IAM service prefix is "sso".
        user_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "sso:CreateAccountAssignment",
                "sso:DeleteAccountAssignment",
            ],
            resources=[
                self.INSTANCE_ARN,
                permission_set.attr_permission_set_arn,
                f"arn:{self.partition}:sso:::account/{self.account}",
            ],
        ))

    def _get_user_lambda_code(self) -> str:
        return '''
import json, boto3, os

def handler(event, context):
    rt = event.get("RequestType")
    store_id = os.environ["IDENTITY_STORE_ID"]
    instance_arn = os.environ["INSTANCE_ARN"]
    account_id = os.environ["ACCOUNT_ID"]
    pset_arn = os.environ["PERMISSION_SET_ARN"]
    username = event.get("ResourceProperties", {}).get("Username", "adt")
    display_name = event.get("ResourceProperties", {}).get("DisplayName", "ACT Demo User")
    email = event.get("ResourceProperties", {}).get("Email", "adt@example.com")

    id_store = boto3.client("identitystore")
    sso_admin = boto3.client("sso-admin")

    if rt == "Delete":
        physical_id = event.get("PhysicalResourceId", "")
        if physical_id and physical_id != "sso-user-act":
            try:
                id_store.delete_user(IdentityStoreId=store_id, UserId=physical_id)
            except Exception:
                pass
        return {"PhysicalResourceId": physical_id}

    # Check if user already exists
    existing = id_store.list_users(
        IdentityStoreId=store_id,
        Filters=[{"AttributePath": "UserName", "AttributeValue": username}]
    )
    if existing.get("Users"):
        user_id = existing["Users"][0]["UserId"]
    else:
        resp = id_store.create_user(
            IdentityStoreId=store_id,
            UserName=username,
            DisplayName=display_name,
            Name={"GivenName": "ACT", "FamilyName": "Demo"},
            Emails=[{"Value": email, "Type": "work", "Primary": True}],
        )
        user_id = resp["UserId"]

    # Assign permission set
    try:
        sso_admin.create_account_assignment(
            InstanceArn=instance_arn,
            TargetId=account_id,
            TargetType="AWS_ACCOUNT",
            PermissionSetArn=pset_arn,
            PrincipalType="USER",
            PrincipalId=user_id,
        )
    except sso_admin.exceptions.ConflictException:
        pass

    return {"PhysicalResourceId": user_id, "Data": {"UserId": user_id}}
'''
