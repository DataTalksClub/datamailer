#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "cloudformation" / "datamailer-mvp.json"
CONFIG_DIR = ROOT / "infra" / "config"

EXPECTED_QUEUES = {
    "TransactionalEmailQueue": "transactional-email",
    "CampaignEmailQueue": "campaign-email",
    "SesWebhooksQueue": "ses-webhooks",
    "EmailEventsQueue": "email-events",
}
EXPECTED_DLQS = {
    "TransactionalEmailDLQ": "transactional-email-dlq",
    "CampaignEmailDLQ": "campaign-email-dlq",
    "SesWebhooksDLQ": "ses-webhooks-dlq",
    "EmailEventsDLQ": "email-events-dlq",
}
EXPECTED_WORKERS = {
    "TransactionalEmailWorker": {
        "handler": "mailing.workers.handlers.transactional_email_handler",
        "role": "TransactionalEmailWorkerRole",
        "log_group": "TransactionalEmailWorkerLogGroup",
        "queue": "TransactionalEmailQueue",
        "dlq": "TransactionalEmailDLQ",
        "suffix": "transactional-email",
        "can_send_email": True,
    },
    "CampaignEmailWorker": {
        "handler": "mailing.workers.handlers.campaign_email_handler",
        "role": "CampaignEmailWorkerRole",
        "log_group": "CampaignEmailWorkerLogGroup",
        "queue": "CampaignEmailQueue",
        "dlq": "CampaignEmailDLQ",
        "suffix": "campaign-email",
        "can_send_email": True,
    },
    "SesWebhooksWorker": {
        "handler": "mailing.workers.handlers.ses_webhooks_handler",
        "role": "SesWebhooksWorkerRole",
        "log_group": "SesWebhooksWorkerLogGroup",
        "queue": "SesWebhooksQueue",
        "dlq": "SesWebhooksDLQ",
        "suffix": "ses-webhooks",
        "can_send_email": False,
    },
    "EmailEventsWorker": {
        "handler": "mailing.workers.handlers.email_events_handler",
        "role": "EmailEventsWorkerRole",
        "log_group": "EmailEventsWorkerLogGroup",
        "queue": "EmailEventsQueue",
        "dlq": "EmailEventsDLQ",
        "suffix": "email-events",
        "can_send_email": False,
    },
}
EXPECTED_EVENT_MAPPINGS = {
    "TransactionalEmailEventSourceMapping",
    "CampaignEmailEventSourceMapping",
    "SesWebhooksEventSourceMapping",
    "EmailEventsEventSourceMapping",
}
EXPECTED_ALARM_METRICS = {
    "ApproximateAgeOfOldestMessage",
    "ApproximateNumberOfMessagesVisible",
    "Errors",
    "Throttles",
    "Duration",
    "CPUUtilization",
    "DatabaseConnections",
    "FreeStorageSpace",
    "Reject",
    "Bounce",
    "Complaint",
    "HealthCheckOk",
    "SendingAgeSeconds",
}


def load_json(path):
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: invalid JSON: {exc}") from exc


def require(condition, message, errors):
    if not condition:
        errors.append(message)


def resource(resources, name, expected_type, errors):
    item = resources.get(name)
    require(item is not None, f"missing resource {name}", errors)
    if item is None:
        return {}
    require(item.get("Type") == expected_type, f"{name} must be {expected_type}", errors)
    return item


def as_list(value):
    if isinstance(value, list):
        return value
    return [value]


def statement_actions(statement):
    return set(as_list(statement.get("Action", [])))


def policy_statements(role):
    statements = []
    for policy in role.get("Properties", {}).get("Policies", []):
        statements.extend(policy.get("PolicyDocument", {}).get("Statement", []))
    return statements


def has_statement(role, *, actions, resource_value=None):
    expected_actions = set(actions)
    for statement in policy_statements(role):
        if not expected_actions.issubset(statement_actions(statement)):
            continue
        if resource_value is None or statement.get("Resource") == resource_value:
            return True
    return False


def uses_forbidden_queue(role, allowed_resources):
    allowed = {json.dumps(resource_value, sort_keys=True) for resource_value in allowed_resources}
    for statement in policy_statements(role):
        actions = statement_actions(statement)
        if not any(action.startswith("sqs:") for action in actions):
            continue
        resources = as_list(statement.get("Resource"))
        for resource_value in resources:
            if json.dumps(resource_value, sort_keys=True) not in allowed:
                return True
    return False


def has_ses_permission(role):
    return any(action.startswith("ses:") for statement in policy_statements(role) for action in statement_actions(statement))


def validate_worker_iam_and_logs(resources, errors):
    require("WorkerRuntimeRole" not in resources, "shared WorkerRuntimeRole must not exist; workers need scoped roles", errors)

    seen_roles = set()
    for worker_name, config in EXPECTED_WORKERS.items():
        worker = resource(resources, worker_name, "AWS::Lambda::Function", errors)
        props = worker.get("Properties", {})
        role_ref = props.get("Role")
        expected_role_ref = {"Fn::GetAtt": [config["role"], "Arn"]}
        require(role_ref == expected_role_ref, f"{worker_name} must use {config['role']}", errors)
        seen_roles.add(json.dumps(role_ref, sort_keys=True))
        require(config["log_group"] in as_list(worker.get("DependsOn", [])), f"{worker_name} must depend on its log group", errors)

        log_group = resource(resources, config["log_group"], "AWS::Logs::LogGroup", errors)
        log_props = log_group.get("Properties", {})
        expected_log_name = {"Fn::Sub": f"/aws/lambda/${{ProjectName}}-${{EnvironmentName}}-{config['suffix']}"}
        require(log_props.get("LogGroupName") == expected_log_name, f"{config['log_group']} must name the worker log group", errors)
        require(log_props.get("RetentionInDays") == {"Ref": "LambdaLogRetentionDays"}, f"{config['log_group']} must set RetentionInDays", errors)

        role = resource(resources, config["role"], "AWS::IAM::Role", errors)
        managed = set(role.get("Properties", {}).get("ManagedPolicyArns", []))
        require(not managed, f"{config['role']} must use inline scoped runtime permissions, not managed policies", errors)
        queue_arn = {"Fn::GetAtt": [config["queue"], "Arn"]}
        dlq_arn = {"Fn::GetAtt": [config["dlq"], "Arn"]}
        log_arn = {
            "Fn::Sub": f"arn:${{AWS::Partition}}:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:/aws/lambda/${{ProjectName}}-${{EnvironmentName}}-{config['suffix']}:*"
        }
        require(
            has_statement(
                role,
                actions=["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
                resource_value=queue_arn,
            ),
            f"{config['role']} must read/delete only from {config['queue']}",
            errors,
        )
        require(
            has_statement(role, actions=["sqs:SendMessage"], resource_value=dlq_arn),
            f"{config['role']} must write only to {config['dlq']}",
            errors,
        )
        require(
            has_statement(role, actions=["logs:CreateLogStream", "logs:PutLogEvents"], resource_value=log_arn),
            f"{config['role']} must write only to its Lambda log group",
            errors,
        )
        require(
            has_statement(
                role,
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeSubnets",
                    "ec2:DeleteNetworkInterface",
                ],
                resource_value="*",
            ),
            f"{config['role']} must include inline VPC network-interface permissions",
            errors,
        )
        require(
            has_statement(role, actions=["secretsmanager:GetSecretValue"], resource_value={"Ref": "DatabaseSecret"}),
            f"{config['role']} must read the database secret",
            errors,
        )
        require(
            not uses_forbidden_queue(role, [queue_arn, dlq_arn]),
            f"{config['role']} has SQS permissions outside its source queue/DLQ",
            errors,
        )
        if config["can_send_email"]:
            require(has_ses_permission(role), f"{config['role']} must have SES send permission", errors)
        else:
            require(not has_ses_permission(role), f"{config['role']} must not have SES send permission", errors)

    require(len(seen_roles) == len(EXPECTED_WORKERS), "each Lambda worker must use a distinct IAM role", errors)


def validate_template(template):
    errors = []
    resources = template.get("Resources", {})
    parameters = template.get("Parameters", {})

    for name in [
        "EnvironmentName",
        "WebAmiId",
        "WebInstanceType",
        "LambdaArtifactBucket",
        "LambdaArtifactKey",
        "LambdaLogRetentionDays",
        "OperatorAlarmTopicArn",
        "SESSenderIdentity",
        "DefaultFromEmail",
        "PublicBaseUrl",
    ]:
        require(name in parameters, f"missing parameter {name}", errors)

    web = resource(resources, "WebInstance", "AWS::EC2::Instance", errors)
    require(web.get("Properties", {}).get("InstanceType") == {"Ref": "WebInstanceType"}, "web instance type must be parameterized", errors)

    db = resource(resources, "DatabaseInstance", "AWS::RDS::DBInstance", errors)
    db_props = db.get("Properties", {})
    require(db.get("DeletionPolicy") == "Snapshot", "database must snapshot on deletion", errors)
    require(db_props.get("StorageEncrypted") is True, "database storage must be encrypted", errors)
    require(db_props.get("PubliclyAccessible") is False, "database must not be public", errors)
    require(db_props.get("BackupRetentionPeriod") == {"Ref": "DBBackupRetentionDays"}, "database backup retention must be parameterized", errors)

    for name, suffix in EXPECTED_QUEUES.items():
        queue = resource(resources, name, "AWS::SQS::Queue", errors)
        props = queue.get("Properties", {})
        require("RedrivePolicy" in props, f"{name} must have a DLQ redrive policy", errors)
        require(props.get("ReceiveMessageWaitTimeSeconds") == 20, f"{name} must enable long polling", errors)
        require(suffix in json.dumps(props.get("QueueName", "")), f"{name} queue name should include {suffix}", errors)

    for name, suffix in EXPECTED_DLQS.items():
        dlq = resource(resources, name, "AWS::SQS::Queue", errors)
        require(suffix in json.dumps(dlq.get("Properties", {}).get("QueueName", "")), f"{name} queue name should include {suffix}", errors)

    for name, config in EXPECTED_WORKERS.items():
        worker = resource(resources, name, "AWS::Lambda::Function", errors)
        props = worker.get("Properties", {})
        require(props.get("Handler") == config["handler"], f"{name} handler must be {config['handler']}", errors)
        require(props.get("Architectures") == ["arm64"], f"{name} must run on arm64", errors)
        require(props.get("ReservedConcurrentExecutions", 0) <= 4, f"{name} concurrency must start conservatively", errors)
        require(props.get("Timeout", 0) < 900, f"{name} timeout must remain below SQS visibility timeout", errors)

    validate_worker_iam_and_logs(resources, errors)

    for name in EXPECTED_EVENT_MAPPINGS:
        mapping = resource(resources, name, "AWS::Lambda::EventSourceMapping", errors)
        props = mapping.get("Properties", {})
        require(props.get("FunctionResponseTypes") == ["ReportBatchItemFailures"], f"{name} must enable partial batch failures", errors)
        require("ScalingConfig" in props, f"{name} must set maximum event-source concurrency", errors)

    resource(resources, "SesConfigurationSet", "AWS::SES::ConfigurationSet", errors)
    resource(resources, "DatamailerDashboard", "AWS::CloudWatch::Dashboard", errors)

    alarm_metrics = {
        alarm.get("Properties", {}).get("MetricName")
        for alarm in resources.values()
        if alarm.get("Type") == "AWS::CloudWatch::Alarm"
    }
    missing_metrics = EXPECTED_ALARM_METRICS - alarm_metrics
    require(not missing_metrics, f"missing CloudWatch alarm metrics: {sorted(missing_metrics)}", errors)

    return errors


def validate_parameter_file(path):
    errors = []
    params = load_json(path)
    require(isinstance(params, list), f"{path} must be a CloudFormation parameter list", errors)
    values = {item.get("ParameterKey"): item.get("ParameterValue") for item in params if isinstance(item, dict)}
    env = values.get("EnvironmentName")
    require(env in {"staging", "production"}, f"{path} EnvironmentName must be staging or production", errors)
    require(values.get("DefaultFromEmail", "").startswith("no-reply@"), f"{path} DefaultFromEmail must be a no-reply sender", errors)
    require(values.get("PublicBaseUrl", "").startswith("https://"), f"{path} PublicBaseUrl must use HTTPS", errors)
    if env == "staging":
        require("staging" in values.get("DefaultFromEmail", ""), "staging sender must be environment-separated", errors)
        require("staging" in values.get("PublicBaseUrl", ""), "staging URL must be environment-separated", errors)
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate Datamailer infrastructure definitions without AWS credentials.")
    parser.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    parser.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    args = parser.parse_args()

    errors = validate_template(load_json(args.template))
    for path in sorted(args.config_dir.glob("*.parameters.example.json")):
        errors.extend(validate_parameter_file(path))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Validated {args.template} and parameter examples in {args.config_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
