from pathlib import Path

import yaml


TEMPLATE = Path("deploy/aws/shadow-missing-run-detector.yaml.example")
SCHEDULES = {
    "StartPresenceSchedule": ("cron(5 9 ? * MON-FRI *)", "start", "presence"),
    "StartClosureSchedule": ("cron(35 9 ? * MON-FRI *)", "start", "closure"),
    "StopPresenceSchedule": ("cron(50 15 ? * MON-FRI *)", "stop", "presence"),
    "StopClosureSchedule": ("cron(20 16 ? * MON-FRI *)", "stop", "closure"),
}


class _CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_unknown(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CloudFormationLoader.add_constructor(None, _construct_unknown)


def _template() -> dict[str, object]:
    value = yaml.load(
        TEMPLATE.read_text(encoding="utf-8"),
        Loader=_CloudFormationLoader,
    )
    assert isinstance(value, dict)
    return value


def test_detector_template_is_disabled_and_metrics_only_by_default():
    template = _template()
    parameters = template["Parameters"]
    assert parameters["EnableSchedules"]["Default"] == "false"
    assert parameters["AlertMode"]["Default"] == "metrics-only"
    assert template["Resources"]["DetectorFunction"]["Properties"][
        "ReservedConcurrentExecutions"
    ] == 1
    assert template["Resources"]["DetectorFunction"]["Properties"][
        "Timeout"
    ] == 10


def test_all_four_schedules_have_exact_timezone_and_disabled_condition():
    resources = _template()["Resources"]
    for name, (expression, schedule, phase) in SCHEDULES.items():
        resource = resources[name]
        properties = resource["Properties"]
        assert properties["ScheduleExpression"] == expression
        assert properties["ScheduleExpressionTimezone"] == "Asia/Seoul"
        assert properties["FlexibleTimeWindow"]["Mode"] == "OFF"
        assert properties["State"] == [
            "SchedulesEnabled", "ENABLED", "DISABLED",
        ]
        assert json_input(properties["Target"]["Input"]) == {
            "schedule": schedule,
            "phase": phase,
        }
        assert properties["Target"]["RetryPolicy"] == {
            "MaximumEventAgeInSeconds": 300,
            "MaximumRetryAttempts": 0,
        }


def json_input(value: object) -> dict[str, str]:
    import json

    assert isinstance(value, str)
    result = json.loads(value)
    assert isinstance(result, dict)
    return result


def test_iam_boundary_contains_no_ec2_ssm_or_broker_permissions():
    resources = _template()["Resources"]
    policies = [
        resources["DetectorRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ],
        resources["SchedulerRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ],
    ]
    actions: set[str] = set()
    for policy in policies:
        for statement in policy["Statement"]:
            if not isinstance(statement, dict):
                continue
            raw_actions = statement["Action"]
            values = (
                raw_actions
                if isinstance(raw_actions, list)
                else [raw_actions]
            )
            actions.update(value for value in values if isinstance(value, str))
    assert not any(
        action.startswith(("ec2:", "ssm:", "sqs:ReceiveMessage"))
        for action in actions
    )
    assert "lambda:InvokeFunction" in actions
    assert "dynamodb:PutItem" in actions
    assert "cloudwatch:PutMetricData" in actions


def test_secret_access_is_conditional_on_slack_mode():
    template = _template()
    assert template["Conditions"]["SlackEnabled"] == [
        "AlertMode", "slack",
    ]
    statements = template["Resources"]["DetectorRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    conditional = [
        statement for statement in statements if isinstance(statement, list)
    ]
    assert conditional == [[
        "SlackEnabled",
        {
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": "AlertSecretArn",
        },
        "AWS::NoValue",
    ]]


def test_template_has_no_runtime_recovery_or_order_operation():
    text = TEMPLATE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "workflow_dispatch", "ssm:", "ec2:", "place_order", "rerun",
    ):
        assert forbidden not in text
