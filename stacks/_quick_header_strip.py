# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Strip the Quick gateway shared secret from viewer requests at CloudFront.

The app's ``AuthMiddleware`` admits ``/api/ask/async``, ``/api/ask/result/*``
and ``GET /api/prompts`` without a Cognito session when the request carries
``x-quick-auth`` equal to ``QUICK_GATEWAY_TOKEN``. The only legitimate
senders reach the internal ALB directly and never pass through CloudFront:
the Quick query-bridge AgentCore runtime (``ADT_APP_INTERNAL_URL``) and the
Quick API Gateway REST API over its VPC link, which injects the header on
every integration request.

The app distribution forwards viewer headers to the ALB
(``ALL_VIEWER_EXCEPT_HOST_HEADER``), so without this function an internet
caller holding the token could reach those routes through CloudFront. The
viewer-request function below deletes the header before CloudFront builds
the origin request. It covers the behavior that serves ``/api/*``. The app
also refuses the token on any request that carries ``X-Amz-Cf-Id``
(``agents/auth/middleware.py``), which CloudFront documents adding to every
origin request; that refusal is the backstop for anything the function does
not see.

Attach the function only where no Lambda@Edge viewer-request handler runs:
CloudFront rejects a behavior that has both a CloudFront Function and a
Lambda@Edge function on the same event type. With edge auth OFF that is the
app distribution's default behavior; with edge auth ON it is the ``/api/*``
behavior (the default behavior carries ``CheckAuth`` there).
"""
from __future__ import annotations

from aws_cdk import aws_cloudfront as cloudfront
from constructs import Construct

#: The Quick gateway shared-secret header. Must equal
#: ``AuthMiddleware.QUICK_GATEWAY_HEADER`` in
#: ``agentic_digital_thread/agents/auth/middleware.py`` (pinned by
#: ``tests/test_quick_header_strip.py``). CloudFront Functions see header
#: names in lower case.
QUICK_GATEWAY_HEADER = "x-quick-auth"

#: Construct id of the ONE function per app distribution.
STRIP_FUNCTION_ID = "AdtQuickHeaderStripFn"

#: Viewer-request handler (cloudfront-js-2.0). Deleting the key removes every
#: value of the header, so a repeated header cannot slip through either.
STRIP_FUNCTION_CODE = (
    "function handler(event) {\n"
    "  var request = event.request;\n"
    f"  delete request.headers['{QUICK_GATEWAY_HEADER}'];\n"
    "  return request;\n"
    "}"
)


def create_quick_header_strip(scope: Construct) -> cloudfront.Function:
    """Create the viewer-request function that removes ``x-quick-auth``.

    Call once per distribution and attach it with
    :func:`quick_header_strip_associations`.
    """
    return cloudfront.Function(
        scope,
        STRIP_FUNCTION_ID,
        comment=f"Remove the {QUICK_GATEWAY_HEADER} header from viewer requests",
        runtime=cloudfront.FunctionRuntime.JS_2_0,
        code=cloudfront.FunctionCode.from_inline(STRIP_FUNCTION_CODE),
    )


def quick_header_strip_associations(
    function: cloudfront.IFunction,
) -> list[cloudfront.FunctionAssociation]:
    """The ``function_associations`` value for a behavior that serves ``/api/*``."""
    return [cloudfront.FunctionAssociation(
        function=function,
        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
    )]
