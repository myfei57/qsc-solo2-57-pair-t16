"""定压基线的校验、影响面与事务回滚。"""

from __future__ import annotations

import pytest

from heatnet.config import default_config
from heatnet.errors import InterlockError
from heatnet.pressure.baseline import PressureBaseline
from heatnet.pressure.control import PressurisingController
from heatnet.secondary.loop import SecondaryLoop
from heatnet.station.station import HeatStation


class _FailingStation:
    """同步到指定目标值时抛错，用于模拟改造中途的部分失败。"""

    def __init__(self, station: HeatStation, fail_at_kpa: float) -> None:
        self._station = station
        self._fail_at_kpa = float(fail_at_kpa)

    @property
    def name(self) -> str:
        return self._station.name

    def zone(self) -> str:
        return self._station.zone()

    def pressure_kpa(self) -> float | None:
        return self._station.pressure_kpa()

    def set_pressure_kpa(self, pressure_kpa: float) -> float:
        if abs(float(pressure_kpa) - self._fail_at_kpa) <= 0.01:
            raise InterlockError(
                f"换热站 {self._station.name} 定压同步失败", component="pressure"
            )
        return self._station.set_pressure_kpa(pressure_kpa)


def _stack(
    station_count: int = 3,
) -> tuple[PressureBaseline, PressurisingController, SecondaryLoop, list[HeatStation]]:
    config = default_config()
    baseline = PressureBaseline(config.pressure)
    initial = baseline.value_kpa()
    controller = PressurisingController(baseline, config.pressure)
    loop = SecondaryLoop(config.secondary, pressure_kpa=initial)
    stations = [
        HeatStation(f"st{index:02d}", "north", config.station, pressure_kpa=initial)
        for index in range(1, station_count + 1)
    ]
    return baseline, controller, loop, stations


def test_retrofit_validates_before_writing():
    baseline, _, _, stations = _stack()
    original = baseline.value_kpa()
    with pytest.raises(InterlockError):
        baseline.apply_retrofit(9999.0)
    assert baseline.value_kpa() == pytest.approx(original)
    assert baseline.rejected() == 1
    assert baseline.revisions() == 0
    assert baseline.last_target_kpa() == pytest.approx(9999.0)
    assert baseline.snapshot().in_range is True
    assert all(station.pressure_kpa() == pytest.approx(original) for station in stations)


def test_controller_preflight_rejects_and_changes_nothing():
    baseline, controller, loop, stations = _stack()
    original = baseline.value_kpa()
    report = controller.retrofit(loop, 9999.0, stations=stations)
    assert report["ok"] is False
    assert report["status"] == "rejected"
    assert report["rolled_back"] is False
    assert report["rollback_scope"] == "none"
    assert report["affected_stations"] == ["st01", "st02", "st03"]
    assert [item["planned_kpa"] for item in report["impact"]] == [9999.0] * 3
    assert baseline.value_kpa() == pytest.approx(original)
    assert loop.pressure_kpa() == pytest.approx(original)
    assert all(station.pressure_kpa() == pytest.approx(original) for station in stations)
    assert baseline.rejected() == 1
    assert baseline.revisions() == 0
    assert baseline.last_target_kpa() == pytest.approx(9999.0)


def test_valid_retrofit_commits_baseline_loop_and_all_stations():
    baseline, controller, loop, stations = _stack()
    target = baseline.limits.baseline_max_kpa - 10.0
    report = controller.retrofit(loop, target, stations=stations)
    assert report["ok"] is True
    assert report["status"] == "committed"
    assert report["retrofit_type"] == "raise"
    assert report["failure_policy"] == "old_baseline"
    assert report["affected_stations"] == ["st01", "st02", "st03"]
    assert report["changed_stations"] == ["st01", "st02", "st03"]
    assert baseline.value_kpa() == pytest.approx(target)
    assert loop.pressure_kpa() == pytest.approx(target)
    assert all(station.pressure_kpa() == pytest.approx(target) for station in stations)
    assert controller.settles() == 1
    assert baseline.revisions() == 1


def test_raise_partial_failure_rolls_back_baseline_loop_and_every_station():
    baseline, controller, loop, stations = _stack(station_count=3)
    original = baseline.value_kpa()
    target = original + 20.0
    failing = _FailingStation(stations[1], target)
    report = controller.retrofit(loop, target, stations=[stations[0], failing, stations[2]])

    assert report["ok"] is False
    assert report["status"] == "rolled_back"
    assert report["rolled_back"] is True
    assert report["rollback_scope"] == "baseline_secondary_affected_stations"
    assert report["failure_policy"] == "old_baseline"
    assert report["rollback_baseline_kpa"] == pytest.approx(original)
    assert report["affected_stations"] == ["st01", "st02", "st03"]
    assert report["changed_stations"] == ["st01"]
    assert report["restored_stations"] == ["st01", "st02", "st03"]
    assert baseline.value_kpa() == pytest.approx(original)
    assert loop.pressure_kpa() == pytest.approx(original)
    for station in stations:
        assert station.pressure_kpa() == pytest.approx(original)
    assert baseline.revisions() == 0


def test_lower_partial_failure_uses_safe_intermediate_for_all_affected_stations():
    baseline, controller, loop, stations = _stack(station_count=3)
    original = baseline.value_kpa()
    target = original - 40.0
    safe = (original + target) / 2.0
    failing = _FailingStation(stations[1], target)
    report = controller.retrofit(loop, target, stations=[stations[0], failing, stations[2]])

    assert report["ok"] is False
    assert report["retrofit_type"] == "lower"
    assert report["failure_policy"] == "safe_intermediate"
    assert report["rollback_baseline_kpa"] == pytest.approx(safe)
    assert baseline.value_kpa() == pytest.approx(safe)
    assert loop.pressure_kpa() == pytest.approx(safe)
    assert report["restored_stations"] == ["st01", "st02", "st03"]
    for station in stations:
        assert station.pressure_kpa() == pytest.approx(safe)
    assert {item["station"]: item["actual_kpa"] for item in report["impact"]} == {
        "st01": round(safe, 2),
        "st02": round(safe, 2),
        "st03": round(safe, 2),
    }
    assert baseline.revisions() == 0


def test_pressurising_controller_rejects_bad_step():
    _, controller, loop, _ = _stack()
    with pytest.raises(ValueError):
        controller.settle(loop, 0.0)
