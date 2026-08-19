from __future__ import annotations

import ast
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "deploy/check_shadow_ssm_contract.py"
ACTIVATION_WORKFLOW = Path(".github/workflows/cd-shadow-worker-activation.yml")
ROLLOUT_WORKFLOW = Path(".github/workflows/cd-shadow-worker-rollout.yml")
ACTIVATION_DOCUMENT = Path("deploy/ssm/shadow-worker-document.yaml")
ROLLOUT_DOCUMENT = Path("deploy/ssm/shadow-worker-rollout-document.yaml")
WORKER = Path("deploy/ec2/shadow_worker_control.sh")
VALIDATOR = Path("deploy/ec2/shadow_runtime_evidence.py")
ROLLOUT_EXECUTOR = Path("src/kiwoom_stock/deployment/shadow_rollout.py")
ROLLOUT_MIGRATION = Path("deploy/migrate_shadow_rollout_document.py")
MIGRATION_BOOTSTRAP = Path("deploy/bootstrap_shadow_rollout_migration.py")
MIGRATION_WORKFLOW = Path(".github/workflows/cd-shadow-rollout-document-migration.yml")
MIGRATION_TRUST = Path("deploy/iam/github-shadow-migration-trust.json.example")
MIGRATION_POLICY = Path("deploy/iam/github-shadow-migration-policy.json.example")
ROLLOUT_POLICY = Path("deploy/iam/github-shadow-rollout-policy.json.example")
CI_WORKFLOW = Path(".github/workflows/ci.yml")
CONTRACT_FILES = (
    ACTIVATION_WORKFLOW, ROLLOUT_WORKFLOW, ACTIVATION_DOCUMENT, ROLLOUT_DOCUMENT,
    WORKER, VALIDATOR, ROLLOUT_EXECUTOR, ROLLOUT_MIGRATION,
    MIGRATION_BOOTSTRAP, MIGRATION_WORKFLOW, MIGRATION_TRUST,
    MIGRATION_POLICY, ROLLOUT_POLICY, CI_WORKFLOW,
)


@pytest.fixture
def contract_root(tmp_path: Path) -> Path:
    for relative in CONTRACT_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return tmp_path


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def read(root: Path, path: Path) -> str:
    return (root / path).read_text(encoding="utf-8")


def replace_once(root: Path, path: Path, old: str, new: str) -> None:
    text = read(root, path)
    assert text.count(old) == 1
    (root / path).write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_in_step(
    root: Path, step_name: str, old: str, new: str,
) -> None:
    path = root / ACTIVATION_WORKFLOW
    text = path.read_text(encoding="utf-8")
    marker = f"      - name: {step_name}\n"
    start = text.index(marker)
    end = text.find("\n      - name:", start + len(marker))
    if end < 0:
        end = len(text)
    step = text[start:end]
    assert step.count(old) == 1
    text = text[:start] + step.replace(old, new, 1) + text[end:]
    path.write_text(text, encoding="utf-8")


def assert_failure(
    result: subprocess.CompletedProcess[str], exit_code: int, category: str,
) -> None:
    assert result.returncode == exit_code
    assert result.stdout == ""
    assert result.stderr == f"{'SETUP' if exit_code == 2 else 'FAIL'} category={category}\n"


def test_clean_contract_has_stable_two_unit_summary(contract_root: Path):
    result = run_checker(contract_root)

    assert result.returncode == 0
    assert result.stdout == (
        "PASS units=2 activation_parameters=13 rollout_parameters=8\n"
    )
    assert result.stderr == ""


def test_activation_notification_secret_contract_baseline(
    contract_root: Path,
):
    text = read(contract_root, ACTIVATION_WORKFLOW)

    assert text.count(
        "secrets.KIWOOM_SHADOW_SLACK_WEBHOOK_URL"
    ) == 2
    assert text.count("secrets.CONFIG_JSON") == 2
    assert "secrets.STRATEGY_CONFIG_JSON" not in text
    assert run_checker(contract_root).returncode == 0


def test_missing_legacy_notification_secret_fails_closed(
    contract_root: Path,
):
    replace_in_step(
        contract_root,
        "Preflight protected Slack status boundary",
        "          CONFIG_JSON: ${{ secrets.CONFIG_JSON }}\n",
        "",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.notification_secret",
    )


def test_renamed_legacy_notification_secret_fails_closed(
    contract_root: Path,
):
    replace_in_step(
        contract_root,
        "Notify protected shadow status",
        "          CONFIG_JSON: ${{ secrets.CONFIG_JSON }}\n",
        "          LEGACY_CONFIG_JSON: ${{ secrets.CONFIG_JSON }}\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.notification_secret",
    )


@pytest.mark.parametrize(
    ("step_name", "old", "new"),
    [
        (
            "Preflight protected Slack status boundary",
            "            --check-webhook\n",
            "            --check-webhook\n"
            "          printf '%s' '${{ secrets.CONFIG_JSON }}'\n",
        ),
        (
            "Upload bounded shadow evidence",
            "          retention-days: 14\n",
            "          retention-days: 14\n"
            "          legacy-config: ${{ secrets.CONFIG_JSON }}\n",
        ),
        (
            "Clear OIDC credentials before evidence upload",
            "        shell: bash\n",
            "        env:\n"
            "          CONFIG_JSON: ${{ secrets.CONFIG_JSON }}\n"
            "        shell: bash\n",
        ),
    ],
)
def test_extra_legacy_reference_outside_exact_env_fails_scope(
    contract_root: Path, step_name: str, old: str, new: str,
):
    replace_in_step(contract_root, step_name, old, new)

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.notification_secret_scope",
    )


@pytest.mark.parametrize(
    ("step_name", "old", "new"),
    [
        (
            "Execute bounded shadow action",
            "          PYTHONPATH: ${{ github.workspace }}/src\n",
            "          INDEXED_CONFIG_JSON: "
            "${{ secrets['CONFIG_JSON'] }}\n"
            "          PYTHONPATH: ${{ github.workspace }}/src\n",
        ),
        (
            "Clear OIDC credentials before evidence upload",
            "        run: |\n",
            "        run: |\n"
            "          printf '%s' '${{ secrets [ \"CONFIG_JSON\" ] }}'\n",
        ),
        (
            "Upload bounded shadow evidence",
            "          retention-days: 14\n",
            "          retention-days: 14\n"
            "          indexed-secret: ${{ secrets['OTHER_SECRET'] }}\n",
        ),
    ],
)
def test_indexed_secret_context_outside_slack_steps_fails_scope(
    contract_root: Path, step_name: str, old: str, new: str,
):
    replace_in_step(contract_root, step_name, old, new)

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.notification_secret_scope",
    )


def test_moved_legacy_reference_to_other_step_fails_closed(
    contract_root: Path,
):
    replace_in_step(
        contract_root,
        "Preflight protected Slack status boundary",
        "          CONFIG_JSON: ${{ secrets.CONFIG_JSON }}\n",
        "",
    )
    replace_in_step(
        contract_root,
        "Clear OIDC credentials before evidence upload",
        "        shell: bash\n",
        "        env:\n"
        "          CONFIG_JSON: ${{ secrets.CONFIG_JSON }}\n"
        "        shell: bash\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.notification_secret",
    )


def test_wrong_activation_instance_target_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '--instance-ids "${EC2_INSTANCE_ID}"',
        '--instance-ids "i-00000000000000000"',
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.workflow.send_flags"
    )


def test_wrong_activation_env_source_fails_closed(contract_root: Path):
    text = read(contract_root, ACTIVATION_WORKFLOW)
    execute = text.index("aws ssm send-command")
    source = text.rfind(
        "SOURCE_SHA: ${{ steps.resolve.outputs.source_sha }}", 0, execute
    )
    assert source >= 0
    text = text[:source] + text[source:].replace(
        "SOURCE_SHA: ${{ steps.resolve.outputs.source_sha }}",
        "SOURCE_SHA: ${{ steps.resolve.outputs.image_digest }}",
        1,
    )
    (contract_root / ACTIVATION_WORKFLOW).write_text(text, encoding="utf-8")

    assert_failure(
        run_checker(contract_root), 1, "activation.workflow.execute_env"
    )


def test_extra_activation_send_anywhere_in_workflow_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "          set -euo pipefail\n          [[ \"${GITHUB_REF}\" == refs/heads/main ]]",
        "          set -euo pipefail\n          aws ssm send-command --document-name extra\n"
        "          [[ \"${GITHUB_REF}\" == refs/heads/main ]]",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.aws_command_allowlist"
    )


@pytest.mark.parametrize(
    ("old", "new", "category"),
    [
        ('"action":"aws:runShellScript"', '"action":"aws:runPowerShellScript"',
         "activation.document.action"),
        ('"ExpectedValidatorSha256": {"type":"String","allowedPattern":"^[0-9a-f]{64}$","interpolationType":"ENV_VAR"}',
         '"ExpectedValidatorSha256": {"type":"String","interpolationType":"ENV_VAR"}',
         "activation.document.parameter_schema"),
    ],
)
def test_activation_document_action_or_constraint_drift_fails_closed(
    contract_root: Path, old: str, new: str, category: str,
):
    replace_once(contract_root, ACTIVATION_DOCUMENT, old, new)

    assert_failure(run_checker(contract_root), 1, category)


def test_actual_worker_cli_parser_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, WORKER,
        "--expected-validator-sha256) expected_validator_hash=",
        "--validator-sha256) expected_validator_hash=",
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.worker.parser_mapping"
    )


def test_activation_cancelling_must_not_be_terminal(contract_root: Path):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "Success|Failed|Cancelled|TimedOut)",
        "Success|Failed|Cancelled|TimedOut|Cancelling)",
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.workflow.terminal_statuses"
    )


def test_validator_must_require_exact_integer_zero(contract_root: Path):
    replace_once(
        contract_root, VALIDATOR,
        "if type(response_code) is not int or response_code != 0:",
        "if not response_code == 0:",
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.validator.ssm_result"
    )


def test_validator_dead_code_predicate_fails_closed(contract_root: Path):
    replace_once(
        contract_root, VALIDATOR,
        "if type(response_code) is not int or response_code != 0:",
        "if False and (type(response_code) is not int or response_code != 0):",
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.validator.ssm_result"
    )


@pytest.mark.parametrize(
    "command",
    [
        "aws iam create-role --role-name forbidden",
        'aws "${AWS_SERVICE}" dynamic-operation',
    ],
)
def test_activation_non_allowlisted_or_dynamic_aws_fails_closed(
    contract_root: Path, command: str,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "          set -euo pipefail\n"
        "          [[ \"${GITHUB_REF}\" == refs/heads/main ]]",
        f"          set -euo pipefail\n          {command}\n"
        "          [[ \"${GITHUB_REF}\" == refs/heads/main ]]",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.aws_command_allowlist",
    )


def test_activation_raw_aws_launcher_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "aws ssm send-command \\",
        "/usr/bin/aws ssm send-command \\",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.aws_command.launcher",
    )


def test_activation_variable_aws_launcher_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "          set -euo pipefail\n"
        "          # codex-cd-ssm-document:",
        "          set -euo pipefail\n"
        "          AWS_CLI=/usr/bin/aws; export AWS_CLI\n"
        '          "${AWS_CLI}" iam create-role --role-name forbidden\n'
        "          # codex-cd-ssm-document:",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.dynamic_aws_launcher",
    )


def test_activation_document_version_must_come_from_attestation(
    contract_root: Path,
):
    text = read(contract_root, ACTIVATION_WORKFLOW)
    line = next(
        item for item in text.splitlines()
        if 'document_version="$(python3 -c' in item
    )
    replace_once(
        contract_root, ACTIVATION_WORKFLOW, line,
        "          document_version=1",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.document_attestation",
    )


def test_activation_document_version_reaching_overwrite_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '          [[ "${document_version}" =~ ^[1-9][0-9]*$ ]]\n',
        '          [[ "${document_version}" =~ ^[1-9][0-9]*$ ]]\n'
        "          document_version=1\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.document_attestation",
    )


def test_activation_document_version_printf_overwrite_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '          [[ "${document_version}" =~ ^[1-9][0-9]*$ ]]\n',
        '          [[ "${document_version}" =~ ^[1-9][0-9]*$ ]]\n'
        "          printf -v document_version '%s' 1\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.document_attestation",
    )


def test_activation_same_line_printf_overwrite_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '          [[ "${document_version}" =~ ^[1-9][0-9]*$ ]]\n',
        '          [[ "${document_version}" =~ ^[1-9][0-9]*$ ]]\n'
        "          :; printf -v document_version '%s' 1\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.document_attestation",
    )


def test_activation_validator_source_binding_drift_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '--source-sha "${SOURCE_SHA}" --image-digest "${IMAGE_DIGEST}"',
        '--source-sha "${IMAGE_DIGEST}" --image-digest "${IMAGE_DIGEST}"',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.validator_wiring",
    )


def test_activation_validator_stdin_binding_drift_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '<"${invocation_file}")"',
        '<"${RUNNER_TEMP}/other.json")"',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.workflow.validator_wiring",
    )


def test_worker_flag_destination_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, WORKER,
        '--expected-validator-sha256) expected_validator_hash="${2:-}"; shift 2 ;;',
        '--expected-validator-sha256) expected_worker_hash="${2:-}"; shift 2 ;;',
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.worker.parser_mapping"
    )


def test_worker_stop_compose_required_guard_fails_closed(contract_root: Path):
    replace_once(
        contract_root, WORKER,
        '        [[ -z "${compose_hash}" ]] || fail "stop does not accept a Compose hash"',
        '        [[ -z "${compose_hash}" ]] || fail "stop does not accept a Compose hash"\n'
        '        [[ -n "${compose_hash}" ]] || fail "stop requires a Compose hash"',
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.worker.mode_guards"
    )


def test_worker_prebranch_compose_validation_fails_closed(contract_root: Path):
    replace_once(
        contract_root, WORKER,
        '    if [[ "${desired_state}" == stop ]]; then\n',
        '    validate_hash "${compose_hash}"\n'
        '    if [[ "${desired_state}" == stop ]]; then\n',
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.worker.mode_guards"
    )


def test_rollout_workflow_executor_argv_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_WORKFLOW,
        '--rollout-attempt-id "${ROLLOUT_ATTEMPT_ID}"',
        '--rollout-attempt-id "${SOURCE_SHA}"',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.workflow.executor_flags"
    )


def test_rollout_executor_send_target_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        'INSTANCE_ID = "i-0e42e09d6c087ba29"',
        'INSTANCE_ID = "i-00000000000000000"',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.fixed_target"
    )


def test_rollout_executor_fixed_identity_category_drift_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        '"fixed-identity:lifecycle": "host_fixed_identity_lifecycle"',
        '"fixed-identity:lifecycle": "unsafe_raw_lifecycle"',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.executor.fixed_identity_categories",
    )


def test_rollout_executor_extra_send_site_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        'response = self.call([\n                "ssm", "send-command",',
        'self.call(["ssm", "send-command"], write=True)\n'
        '        response = self.call([\n                "ssm", "send-command",',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.call_allowlist"
    )


@pytest.mark.parametrize(
    ("old", "new", "category"),
    [
        (
            '"--version-name", version_name, "--content", source_text',
            '"--version-name", version_name, "--content", "file:///tmp/mutable"',
            "rollout.migration.positive_contract",
        ),
        (
            '        submits["update"] = 1\n        try:\n            journal.update(\n                "update_submitting",',
            '        submits["update"] = 1\n        try:\n            journal.update(\n                "update_ready",',
            "rollout.migration.operation_class",
        ),
        (
            '"status", "--porcelain", "--untracked-files=all"',
            '"status", "--short"',
            "rollout.migration.positive_contract",
        ),
        (
            'command == ("sts", "get-caller-identity")',
            'command == ("sts", "get-session-token")',
            "rollout.migration.positive_contract",
        ),
    ],
)
def test_rollout_migration_positive_and_negative_contracts_fail_closed(
    contract_root: Path, old: str, new: str, category: str,
):
    replace_once(contract_root, ROLLOUT_MIGRATION, old, new)
    assert_failure(run_checker(contract_root), 1, category)


def test_migration_workflow_concurrency_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, MIGRATION_WORKFLOW,
        "  group: kiwoom-stock-shadow-i-0e42e09d6c087ba29",
        "  group: unsafe-independent-migration",
    )
    assert_failure(run_checker(contract_root), 1, "migration.workflow.concurrency")


def test_migration_workflow_top_level_runner_context_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, MIGRATION_WORKFLOW,
        "  AWS_REGION: ap-northeast-2",
        "  AWS_REGION: ap-northeast-2\n"
        "  AUDIT_PATH: ${{ runner.temp }}/unsafe.json",
    )
    assert_failure(run_checker(contract_root), 1, "migration.workflow.forbidden")


def test_migration_iam_forbidden_send_command_fails_closed(contract_root: Path):
    replace_once(
        contract_root, MIGRATION_POLICY,
        '"ssm:UpdateDocumentDefaultVersion"',
        '"ssm:UpdateDocumentDefaultVersion","ssm:SendCommand"',
    )
    assert_failure(run_checker(contract_root), 1, "migration.iam.forbidden")


def test_migration_bootstrap_overwrite_authority_fails_closed(contract_root: Path):
    replace_once(
        contract_root, MIGRATION_BOOTSTRAP,
        '"iam", "create-role"',
        '"iam", "update-assume-role-policy"',
    )
    assert_failure(run_checker(contract_root), 1, "migration.bootstrap.create_only")


def test_rollout_executor_extra_iam_call_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        'response = self.call([\n                "ssm", "send-command",',
        'self.call(["iam", "create-role"], write=True)\n'
        '        response = self.call([\n                "ssm", "send-command",',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.call_allowlist"
    )


def test_migration_adapter_approved_content_binding_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        '"--version-name", approved_version_name, "--content", approved_content',
        '"--version-name", approved_version_name, "--content", "arbitrary"',
    )
    assert_failure(
        run_checker(contract_root), 1, "rollout.migration.positive_contract"
    )


def test_migration_adapter_constructor_binding_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        "        approved_content: str,\n        approved_version_name: str,",
        "        approved_version_name: str,",
    )
    assert_failure(
        run_checker(contract_root), 1, "rollout.migration.adapter_contract"
    )


def test_migration_session_in_stable_contract_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        '        "account": account,',
        '        "account": account, "session": "unstable",',
    )
    assert_failure(
        run_checker(contract_root), 1, "rollout.migration.stable_contract"
    )


def test_migration_update_terminal_budget_spoof_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        '            ], operation="primary")\n            response_version =',
        '            ], operation="terminal")\n            response_version =',
    )
    assert_failure(
        run_checker(contract_root), 1, "rollout.migration.operation_class"
    )


@pytest.mark.parametrize(
    "literal",
    ["cutover_uncertain_no_cas"],
)
def test_migration_uncertain_cas_literals_are_independently_required(
    contract_root: Path, literal: str,
):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        f'"{literal}"', f'"{literal}_removed"',
    )
    assert_failure(
        run_checker(contract_root), 1, "rollout.migration.positive_contract"
    )


def test_migration_checker_keeps_uncertain_cas_literals_as_distinct_items():
    tree = ast.parse(CHECKER.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_verify_rollout_migration"
    )
    assignment = next(
        node for node in function.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "required_literals"
            for target in node.targets
        )
    )
    required_literals = ast.literal_eval(assignment.value)
    assert required_literals.count('"cutover_uncertain_no_cas"') == 1
    assert required_literals.count('status="MANUAL_HOLD"') == 1
    assert not any("rollback" in literal for literal in required_literals)


def test_migration_failed_safe_current_actor_update_is_required(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        "        actor_last=actor,\n    )\n"
        "    phase_hook(\"failed_safe_release\")",
        "        actor_last=\"0\" * 64,\n    )\n"
        "    phase_hook(\"failed_safe_release\")",
    )
    assert_failure(
        run_checker(contract_root), 1,
        "rollout.migration.failed_safe_actor_audit",
    )


def test_migration_journal_post_lock_reread_is_required(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_MIGRATION,
        "    _put_lock(aws, lock_value)\n"
        "    journal = RemoteJournal.open(aws, journal_name)",
        "    _put_lock(aws, lock_value)\n"
        "    journal = journal",
    )
    assert_failure(
        run_checker(contract_root), 1, "rollout.migration.journal_first"
    )


def test_rollout_executor_markerless_iam_call_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        'response = self.call([\n                "ssm", "send-command",',
        'self.call(["iam", "create-role"])\n'
        '        response = self.call([\n                "ssm", "send-command",',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.call_allowlist"
    )


def test_rollout_executor_write_keyword_omission_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        '            ], write=True)\n        command_id = self._send_response_command_id(',
        '            ])\n        command_id = self._send_response_command_id(',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.call_allowlist"
    )


def test_rollout_executor_direct_aws_subprocess_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        '        described = aws.call(["ssm", "describe-document", "--name", SHADOW_DOCUMENT])',
        '        subprocess.run(["aws", "iam", "create-role"])\n'
        '        described = aws.call(["ssm", "describe-document", "--name", SHADOW_DOCUMENT])',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.executor.process_authority",
    )


def test_rollout_executor_subprocess_alias_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        '        described = aws.call(["ssm", "describe-document", "--name", SHADOW_DOCUMENT])',
        "        run_aws = subprocess.run\n"
        '        run_aws(["aws", "ssm", "update-document-default-version", '
        '"--name", SHADOW_DOCUMENT, "--document-version", "1"])\n'
        '        described = aws.call(["ssm", "describe-document", "--name", SHADOW_DOCUMENT])',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.executor.process_authority",
    )


def test_rollout_executor_module_level_aws_subprocess_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        "import subprocess\n",
        "import subprocess\n"
        'subprocess.run(["aws", "iam", "create-role"])\n',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.executor.process_authority",
    )


def test_rollout_executor_subprocess_final_binding_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        "import subprocess\n",
        "import subprocess\n\n"
        "class _NoProcess:\n"
        "    TimeoutExpired = TimeoutError\n\n"
        "    @staticmethod\n"
        "    def run(*args, **kwargs):\n"
        "        return type(\n"
        "            '_Result', (),\n"
        "            {'returncode': 0, 'stdout': '{}', 'stderr': ''},\n"
        "        )()\n\n"
        "subprocess = _NoProcess\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.executor.process_authority",
    )


def test_rollout_executor_document_write_flag_drift_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        '"ssm", "update-document", "--name", SHADOW_DOCUMENT,',
        '"ssm", "delete-document", "--name", SHADOW_DOCUMENT,',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.call_allowlist"
    )


def test_rollout_executor_dynamic_write_command_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        'aws.call([\n            "ssm", "update-document-default-version", "--name", SHADOW_DOCUMENT,',
        'aws.call([\n            *dynamic_command, "--name", SHADOW_DOCUMENT,',
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.call_allowlist"
    )


def test_rollout_document_schema_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        "  - action: aws:runShellScript",
        "  - action: aws:runPowerShellScript",
    )

    assert_failure(run_checker(contract_root), 1, "rollout.document.action")


def test_rollout_document_artifact_target_drift_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        "validator_target=/usr/local/libexec/kiwoom-shadow-runtime-evidence.py",
        "validator_target=/tmp/kiwoom-shadow-runtime-evidence.py",
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.document.artifact_wiring"
    )


def test_rollout_document_fixed_identity_marker_drift_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        'reject("lifecycle")', 'reject("raw_docker_state")',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.fixed_container_recovery",
    )


@pytest.mark.parametrize(("replacement", "category"), [
    ('reject("raw-docker-state")', "rollout.document.fixed_container_recovery"),
    ('reject("lifecycle" + "")', "rollout.document.fixed_container_recovery"),
    ('reject("lifecycle"); reject("lifecycle")',
     "rollout.document.fixed_container_recovery"),
    ('reject("lifecycle"); print("fixed-identity:rogue")',
     "rollout.document.capability_contract"),
])
def test_rollout_document_nonliteral_or_duplicate_identity_reject_fails_closed(
    contract_root: Path, replacement: str, category: str,
):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        'reject("lifecycle")', replacement,
    )

    assert_failure(
        run_checker(contract_root), 1, category,
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("len(cap_drop)!=1", "len(cap_drop)<1"),
        ("len(cap_add)!=3", "len(cap_add)<3"),
        (
            '{"CAP_CHOWN","CAP_SETGID","CAP_SETUID"}',
            '{"CHOWN","CAP_SETGID","CAP_SETUID"}',
        ),
        (
            "set(cap_add) not in (legacy_cap_add,canonical_cap_add)",
            "set(cap_add)!=legacy_cap_add",
        ),
    ],
)
def test_rollout_document_capability_invariant_drift_fails_closed(
    contract_root: Path, old: str, new: str,
):
    replace_once(contract_root, ROLLOUT_DOCUMENT, old, new)

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.capability_contract",
    )


@pytest.mark.parametrize("insertion", [
    'host["CapAdd"]=["CHOWN","SETGID","SETUID"]',
    "raise SystemExit(0)",
])
def test_rollout_document_capability_authority_insertion_fails_closed(
    contract_root: Path, insertion: str,
):
    baseline = run_checker(contract_root)
    assert baseline.returncode == 0

    capability_read = (
        '          cap_drop=host.get("CapDrop"); cap_add=host.get("CapAdd")'
    )
    replace_once(
        contract_root, ROLLOUT_DOCUMENT, capability_read,
        f"          {insertion}\n{capability_read}",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.capability_contract",
    )


def test_rollout_document_decoy_identity_heredoc_fails_closed(
    contract_root: Path,
):
    baseline = run_checker(contract_root)
    assert baseline.returncode == 0

    text = read(contract_root, ROLLOUT_DOCUMENT)
    guard_start_marker = (
        '            if ! python3 - "$snapshot" "$binding" '
        '"$worker_target" "$validator_target" <<\'PY\'\n'
    )
    guard_end_marker = '            docker rm -- "$fixed_container"'
    guard_start = text.index(guard_start_marker)
    guard_end = text.index(guard_end_marker, guard_start)
    normal_guard = text[guard_start:guard_end]
    guard_lines = normal_guard.splitlines(keepends=True)
    identity_end = guard_lines.index("          PY\n")
    bypassed_guard = "".join(
        guard_lines[:1]
        + ["          raise SystemExit(0)\n"]
        + guard_lines[identity_end:]
    )
    decoy_and_bypassed_guard = (
        "            if false; then\n"
        f"{normal_guard}"
        "            fi\n"
        f"{bypassed_guard}"
    )
    assert text.count(normal_guard) == 1
    (contract_root / ROLLOUT_DOCUMENT).write_text(
        text.replace(normal_guard, decoy_and_bypassed_guard, 1),
        encoding="utf-8",
    )

    document = yaml.safe_load(read(contract_root, ROLLOUT_DOCUMENT))
    command = document["mainSteps"][0]["inputs"]["runCommand"][0]
    syntax = subprocess.run(
        ["bash", "-n"], input=command, check=False,
        capture_output=True, text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.capability_contract",
    )


@pytest.mark.parametrize(("old", "new"), [
    (
        "          prepare_fixed_container_for_install() {",
        "          prepare_fixed_container_for_install() {\n"
        "          prepare_fixed_container_for_install() {",
    ),
    (
        '          if [[ "$action" == install ]]; then '
        "prepare_fixed_container_for_install; fi",
        '          if [[ "$action" == install ]]; then '
        "prepare_fixed_container_for_install; fi\n"
        '          if [[ "$action" == install ]]; then '
        "prepare_fixed_container_for_install; fi",
    ),
    (
        '          }\n          if [[ "$action" == install ]]; then '
        "prepare_fixed_container_for_install; fi",
        '          }\n          :\n          if [[ "$action" == install ]]; then '
        "prepare_fixed_container_for_install; fi",
    ),
])
def test_rollout_document_recovery_envelope_boundary_drift_fails_closed(
    contract_root: Path, old: str, new: str,
):
    baseline = run_checker(contract_root)
    assert baseline.returncode == 0

    replace_once(contract_root, ROLLOUT_DOCUMENT, old, new)

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.capability_contract",
    )


def test_rollout_document_python_command_shadow_fails_closed(
    contract_root: Path,
):
    baseline = run_checker(contract_root)
    assert baseline.returncode == 0

    function_header = "          prepare_fixed_container_for_install() {"
    python_shadow = (
        '          python3() { if [[ "$1" == - && "$#" -eq 5 ]]; then '
        'return 0; fi; command python3 "$@"; }'
    )
    replace_once(
        contract_root, ROLLOUT_DOCUMENT, function_header,
        f"{python_shadow}\n{function_header}",
    )

    document = yaml.safe_load(read(contract_root, ROLLOUT_DOCUMENT))
    command = document["mainSteps"][0]["inputs"]["runCommand"][0]
    syntax = subprocess.run(
        ["bash", "-n"], input=command, check=False,
        capture_output=True, text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.capability_contract",
    )


def test_rollout_publish_expected_hash_guard_removal_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        '            [[ "$(sha256sum "$temporary" | cut -d\' \' -f1)" == "$expected" ]]',
        "            : # removed expected hash guard",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.publish_hash_guard",
    )


def test_rollout_worker_hash_reassignment_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        '            [[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
        '            worker_sha="$(sha256sum "$downloaded" | cut -d\' \' -f1)"\n'
        '            [[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.trusted_input_assignment",
    )


def test_rollout_worker_hash_printf_overwrite_fails_closed(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        '            [[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
        '            printf -v worker_sha \'%s\' "$(sha256sum "$downloaded" | cut -d\' \' -f1)"\n'
        '            [[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.trusted_input_assignment",
    )


def test_rollout_worker_hash_same_line_printf_overwrite_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, ROLLOUT_DOCUMENT,
        '            [[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
        '            :; printf -v worker_sha \'%s\' '
        '"$(sha256sum "$downloaded" | cut -d\' \' -f1)"\n'
        '            [[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
    )

    assert_failure(
        run_checker(contract_root), 1,
        "rollout.document.trusted_input_assignment",
    )


def test_duplicate_permissive_validator_authority_fails_closed(
    contract_root: Path,
):
    validator = read(contract_root, VALIDATOR)
    validator += (
        "\n\ndef _records(content: str, input_format: str):\n"
        "    return _json_lines(content), None\n"
    )
    (contract_root / VALIDATOR).write_text(validator, encoding="utf-8")

    assert_failure(
        run_checker(contract_root), 1,
        "activation.validator.duplicate_function",
    )


def test_permissive_validator_final_lambda_binding_fails_closed(
    contract_root: Path,
):
    replace_once(
        contract_root, VALIDATOR,
        "\ndef _aware_iso_or_none(value: object) -> bool:\n",
        "\n_records = lambda content, input_format: (\n"
        "    _json_lines(_strict_json_loads(\n"
        "        content, 'invocation_json_invalid'\n"
        "    )['StandardOutputContent']), None\n"
        ")\n\n"
        "def _aware_iso_or_none(value: object) -> bool:\n",
    )

    assert_failure(
        run_checker(contract_root), 1,
        "activation.validator.authority_binding",
    )


@pytest.mark.parametrize("value", ["null", "7"])
def test_parseable_unrelated_run_type_has_stable_error(
    contract_root: Path, value: str,
):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "    steps:\n",
        "    steps:\n"
        "      - name: malformed but parseable\n"
        f"        run: {value}\n",
    )

    assert_failure(
        run_checker(contract_root), 1, "activation.workflow.run_type"
    )


def test_parseable_scalar_steps_has_stable_error(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_WORKFLOW,
        "    steps:\n",
        "    steps: scalar\n    ignored_steps:\n",
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.workflow.steps_shape"
    )


def test_nonliteral_rollout_parameter_key_has_stable_error(contract_root: Path):
    replace_once(
        contract_root, ROLLOUT_EXECUTOR,
        '        "Action": [action],',
        "        action: [action],",
    )

    assert_failure(
        run_checker(contract_root), 1, "rollout.executor.parameter_key"
    )


@pytest.mark.parametrize(
    ("path", "needle", "category"),
    [
        (ACTIVATION_WORKFLOW, "env:\n  AWS_REGION:", "activation.workflow.yaml.duplicate_key"),
        (ROLLOUT_DOCUMENT, 'schemaVersion: "2.2"', "rollout.document.yaml.duplicate_key"),
    ],
)
def test_duplicate_yaml_key_is_setup_error(
    contract_root: Path, path: Path, needle: str, category: str,
):
    replacement = (
        "env:\n  AWS_REGION: ap-northeast-2\n  AWS_REGION:"
        if path == ACTIVATION_WORKFLOW else
        'schemaVersion: "2.2"\nschemaVersion: "2.2"'
    )
    replace_once(contract_root, path, needle, replacement)

    assert_failure(run_checker(contract_root), 2, category)


def test_malformed_yaml_is_setup_error(contract_root: Path):
    replace_once(contract_root, ROLLOUT_WORKFLOW, "jobs:\n", "jobs: [\n")

    assert_failure(
        run_checker(contract_root), 2, "rollout.workflow.yaml_malformed"
    )


def test_harmless_workflow_step_rename_is_accepted(contract_root: Path):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        "- name: Execute bounded shadow action",
        "- name: Run the bounded shadow action",
    )

    assert run_checker(contract_root).returncode == 0


def test_equivalent_flag_equals_syntax_is_accepted(contract_root: Path):
    replace_once(
        contract_root, ACTIVATION_WORKFLOW,
        '--document-name "${SHADOW_DOCUMENT_NAME}"',
        '--document-name="${SHADOW_DOCUMENT_NAME}"',
    )

    assert run_checker(contract_root).returncode == 0


def test_harmless_document_yaml_formatting_is_accepted(contract_root: Path):
    path = contract_root / ACTIVATION_DOCUMENT
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    assert run_checker(contract_root).returncode == 0


def test_ci_checker_must_precede_quality_gates(contract_root: Path):
    text = read(contract_root, CI_WORKFLOW)
    checker = text.index("      - name: Verify authoritative shadow SSM contracts")
    lint = text.index("      - name: Critical lint")
    checker_block = text[checker:lint]
    text = text[:checker] + text[lint:]
    lint = text.index("      - name: Critical lint")
    type_check = text.index("      - name: Type check")
    text = text[:type_check] + checker_block + text[type_check:]
    (contract_root / CI_WORKFLOW).write_text(text, encoding="utf-8")

    assert_failure(run_checker(contract_root), 1, "ci.checker_order")


def test_ci_commented_checker_command_fails_closed(contract_root: Path):
    replace_once(
        contract_root, CI_WORKFLOW,
        "          python deploy/check_shadow_ssm_contract.py\n",
        "          # python deploy/check_shadow_ssm_contract.py\n",
    )

    assert_failure(run_checker(contract_root), 1, "ci.checker_count")


def test_ci_build_before_checker_fails_closed(contract_root: Path):
    replace_once(
        contract_root, CI_WORKFLOW,
        '          python -m pip install --no-deps --no-build-isolation -e .\n\n'
        "      - name: Verify authoritative shadow SSM contracts",
        '          python -m pip install --no-deps --no-build-isolation -e .\n'
        "          python -m build\n\n"
        "      - name: Verify authoritative shadow SSM contracts",
    )

    assert_failure(run_checker(contract_root), 1, "ci.build_before_checker")


def test_ci_independent_sibling_build_fails_closed(contract_root: Path):
    path = contract_root / CI_WORKFLOW
    text = path.read_text(encoding="utf-8")
    text += (
        "\n  sibling-build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Independent package build\n"
        "        run: python -m build\n"
    )
    path.write_text(text, encoding="utf-8")

    assert_failure(
        run_checker(contract_root), 1,
        "ci.build_without_quality_dependency",
    )


@pytest.mark.parametrize(
    "build_command",
    [
        "command python -m build",
        "env PYTHONHASHSEED=0 python -m build",
        "/usr/bin/python3 -m build",
    ],
)
def test_ci_normalized_build_launcher_before_checker_fails_closed(
    contract_root: Path, build_command: str,
):
    replace_once(
        contract_root, CI_WORKFLOW,
        '          python -m pip install --no-deps --no-build-isolation -e .\n\n'
        "      - name: Verify authoritative shadow SSM contracts",
        '          python -m pip install --no-deps --no-build-isolation -e .\n'
        f"          {build_command}\n\n"
        "      - name: Verify authoritative shadow SSM contracts",
    )

    assert_failure(run_checker(contract_root), 1, "ci.build_before_checker")
