"""定压基线的校验、影响面评估与整体退回。"""

from __future__ import annotations

import pytest

from heatnet.config import default_config
from heatnet.errors import InterlockError
from heatnet.pressure.baseline import PressureBaseline
from heatnet.pressure.control import PressurisingController
from heatnet.pressure.retrofit import (
    ROLLBACK_OLD_BASELINE,
    ROLLBACK_SAFE_INTERMEDIATE,
)
from heatnet.secondary.loop import SecondaryLoop
from heatnet.station.station import HeatStation


class FaultyPressureStation(HeatStation):
    """改造到指定压力后抛错，用于模拟部分站已经动作后的中途失败。"""

    def __init__(self, station: HeatStation, fail_at: float) -> None:
        super().__init__(
            station.name,
            station.zone(),
            station.limits,
            pressure_kpa=station.pressure_kpa(),
        )
        self._fail_at = float(fail_at)
        self._fail_target: float | None = None

    def drive_pressure_kpa(
        self,
        target_kpa: float,
        max_step_kpa: float = 12.0,
        dt: float = 1.0,
    ) -> float:
        before = self.pressure_kpa()
        result = super().drive_pressure_kpa(target_kpa, max_step_kpa, dt)
        if self._fail_target is None:
            self._fail_target = float(target_kpa)
        if target_kpa != self._fail_target:
            return result
        if (self._fail_target < before and result <= self._fail_at) or (
            self._fail_target > before and result >= self._fail_at
        ):
            raise RuntimeError("station pressure actuator offline")
        return result


def _stack(station_count: int = 3):
    config = default_config()
    baseline = PressureBaseline(
        config.pressure,
        initial_kpa=config.secondary.makeup_target_kpa,
    )
    loop = SecondaryLoop(config.secondary)
    stations = {
        f"st{index:02d}": HeatStation(
            f"st{index:02d}",
            "north",
            config.station,
            pressure_kpa=loop.pressure_kpa(),
        )
        for index in range(1, station_count + 1)
    }
    controller = PressurisingController(baseline, config.pressure, config.secondary, stations)
    return config, baseline, controller, loop, stations


def test_retrofit_validates_before_writing():
    _, baseline, _, _, _ = _stack()
    original = baseline.value_kpa()
    with pytest.raises(InterlockError):
        baseline.apply_retrofit(9999.0)
    assert baseline.value_kpa() == pytest.approx(original)
    assert baseline.rejected() == 1
    assert baseline.revisions() == 0
    assert baseline.last_target_kpa() == pytest.approx(9999.0)
    assert baseline.snapshot().in_range is True


def test_precheck_rejection_changes_neither_baseline_loop_nor_stations():
    _, baseline, controller, loop, stations = _stack()
    original = baseline.value_kpa()
    before_pressure = loop.pressure_kpa()
    before_station_pressures = {name: station.pressure_kpa() for name, station in stations.items()}

    report = controller.retrofit(loop, original + 2000.0)

    assert report["ok"] is False
    assert report["precheck_rejected"] is True
    assert report["partial_failure"] is False
    assert report["unchanged"] is True
    assert report["rolled_back"] is False
    assert report["rollback_scope"] == "none"
    assert report["baseline_kpa"] == pytest.approx(original)
    assert report["pressure_kpa"] == pytest.approx(before_pressure)
    assert loop.pressure_kpa() == pytest.approx(before_pressure)
    assert baseline.revisions() == 0
    for name, station in stations.items():
        assert station.pressure_kpa() == pytest.approx(before_station_pressures[name])


def test_precheck_uses_one_rule_for_baseline_and_secondary_band():
    _, baseline, controller, loop, _ = _stack()
    target = 250.0
    assert baseline.limits.in_range(target) is True

    report = controller.retrofit(loop, target)

    assert report["ok"] is False
    assert report["precheck_rejected"] is True
    assert report["baseline_kpa"] == pytest.approx(320.0)
    assert "二次网定压" in report["violations"][0]
    assert baseline.value_kpa() == pytest.approx(320.0)
    assert loop.pressure_kpa() == pytest.approx(320.0)


def test_retrofit_rejects_plan_when_baseline_and_loop_are_already_inconsistent():
    _, baseline, controller, loop, _ = _stack()
    loop.add_pressure(25.0)

    report = controller.retrofit(loop, 350.0)

    assert report["ok"] is False
    assert report["precheck_rejected"] is True
    assert report["unchanged"] is True
    assert "不一致" in report["violations"][0]
    assert baseline.value_kpa() == pytest.approx(320.0)
    assert loop.pressure_kpa() == pytest.approx(345.0)


def test_valid_retrofit_settles_loop_and_reports_affected_stations():
    _, baseline, controller, loop, stations = _stack()
    target = 350.0
    report = controller.retrofit(loop, target)

    assert report["ok"] is True
    assert report["rolled_back"] is False
    assert report["retrofit_type"] == "raise"
    assert report["impacted_count"] == len(stations)
    assert report["affected_stations"] == sorted(stations)
    assert {item["station"] for item in report["impacts"]} == set(stations)
    assert all(item["settled"] for item in report["impacts"])
    assert baseline.value_kpa() == pytest.approx(target)
    assert loop.pressure_kpa() == pytest.approx(target, abs=2.0)
    for station in stations.values():
        assert station.pressure_kpa() == pytest.approx(target, abs=2.0)
    assert controller.settles() == 1
    assert baseline.revisions() == 1


def test_lowering_failure_uses_safe_intermediate_and_rolls_back_all_stations():
    _, baseline, controller, loop, stations = _stack()
    valid_stations = dict(stations)
    valid_stations["st02"] = FaultyPressureStation(stations["st02"], 310.0)
    target = 300.0
    plan_safe_midpoint = (baseline.value_kpa() + target) / 2.0

    report = controller.retrofit(loop, target, stations=valid_stations)

    assert report["ok"] is False
    assert report["precheck_rejected"] is False
    assert report["partial_failure"] is True
    assert report["rolled_back"] is True
    assert report["retrofit_type"] == "lower"
    assert report["rollback_mode"] == ROLLBACK_SAFE_INTERMEDIATE
    assert report["rollback_scope"] == "baseline_loop_stations"
    assert report["rollback_baseline_kpa"] == pytest.approx(plan_safe_midpoint)
    assert baseline.value_kpa() == pytest.approx(plan_safe_midpoint)
    assert loop.pressure_kpa() == pytest.approx(plan_safe_midpoint, abs=2.0)
    rollback_names = {item["station"] for item in report["rollback_stations"]}
    assert all(name in rollback_names for name in stations)
    for name, station in stations.items():
        assert station.pressure_kpa() == pytest.approx(plan_safe_midpoint, abs=2.0)
    assert controller.partial_failures() == 1
    assert controller.rollbacks() == 1
    assert baseline.revisions() == 0


def test_raising_failure_restores_old_baseline_for_loop_and_all_stations():
    _, baseline, controller, loop, stations = _stack()
    original = baseline.value_kpa()
    valid_stations = dict(stations)
    valid_stations["st02"] = FaultyPressureStation(stations["st02"], 350.0)

    report = controller.retrofit(loop, 380.0, stations=valid_stations)

    assert report["ok"] is False
    assert report["retrofit_type"] == "raise"
    assert report["rollback_mode"] == ROLLBACK_OLD_BASELINE
    assert report["rollback_scope"] == "baseline_loop_stations"
    assert baseline.value_kpa() == pytest.approx(original)
    assert loop.pressure_kpa() == pytest.approx(original, abs=2.0)
    for station in stations.values():
        assert station.pressure_kpa() == pytest.approx(original, abs=2.0)


def test_pressurising_controller_rejects_bad_step():
    _, _, controller, loop, _ = _stack()
    with pytest.raises(ValueError):
        controller.settle(loop, 0.0)
