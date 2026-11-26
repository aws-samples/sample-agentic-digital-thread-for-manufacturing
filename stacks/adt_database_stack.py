# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ADT Database Stack (engine) — the application's own datastores.

Provisions ONLY the platform's own state stores:
- Amazon Neptune Analytics graph (the digital-thread knowledge graph)
- Amazon DynamoDB table (self-learning proposed mappings)
- Amazon DynamoDB tables (prompt library, dashboard cards, user feedback, usage)

This stack is part of the base engine and has **no** dependency on, or knowledge
of, any demo/seed/simulator infrastructure. The bundled sample source-system RDS
lives in the opt-in `ADTSeedDataStack` (deployed only with `--seed`/`--simulator`).
See ADR-0030.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_ssm as ssm,
)
from constructs import Construct

try:
    from aws_cdk import aws_neptunegraph as neptune
except ImportError:
    neptune = None


#: FEEDBACK TABLE ---------------------------------------------------------
#: Same class of gap as the prompt-library tables below, but worse: NOTHING
#: provisioned this table and NOTHING set ``FEEDBACK_TABLE_NAME``, so
#: ``FeedbackStorage.store`` no-oped while ``POST /api/feedback`` answered
#: ``{"status": "success"}`` — every thumbs-up/down on every deployed
#: environment was discarded (confirmed on the live task definition, whose only
#: table env vars were the four other stores). The name is wired into the
#: ``adt-app`` task as ``FEEDBACK_TABLE_NAME`` (stacks/adt_compute_stack.py),
#: which ``agents/config.py`` reads.
#:
#: Shape (``agentic_digital_thread/agents/storage.py`` owns the app side of this
#: contract; tests/unit/test_feedback_durability.py pins the two in lockstep):
#:
#: * ``feedback_id`` (HASH) + ``timestamp`` (RANGE) — ``feedback_id`` is a
#:   stable digest of (session_id, message_id), so one lookup by that id returns
#:   every thumb ever recorded for an answer instead of the latest write
#:   overwriting the earlier one.
#: * ``feedback-date-index`` (``feedback_date`` HASH + ``timestamp`` RANGE) —
#:   the admin surface reads by time range; the GSI turns that into one bounded
#:   Query per UTC day instead of a full-table scan, which is also what lets the
#:   task role hold only PutItem + Query.
#: * ``ttl`` (epoch seconds) — TTL is ENABLED here, unlike the sibling tables,
#:   because feedback rows carry customer-derived free text (``user_comment``).
#:   Retention is 90 days (``storage.FEEDBACK_TTL_DAYS``, the repo's existing
#:   decay horizon for accumulated customer-derived state); the attribute name
#:   matches the ``adt-onboarding-sessions`` convention.
#:
#: Everything else mirrors the sibling tables: on-demand billing, default
#: encryption and PITR settings, and a DESTROY removal policy (demo datastore —
#: teardown must not leave a paid-for table behind).
FEEDBACK_TABLE_NAME = "adt-feedback"
FEEDBACK_DATE_INDEX_NAME = "feedback-date-index"
FEEDBACK_TTL_ATTRIBUTE = "ttl"

#: USAGE TABLE -----------------------------------------------------------
#: Same gap as the feedback table had: nothing provisioned a usage table and
#: nothing set ``USAGE_TABLE_NAME``, so every chat turn's usage record went to
#: ``UsageStorage(None)`` and the admin dashboard's usage totals read nothing
#: (seen on a deployed account, 2026-10-21). Keyed to what ``UsageRecord`` writes
#: (``user_id`` HASH + ``timestamp`` RANGE); ``UsageStorage.get_by_time_range``
#: scans by timestamp, so no index is needed. ``ttl`` bounds retention (a row
#: carries the user's email). Conventions mirror the feedback table: on-demand
#: billing, default encryption and PITR settings, DESTROY removal policy.
#: ``agents/storage.py`` owns the app side (lockstep:
#: tests/unit/test_usage_records_land.py).
USAGE_TABLE_NAME = "adt-usage"
USAGE_TTL_ATTRIBUTE = "ttl"


class ADTDatabaseStack(Stack):
    """ADT engine datastores: Amazon Neptune Analytics + Amazon DynamoDB."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, description="ADT - Neptune Analytics graph + DynamoDB (engine datastores)", **kwargs)
        self.network_stack = network_stack

        # --- Neptune Analytics (the app's knowledge graph) ---
        self._create_neptune()

        # --- DynamoDB (self-learning proposed mappings) ---
        self._create_dynamodb()

        # --- DynamoDB (prompt library + dashboard cards persistence) ---
        self._create_prompt_library_tables()

        # --- DynamoDB (user feedback persistence) ---
        self._create_feedback_table()

        # --- DynamoDB (per-turn usage records for the admin dashboard) ---
        self._create_usage_table()

        # --- DynamoDB (durable per-system guidance delivery) ---
        self._create_system_guidance_table()

    # ------------------------------------------------------------------
    def _create_neptune(self) -> None:
        """Neptune Analytics graph for digital thread."""
        if neptune is None:
            return

        graph = neptune.CfnGraph(
            self, "DigitalThreadGraph",
            graph_name="adt",
            provisioned_memory=16,
            # The orchestrator (AgentCore-managed runtime) and the ADT app (ECS task in
            # private subnets) reach this graph via the regional `neptune-graph`
            # ExecuteQuery data-plane API (boto3), not via the graph's own connectable
            # network endpoint. That API path is independent of public_connectivity, so
            # there is no need to expose a public graph endpoint for a sample.
            public_connectivity=False,
            replica_count=0,
            deletion_protection=False,
        )
        self.graph_id = graph.attr_graph_id
        self.graph_endpoint = graph.attr_endpoint

        CfnOutput(self, "NeptuneGraphId", value=self.graph_id)
        CfnOutput(self, "NeptuneEndpoint", value=self.graph_endpoint)

    # ------------------------------------------------------------------
    def _create_dynamodb(self) -> None:
        """DynamoDB table for self-learning proposed mappings."""
        self.proposed_mappings_table = dynamodb.Table(
            self, "ProposedMappingsTable",
            table_name="adt-proposed-mappings",
            partition_key=dynamodb.Attribute(
                name="mapping_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="discovered_at", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.proposed_mappings_table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="discovered_at", type=dynamodb.AttributeType.STRING
            ),
        )
        self.proposed_mappings_table.add_global_secondary_index(
            index_name="subject-key-index",
            partition_key=dynamodb.Attribute(
                name="subject_key", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="discovered_at", type=dynamodb.AttributeType.STRING
            ),
        )

        # SSM parameter for runtime discovery
        ssm.StringParameter(
            self, "ProposedMappingsTableParam",
            parameter_name="/adt/proposed-mappings-table",
            string_value="adt-proposed-mappings",
        )

        CfnOutput(self, "ProposedMappingsTableName", value=self.proposed_mappings_table.table_name)

    # ------------------------------------------------------------------
    def _create_prompt_library_tables(self) -> None:
        """DynamoDB tables backing the Prompt Library + dashboard cards.

        Without these tables the app's ``DynamoDBStorage`` silently no-ops
        (``agents/storage.py`` returns early when the table name is unset), so
        ``POST /api/prompts`` reports success but ``GET /api/prompts`` is always
        empty and saved dashboard cards do not survive a container restart. The
        table names are wired into the ``adt-app`` task as
        ``PROMPT_TEMPLATES_TABLE_NAME`` / ``DASHBOARD_CARDS_TABLE_NAME`` (see
        ``stacks/adt_compute_stack.py``), which ``agents/config.py`` reads.

        Both are keyed by a single partition key and read via ``get_item`` /
        ``scan`` (``PromptTemplateStorage`` / ``DashboardCardStorage``);
        ``DashboardCardStorage.get_by_prompt`` uses a ``scan`` filter on
        ``prompt_id`` rather than a query, so no GSI is required. Conventions
        mirror ``proposed_mappings_table`` above: on-demand billing and a
        DESTROY removal policy (demo datastore; default PITR, matching siblings).
        """
        self.prompt_templates_table = dynamodb.Table(
            self, "PromptTemplatesTable",
            table_name="adt-prompt-templates",
            partition_key=dynamodb.Attribute(
                name="template_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.dashboard_cards_table = dynamodb.Table(
            self, "DashboardCardsTable",
            table_name="adt-dashboard-cards",
            partition_key=dynamodb.Attribute(
                name="card_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        CfnOutput(self, "PromptTemplatesTableName", value=self.prompt_templates_table.table_name)
        CfnOutput(self, "DashboardCardsTableName", value=self.dashboard_cards_table.table_name)

    # ------------------------------------------------------------------
    def _create_feedback_table(self) -> None:
        """DynamoDB table backing user feedback — see FEEDBACK TABLE above."""
        self.feedback_table = dynamodb.Table(
            self, "FeedbackTable",
            table_name=FEEDBACK_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="feedback_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=FEEDBACK_TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.feedback_table.add_global_secondary_index(
            index_name=FEEDBACK_DATE_INDEX_NAME,
            partition_key=dynamodb.Attribute(
                name="feedback_date", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp", type=dynamodb.AttributeType.STRING
            ),
        )

        CfnOutput(self, "FeedbackTableName", value=self.feedback_table.table_name)

    def _create_usage_table(self) -> None:
        """DynamoDB table backing usage records — see USAGE TABLE above."""
        self.usage_table = dynamodb.Table(
            self, "UsageTable",
            table_name=USAGE_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="user_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=USAGE_TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.DESTROY,
        )
        CfnOutput(self, "UsageTableName", value=self.usage_table.table_name)

    def _create_system_guidance_table(self) -> None:
        """Durable exact guidance snapshots keyed by system id.

        Uses the neighboring engine tables' on-demand billing, default
        AWS-owned encryption/PITR posture, and DESTROY demo removal policy.
        """
        self.system_guidance_table = dynamodb.Table(
            self, "SystemGuidanceTable",
            table_name="adt-system-guidance",
            partition_key=dynamodb.Attribute(
                name="system_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        CfnOutput(
            self, "SystemGuidanceTableName",
            value=self.system_guidance_table.table_name,
        )
