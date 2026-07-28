"""Acceptance checks for the installed reporting and resource closure."""

import importlib
from importlib import resources
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROMPT_NAMES = (
    "daily_postmortem_system.md",
    "daily_postmortem_user.md",
)


@pytest.mark.parametrize("prompt_name", PROMPT_NAMES)
def test_packaged_prompt_bytes_match_legacy_root_resource(prompt_name):
    packaged = resources.files("kiwoom_stock.resources.prompts").joinpath(prompt_name)
    legacy = REPOSITORY_ROOT / "prompt" / prompt_name

    assert packaged.read_bytes() == legacy.read_bytes()


def test_root_tool_callables_delegate_with_legacy_patch_points(monkeypatch):
    chart_wrapper = importlib.import_module("tools.extract_1min_chart")
    trade_wrapper = importlib.import_module("tools.trade_validator")
    calls = []

    def extract(target_date, **dependencies):
        calls.append(("chart", target_date, dependencies))
        return ["minute.csv"]

    def analyze(target_date, **dependencies):
        calls.append(("trade", target_date, dependencies))
        return "analysis.csv"

    monkeypatch.setattr(chart_wrapper, "_extract_and_save_1min_chart", extract)
    monkeypatch.setattr(trade_wrapper, "_analyze_trade_efficiency", analyze)

    assert chart_wrapper.extract_and_save_1min_chart("2026-07-18") == ["minute.csv"]
    assert trade_wrapper.analyze_trade_efficiency("2026-07-18") == "analysis.csv"

    chart_dependencies = calls[0][2]
    trade_dependencies = calls[1][2]
    assert chart_dependencies == {
        "config_module": chart_wrapper.config,
        "datetime_type": chart_wrapper.datetime,
        "client_factory": chart_wrapper.KiwoomClient,
        "collector_factory": chart_wrapper.MarketDataCollector,
        "database_factory": chart_wrapper.TradeLogger,
        "target_logger": chart_wrapper.logger,
    }
    assert trade_dependencies == {
        "config_module": trade_wrapper.config,
        "datetime_type": trade_wrapper.datetime,
        "database_factory": trade_wrapper.TradeLogger,
        "target_logger": trade_wrapper.logger,
    }


@pytest.mark.parametrize(
    ("relative_path", "module_name", "implementation_name"),
    [
        (
            "tools/extract_1min_chart.py",
            "kiwoom_stock.reporting.minute_chart",
            "_extract_and_save_1min_chart",
        ),
        (
            "tools/trade_validator.py",
            "kiwoom_stock.reporting.trade_analysis",
            "_analyze_trade_efficiency",
        ),
    ],
)
def test_root_tool_cli_surfaces_still_invoke_the_packaged_callable(
    monkeypatch,
    relative_path,
    module_name,
    implementation_name,
):
    calls = []

    def implementation(target_date, **_dependencies):
        calls.append(target_date)
        return []

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, implementation_name, implementation)

    runpy.run_path(str(REPOSITORY_ROOT / relative_path), run_name="__main__")

    assert calls == [None]


def test_source_package_all_modules_and_resources_close_outside_repository(tmp_path):
    source_root = REPOSITORY_ROOT / "src"
    script = r'''
import importlib
from importlib import resources
import importlib.util
from pathlib import Path
import pkgutil
import sys

import kiwoom_stock

repository_root = Path(__import__("os").environ["A0_REPOSITORY_ROOT"]).resolve()
assert str(repository_root) not in sys.path
assert importlib.util.find_spec("tools") is None

importlib.import_module("kiwoom_stock.application.lifecycle")
importlib.import_module("kiwoom_stock.monitoring.reporter")

module_names = sorted(
    module.name
    for module in pkgutil.walk_packages(kiwoom_stock.__path__, kiwoom_stock.__name__ + ".")
)
assert "kiwoom_stock.application.lifecycle" in module_names
assert "kiwoom_stock.monitoring.reporter" in module_names
for module_name in module_names:
    if module_name != "kiwoom_stock.__main__":
        importlib.import_module(module_name)

sys.argv = ["kiwoom_stock"]
try:
    importlib.import_module("kiwoom_stock.__main__")
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("kiwoom_stock.__main__ must terminate through its CLI")

prompts = resources.files("kiwoom_stock.resources.prompts")
assert "The 6 Physical Forces" in prompts.joinpath("daily_postmortem_system.md").read_text("utf-8")
assert "{stats}" in prompts.joinpath("daily_postmortem_user.md").read_text("utf-8")
assert not Path("trades.db").exists()
assert not Path("logs").exists()
print("package closure ok")
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)
    env["A0_REPOSITORY_ROOT"] = str(REPOSITORY_ROOT)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "package closure ok" in result.stdout
