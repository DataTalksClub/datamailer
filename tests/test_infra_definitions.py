import json
from pathlib import Path

from scripts.validate_infra import validate_parameter_file, validate_template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "datamailer-mvp.json"


def load_json(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def worker_role(resources, worker_name):
    role_name = resources[worker_name]["Properties"]["Role"]["Fn::GetAtt"][0]
    return resources[role_name]


def policy_statements(role):
    statements = []
    for policy in role["Properties"].get("Policies", []):
        statements.extend(policy["PolicyDocument"].get("Statement", []))
    return statements


def statement_actions(statement):
    actions = statement.get("Action", [])
    return set(actions if isinstance(actions, list) else [actions])


def statement_resources(role, action_prefix):
    resources = []
    for statement in policy_statements(role):
        if any(action.startswith(action_prefix) for action in statement_actions(statement)):
            resource = statement.get("Resource")
            resources.extend(resource if isinstance(resource, list) else [resource])
    return resources


def test_infra_template_contract():
    assert validate_template(load_json(TEMPLATE)) == []


def test_parameter_examples_are_environment_separated():
    parameter_files = sorted((ROOT / "infra" / "config").glob("*.parameters.example.json"))
    assert parameter_files
    for path in parameter_files:
        assert validate_parameter_file(path) == []


def test_transactional_and_campaign_queues_are_independent():
    resources = load_json(TEMPLATE)["Resources"]
    transactional_queue = resources["TransactionalEmailQueue"]
    campaign_queue = resources["CampaignEmailQueue"]
    assert transactional_queue["Properties"]["QueueName"] != campaign_queue["Properties"]["QueueName"]
    assert transactional_queue["Properties"]["RedrivePolicy"] != campaign_queue["Properties"]["RedrivePolicy"]


def test_ses_webhook_event_source_is_enabled_and_email_events_stays_optional():
    resources = load_json(TEMPLATE)["Resources"]
    ses_mapping = resources["SesWebhooksEventSourceMapping"]
    email_events_mapping = resources["EmailEventsEventSourceMapping"]
    worker = resources["SesWebhooksWorker"]

    assert ses_mapping["Properties"].get("Enabled", True) is True
    assert email_events_mapping["Properties"]["Enabled"] is False
    assert {"Key": "DeferredUntilIssue", "Value": "11"} not in worker["Properties"]["Tags"]


def test_lambda_workers_have_distinct_scoped_roles():
    resources = load_json(TEMPLATE)["Resources"]
    worker_scope = {
        "TransactionalEmailWorker": ("TransactionalEmailQueue", "TransactionalEmailDLQ", True),
        "CampaignEmailWorker": ("CampaignEmailQueue", "CampaignEmailDLQ", True),
        "SesWebhooksWorker": ("SesWebhooksQueue", "SesWebhooksDLQ", False),
        "EmailEventsWorker": ("EmailEventsQueue", "EmailEventsDLQ", False),
    }
    role_names = [resources[worker]["Properties"]["Role"]["Fn::GetAtt"][0] for worker in worker_scope]
    assert len(set(role_names)) == len(worker_scope)
    assert "WorkerRuntimeRole" not in resources

    for worker, (queue, dlq, can_send_email) in worker_scope.items():
        role = worker_role(resources, worker)
        allowed_sqs_resources = [
            {"Fn::GetAtt": [queue, "Arn"]},
            {"Fn::GetAtt": [dlq, "Arn"]},
        ]
        assert statement_resources(role, "sqs:") == allowed_sqs_resources
        has_ses = any(action.startswith("ses:") for statement in policy_statements(role) for action in statement_actions(statement))
        assert has_ses is can_send_email
        assert role["Properties"].get("ManagedPolicyArns", []) == []
        assert any(
            {
                "ec2:CreateNetworkInterface",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeSubnets",
                "ec2:DeleteNetworkInterface",
            }.issubset(statement_actions(statement))
            and statement.get("Resource") == "*"
            for statement in policy_statements(role)
        )


def test_ses_sender_workers_have_stack_level_rate_controls():
    template = load_json(TEMPLATE)
    parameters = template["Parameters"]
    resources = template["Resources"]
    assert parameters["SESSendRatePerSecond"]["Default"] == 6
    assert parameters["SESSendRatePerSecond"]["MaxValue"] == 14

    sender_workers = ["TransactionalEmailWorker", "CampaignEmailWorker"]
    for worker in sender_workers:
        props = resources[worker]["Properties"]
        assert props["ReservedConcurrentExecutions"] == 1
        assert props["Environment"]["Variables"]["DATAMAILER_SES_MAX_SEND_RATE"] == {"Ref": "SESSendRatePerSecond"}

    assert resources["TransactionalEmailEventSourceMapping"]["Properties"]["ScalingConfig"]["MaximumConcurrency"] == 1
    assert resources["CampaignEmailEventSourceMapping"]["Properties"]["ScalingConfig"]["MaximumConcurrency"] == 1


def test_lambda_log_groups_have_retention_and_scoped_role_permissions():
    resources = load_json(TEMPLATE)["Resources"]
    expected = {
        "TransactionalEmailWorker": ("TransactionalEmailWorkerLogGroup", "transactional-email"),
        "CampaignEmailWorker": ("CampaignEmailWorkerLogGroup", "campaign-email"),
        "SesWebhooksWorker": ("SesWebhooksWorkerLogGroup", "ses-webhooks"),
        "EmailEventsWorker": ("EmailEventsWorkerLogGroup", "email-events"),
    }

    for worker, (log_group_name, suffix) in expected.items():
        log_group = resources[log_group_name]
        assert log_group["Type"] == "AWS::Logs::LogGroup"
        assert log_group["Properties"]["RetentionInDays"] == {"Ref": "LambdaLogRetentionDays"}
        assert log_group["Properties"]["LogGroupName"] == {
            "Fn::Sub": f"/aws/lambda/${{ProjectName}}-${{EnvironmentName}}-{suffix}"
        }
        assert log_group_name in resources[worker]["DependsOn"]

        log_resources = statement_resources(worker_role(resources, worker), "logs:")
        assert log_resources == [
            {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:"
                    f"log-group:/aws/lambda/${{ProjectName}}-${{EnvironmentName}}-{suffix}:*"
                )
            }
        ]
