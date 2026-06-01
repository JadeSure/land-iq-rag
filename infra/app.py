#!/usr/bin/env python3
"""CDK app entrypoint for the LandIQ RAG AWS landing (Track B).

Serverless backend (API Gateway + Lambda, SQS + Lambda) with a single small EC2
running Postgres + pgvector + pgbouncer. See docs/PLAN-RAG.md section 10.
"""

import os

import aws_cdk as cdk

from landiq_rag_stack import LandiqRagStack

app = cdk.App()

# Pass a concrete env only when explicitly provided (real deploy). Left agnostic
# otherwise so `cdk synth` works offline (VPC AZs resolve via Fn::GetAZs).
env = None
if os.environ.get("CDK_DEPLOY_ACCOUNT"):
    env = cdk.Environment(
        account=os.environ["CDK_DEPLOY_ACCOUNT"],
        region=os.environ.get("CDK_DEPLOY_REGION", "ap-southeast-2"),
    )

LandiqRagStack(app, "LandiqRagStack", env=env)
app.synth()
