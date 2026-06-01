"""LandIQ RAG AWS stack.

Always-on footprint = one t4g.small EC2 (Postgres + pgvector + pgbouncer) in a
private subnet. Everything else is serverless: API Gateway + Lambda for the API,
SQS + Lambda for ingestion/rebuild. Outbound to OpenAI goes through a t4g.nano NAT
instance (not a managed NAT gateway). No Bedrock, no Step Functions.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    CfnTag,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_dlm as dlm,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_lambda_event_sources as sources,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from constructs import Construct

HERE = Path(__file__).resolve().parent
BUILD_DIR = HERE / "build"  # assembled by build_lambda.sh before `cdk deploy`
SSM_PREFIX = "/landiq-rag"
LAMBDA_TIMEOUT = 900  # seconds; SQS visibility must exceed this


class LandiqRagStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        # ── Network: 2 AZ, NAT *instance* (not gateway) for cheap egress ──────
        nat = ec2.NatProvider.instance_v2(
            instance_type=ec2.InstanceType("t4g.nano"),
        )
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateway_provider=nat,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.S3
                ),
            },
        )

        # ── Raw document storage ──────────────────────────────────────────────
        bucket = s3.Bucket(
            self,
            "DocsBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(30),
                        ),
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90),
                        ),
                    ],
                ),
            ],
        )

        # ── SQS ingestion/rebuild queue + DLQ ─────────────────────────────────
        dlq = sqs.Queue(self, "IngestDLQ", retention_period=Duration.days(14))
        ingest_queue = sqs.Queue(
            self,
            "IngestQueue",
            visibility_timeout=Duration.seconds(LAMBDA_TIMEOUT + 60),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
        )

        # ── EC2: Postgres + pgvector + pgbouncer (the only always-on box) ─────
        db_sg = ec2.SecurityGroup(self, "DbSg", vpc=vpc, allow_all_outbound=True)
        lambda_sg = ec2.SecurityGroup(self, "LambdaSg", vpc=vpc, allow_all_outbound=True)
        db_sg.add_ingress_rule(lambda_sg, ec2.Port.tcp(6432), "pgbouncer from Lambdas")

        db_role = iam.Role(
            self,
            "DbInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        bucket.grant_read_write(db_role)  # nightly pg_dump -> S3 backups
        db_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParametersByPath"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{SSM_PREFIX}/*"],
            )
        )

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            f"mkdir -p /etc/landiq && echo BACKUP_BUCKET={bucket.bucket_name} > /etc/landiq/rag.env",
            f"echo SSM_PREFIX={SSM_PREFIX} >> /etc/landiq/rag.env",
        )
        user_data.add_commands((HERE / "bootstrap_ec2.sh").read_text())

        db_instance = ec2.Instance(
            self,
            "PgVectorDb",
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_group=db_sg,
            role=db_role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        30, volume_type=ec2.EbsDeviceVolumeType.GP3
                    ),
                )
            ],
        )
        Tags.of(db_instance).add("landiq-backup", "true")  # DLM target

        db_url = (
            f"postgresql://landiq:CHANGEME@{db_instance.instance_private_ip}:6432/landiq_rag"
        )

        # ── SSM config (admin-editable; F8). OpenAI key is a SecureString created
        #    out-of-band (CloudFormation cannot create SecureString params). ─────
        params = {
            "active_embedding_model": "openai:text-embedding-3-small",
            "chunk_tokens": "600",
            "chunk_overlap": "100",
            "storage_backend": "s3",
            "s3_bucket": bucket.bucket_name,
            "sqs_queue_url": ingest_queue.queue_url,
            "database_url": db_url,
        }
        for key, value in params.items():
            ssm.StringParameter(
                self, f"Param{key.title().replace('_', '')}",
                parameter_name=f"{SSM_PREFIX}/{key}", string_value=value,
            )

        # ── Lambda code asset (assembled by build_lambda.sh) ──────────────────
        code = lambda_.Code.from_asset(str(BUILD_DIR))
        common_env = {
            "RAG_PROFILE": "aws",
            "RAG_SSM_PREFIX": f"{SSM_PREFIX}/",
            "RAG_WORKER_ENABLED": "false",
            "RAG_RUN_MIGRATIONS_ON_STARTUP": "false",
        }
        ssm_read = iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{SSM_PREFIX}/*"],
        )

        def make_fn(name: str, handler: str, *, timeout: int, memory: int, **kw) -> lambda_.Function:
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=code,
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                security_groups=[lambda_sg],
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment=common_env,
                **kw,
            )
            fn.add_to_role_policy(ssm_read)
            return fn

        # API Lambda (FastAPI via Mangum) behind API Gateway.
        api_fn = make_fn("ApiFn", "landiq_rag.aws.lambda_api.handler", timeout=30, memory=512)
        bucket.grant_read_write(api_fn)
        ingest_queue.grant_send_messages(api_fn)

        # Ingestion/rebuild worker Lambda (SQS event source, capped concurrency).
        worker_fn = make_fn(
            "WorkerFn",
            "landiq_rag.aws.lambda_worker.handler",
            timeout=LAMBDA_TIMEOUT,
            memory=2048,
            reserved_concurrent_executions=10,  # bound DB connection pressure
        )
        bucket.grant_read(worker_fn)
        ingest_queue.grant_consume_messages(worker_fn)
        worker_fn.add_event_source(
            sources.SqsEventSource(ingest_queue, batch_size=5, report_batch_item_failures=True)
        )

        # Reconciler Lambda (EventBridge, every 15 min).
        reconciler_fn = make_fn(
            "ReconcilerFn", "landiq_rag.aws.lambda_reconciler.handler", timeout=120, memory=256
        )
        ingest_queue.grant_send_messages(reconciler_fn)
        events.Rule(
            self,
            "ReconcilerSchedule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            targets=[targets.LambdaFunction(reconciler_fn)],
        )

        api = apigw.LambdaRestApi(self, "Api", handler=api_fn, proxy=True)

        # ── EBS snapshot backups via Data Lifecycle Manager (daily, keep 7) ───
        dlm_role = iam.Role(
            self,
            "DlmRole",
            assumed_by=iam.ServicePrincipal("dlm.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSDataLifecycleManagerServiceRole"
                )
            ],
        )
        dlm.CfnLifecyclePolicy(
            self,
            "EbsBackup",
            description="LandIQ RAG Postgres EBS daily snapshots",
            state="ENABLED",
            execution_role_arn=dlm_role.role_arn,
            policy_details=dlm.CfnLifecyclePolicy.PolicyDetailsProperty(
                resource_types=["INSTANCE"],
                target_tags=[CfnTag(key="landiq-backup", value="true")],
                schedules=[
                    dlm.CfnLifecyclePolicy.ScheduleProperty(
                        name="daily",
                        create_rule=dlm.CfnLifecyclePolicy.CreateRuleProperty(
                            interval=24, interval_unit="HOURS", times=["17:00"]
                        ),
                        retain_rule=dlm.CfnLifecyclePolicy.RetainRuleProperty(count=7),
                    )
                ],
            ),
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "QueueUrl", value=ingest_queue.queue_url)
        CfnOutput(self, "DbInstanceId", value=db_instance.instance_id)
        CfnOutput(
            self,
            "ManualSteps",
            value=(
                "1) create SecureString " + SSM_PREFIX + "/openai_api_key; "
                "2) set a real DB password in " + SSM_PREFIX + "/database_url (or move creds "
                "to a SecureString); 3) run infra/build_lambda.sh before cdk deploy."
            ),
        )
