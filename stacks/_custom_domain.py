# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Optional custom domain (Route 53 + ACM) for the ADT app CloudFront distribution.

Generic, opt-in integration: when a custom domain and its Route 53 public
hosted zone are provided, the base ADT distribution ("AdtDistribution" on
ADTComputeStack) is served on that domain with a DNS-validated ACM certificate
and Route 53 alias records — all created automatically. When absent, nothing
here runs and the synthesized templates are unchanged.

Context keys (all three required together; deploy.sh flags
``--domain`` / ``--hosted-zone-id`` / ``--hosted-zone-name`` persist them as
sticky SSM parameters, HighByte discipline):

- ``custom_domain``      FQDN to serve the app on (e.g. ``adt.example.com`` —
                         may equal the zone apex).
- ``hosted_zone_id``     Route 53 PUBLIC hosted zone id authoritative for the
                         domain (must live in the deployment account).
- ``hosted_zone_name``   The zone's name (e.g. ``example.com``).

Design notes:

- CloudFront only accepts ACM certificates from **us-east-1**, so the
  certificate lives in its own :class:`CustomDomainCertificateStack` pinned to
  us-east-1. app.py enables ``cross_region_references`` on both that stack and
  ADTComputeStack so the certificate ARN flows into the distribution's viewer
  certificate regardless of the deployment region (same-region deploys fall
  back to plain exports — the flag is harmless there).
- :func:`wire_custom_domain` is an ADDITIVE post-construction seam (the
  ``_observability.py`` pattern): it locates "AdtDistribution" by its fixed id
  (a missing child raises loudly — no fallback), sets the alias + viewer
  certificate via an L1 property override, and adds A/AAAA alias records. The
  compute stack itself stays untouched and demo/data-mode agnostic.
- DNS validation requires the hosted zone to be authoritative and reachable
  from the public internet; the certificate stack blocks on validation during
  its first deploy (typically a few minutes).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_CONTEXT_HELP = (
    "custom_domain, hosted_zone_id and hosted_zone_name must be provided "
    "together (deploy.sh: --domain <fqdn> --hosted-zone-id <id> "
    "--hosted-zone-name <zone>)"
)


@dataclass(frozen=True)
class CustomDomainConfig:
    """Resolved, validated custom-domain settings."""

    domain_name: str
    hosted_zone_id: str
    hosted_zone_name: str


def resolve_custom_domain(
    domain_name: Optional[str],
    hosted_zone_id: Optional[str],
    hosted_zone_name: Optional[str],
) -> Optional[CustomDomainConfig]:
    """Validate the three context values into a config, or ``None`` when unset.

    Pure function (no CDK / AWS imports) so it is offline-unit-testable.
    Partial configuration fails LOUD — a silently ignored half-set flag would
    otherwise strand an environment without its domain.

    :raises ValueError: when only some of the three values are provided, or
        when the domain is not within the hosted zone.
    """
    values = {
        "custom_domain": (domain_name or "").strip(),
        "hosted_zone_id": (hosted_zone_id or "").strip(),
        "hosted_zone_name": (hosted_zone_name or "").strip().rstrip("."),
    }
    provided = {k: v for k, v in values.items() if v}
    if not provided:
        return None
    if len(provided) != len(values):
        missing = sorted(set(values) - set(provided))
        raise ValueError(
            f"Incomplete custom-domain configuration: missing {', '.join(missing)}. "
            + _CONTEXT_HELP
        )

    domain = values["custom_domain"].rstrip(".").lower()
    zone_name = values["hosted_zone_name"].lower()
    if domain != zone_name and not domain.endswith("." + zone_name):
        raise ValueError(
            f"custom_domain {domain!r} is not within hosted zone {zone_name!r} "
            "(it must equal the zone name or be a subdomain of it)."
        )

    return CustomDomainConfig(
        domain_name=domain,
        hosted_zone_id=values["hosted_zone_id"],
        hosted_zone_name=zone_name,
    )


# --------------------------------------------------------------------------
# CDK constructs — imported lazily by app.py only when a domain is configured.
# aws_cdk imports live at function/class-use time paths below, but keeping
# them module-level matches the sibling stacks/_*.py helpers; the unit test
# for the resolver guards with importorskip.
# --------------------------------------------------------------------------
from aws_cdk import (  # noqa: E402
    CfnOutput,
    Stack,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
)
from constructs import Construct  # noqa: E402


class CustomDomainCertificateStack(Stack):
    """DNS-validated ACM certificate for the CloudFront custom domain.

    Always deployed to **us-east-1** (the CloudFront certificate region),
    independent of the application region. app.py instantiates this stack only
    when the custom-domain context is configured, with
    ``cross_region_references=True`` so ADTComputeStack can consume
    ``certificate_arn`` directly.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CustomDomainConfig,
        include_highbyte: bool = False,
        include_erp_dashboard: bool = False,
        **kwargs,
    ) -> None:
        kwargs.setdefault("cross_region_references", True)
        super().__init__(
            scope,
            construct_id,
            description="ADT - ACM certificate for the optional custom domain (us-east-1)",
            **kwargs,
        )
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "CustomDomainZone",
            hosted_zone_id=config.hosted_zone_id,
            zone_name=config.hosted_zone_name,
        )
        self.certificate = acm.Certificate(
            self,
            "CustomDomainCertificate",
            domain_name=config.domain_name,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        CfnOutput(
            self,
            "CustomDomainCertificateArn",
            value=self.certificate.certificate_arn,
            description="ACM certificate for the ADT custom domain",
        )
        self._create_sibling_certificates(
            config,
            zone,
            include_highbyte=include_highbyte,
            include_erp_dashboard=include_erp_dashboard,
        )

    def _create_sibling_certificates(
        self,
        config: CustomDomainConfig,
        zone: route53.IHostedZone,
        *,
        include_highbyte: bool,
        include_erp_dashboard: bool,
    ) -> None:
        """Create enabled sibling certificates in their historical order."""
        self.highbyte_certificate: Optional[acm.ICertificate] = None
        if include_highbyte:
            self.highbyte_certificate = self._create_sibling_certificate(
                config,
                zone,
                subdomain="highbyte",
                certificate_id="HighByteCustomDomainCertificate",
                output_id="HighByteCustomDomainCertificateArn",
                output_description="ACM certificate for the HighByte UI custom domain",
            )
        self.erp_certificate: Optional[acm.ICertificate] = None
        if include_erp_dashboard:
            self.erp_certificate = self._create_sibling_certificate(
                config,
                zone,
                subdomain="erp",
                certificate_id="ErpDashboardCustomDomainCertificate",
                output_id="ErpDashboardCustomDomainCertificateArn",
                output_description="ACM certificate for the ERP dashboard custom domain",
            )

    def _create_sibling_certificate(
        self,
        config: CustomDomainConfig,
        zone: route53.IHostedZone,
        *,
        subdomain: str,
        certificate_id: str,
        output_id: str,
        output_description: str,
    ) -> acm.ICertificate:
        """Create one DNS-validated sibling certificate with stable IDs."""
        certificate = acm.Certificate(
            self,
            certificate_id,
            domain_name=f"{subdomain}.{config.domain_name}",
            validation=acm.CertificateValidation.from_dns(zone),
        )
        CfnOutput(
            self,
            output_id,
            value=certificate.certificate_arn,
            description=output_description,
        )
        return certificate


def wire_custom_domain(
    compute_stack: Stack,
    *,
    config: CustomDomainConfig,
    certificate: acm.ICertificate,
) -> None:
    """Attach the custom domain to the ADT distribution + create DNS records.

    Additive post-construction wiring (applied from app.py after
    ADTComputeStack is built): alias + viewer certificate on "AdtDistribution"
    (located by its fixed construct id — a rename raises loudly instead of
    silently dropping the domain), Route 53 A/AAAA ALIAS records in the
    provided hosted zone, and an ``ADTCustomDomainUrl`` output alongside the
    existing ``ADTCloudFrontUrl`` (which keeps pointing at the
    *.cloudfront.net name — both serve the app).
    """
    distribution = compute_stack.node.find_child("AdtDistribution")
    _attach_viewer_certificate(distribution, config.domain_name, certificate)
    _create_alias_records(compute_stack, distribution, config)
    CfnOutput(
        compute_stack,
        "ADTCustomDomainUrl",
        value=f"https://{config.domain_name}",
        description="ADT Dashboard URL (custom domain)",
    )


def wire_subdomain_alias(
    stack: Stack,
    *,
    subdomain: str,
    config: CustomDomainConfig,
    certificate: acm.ICertificate,
    distribution_construct_id: str,
    construct_id_prefix: Optional[str] = None,
    output_id: Optional[str] = None,
    output_description: Optional[str] = None,
    attach_viewer_certificate: bool = True,
) -> None:
    """Attach a sibling alias using stable caller-selected construct IDs.

    ``attach_viewer_certificate=False`` supports distributions whose historical
    L2 ``domain_names``/``certificate`` properties must retain their synthesized
    shape.  The seam then shares only Route 53 record construction.
    """
    distribution = stack.node.find_child(distribution_construct_id)
    fqdn = f"{subdomain}.{config.domain_name}"
    if attach_viewer_certificate:
        _attach_viewer_certificate(distribution, fqdn, certificate)
    prefix = construct_id_prefix or subdomain.title()
    _create_subdomain_alias_records(stack, distribution, config, fqdn, prefix)
    if output_id:
        CfnOutput(
            stack,
            output_id,
            value=f"https://{fqdn}",
            description=output_description,
        )


def _create_subdomain_alias_records(
    stack: Stack, distribution, config: CustomDomainConfig, fqdn: str, prefix: str
) -> None:
    zone = route53.HostedZone.from_hosted_zone_attributes(
        stack,
        f"{prefix}CustomDomainZone",
        hosted_zone_id=config.hosted_zone_id,
        zone_name=config.hosted_zone_name,
    )
    target = route53.RecordTarget.from_alias(
        route53_targets.CloudFrontTarget(distribution)
    )
    route53.ARecord(
        stack,
        f"{prefix}CustomDomainAliasA",
        zone=zone,
        record_name=fqdn,
        target=target,
    )
    route53.AaaaRecord(
        stack,
        f"{prefix}CustomDomainAliasAaaa",
        zone=zone,
        record_name=fqdn,
        target=target,
    )


def _attach_viewer_certificate(distribution, domain_name: str, certificate) -> None:
    """Set Aliases + ViewerCertificate (SNI, TLSv1.2_2021 minimum) on the L1."""
    cfn_distribution = distribution.node.default_child
    cfn_distribution.add_property_override(
        "DistributionConfig.Aliases", [domain_name]
    )
    cfn_distribution.add_property_override(
        "DistributionConfig.ViewerCertificate",
        {
            "AcmCertificateArn": certificate.certificate_arn,
            "SslSupportMethod": "sni-only",
            "MinimumProtocolVersion": "TLSv1.2_2021",
        },
    )


def _create_alias_records(compute_stack, distribution, config) -> None:
    """Route 53 A/AAAA ALIAS records for the domain → the distribution."""
    zone = route53.HostedZone.from_hosted_zone_attributes(
        compute_stack,
        "CustomDomainZone",
        hosted_zone_id=config.hosted_zone_id,
        zone_name=config.hosted_zone_name,
    )
    target = route53.RecordTarget.from_alias(
        route53_targets.CloudFrontTarget(distribution)
    )
    route53.ARecord(
        compute_stack,
        "CustomDomainAliasA",
        zone=zone,
        record_name=config.domain_name,
        target=target,
    )
    route53.AaaaRecord(
        compute_stack,
        "CustomDomainAliasAaaa",
        zone=zone,
        record_name=config.domain_name,
        target=target,
    )
