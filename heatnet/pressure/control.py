"""定压控制。

基线只是目标值，泵房还要把实际定压拉到基线附近。改造计划、基线范围、二次网压力带
与受影响站的一致性都在 :mod:`~heatnet.pressure.retrofit` 同一处保证；这里只负责
调用计划并汇总控制器状态，下发失败时二次网压力一个值都不许动，执行期部分失败则整
体退回基线、二次网和全部受影响站。
"""

from __future__ import annotations

from ..config import PressureLimits, SecondaryLimits
from ..errors import PlantError
from ..secondary.loop import SecondaryLoop
from ..station.station import HeatStation
from .baseline import PressureBaseline
from .retrofit import (
    ROLLBACK_NONE,
    ROLLBACK_SCOPE_NONE,
    build_retrofit_plan,
    drive_loop_pressure,
    execute_retrofit,
    pressure_consistent,
)


class PressurisingController:
    """把二次网定压拉到基线附近的控制器。"""

    def __init__(
        self,
        baseline: PressureBaseline,
        limits: PressureLimits,
        secondary_limits: SecondaryLimits | None = None,
        stations: dict[str, HeatStation] | None = None,
    ) -> None:
        self._baseline = baseline
        self._limits = limits
        self._secondary_limits = secondary_limits
        self._stations = dict(stations or {})
        self._settles = 0
        self._partial_failures = 0
        self._rollbacks = 0
        self._last_report: dict | None = None

    @property
    def baseline(self) -> PressureBaseline:
        return self._baseline

    @property
    def limits(self) -> PressureLimits:
        return self._limits

    def stations(self) -> dict[str, HeatStation]:
        """返回改造影响到的换热站。"""

        return dict(self._stations)

    def bind_stations(self, stations: dict[str, HeatStation]) -> None:
        """绑定受定压改造影响的站。"""

        self._stations = dict(stations)

    def settles(self) -> int:
        """返回累计收敛次数。"""

        return self._settles

    def partial_failures(self) -> int:
        """返回执行期部分失败次数（预校验拒绝不计入）。"""

        return self._partial_failures

    def rollbacks(self) -> int:
        """返回执行期整体退回次数。"""

        return self._rollbacks

    def last_report(self) -> dict | None:
        """返回最近一次改造报告。"""

        return None if self._last_report is None else dict(self._last_report)

    def annotate_last_report(self, **context: object) -> None:
        """补充平台侧审计关联信息（例如操作 ID）。"""

        if self._last_report is not None:
            self._last_report.update(context)

    def settle(self, loop: SecondaryLoop, dt: float = 1.0) -> float:
        """把二次网定压推进到当前基线附近，返回收敛后的压力。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        pressure = drive_loop_pressure(loop, self._baseline.value_kpa(), dt=dt)
        self._settles += 1
        return pressure

    def retrofit(
        self,
        loop: SecondaryLoop,
        target_kpa: float,
        dt: float = 1.0,
        stations: dict[str, HeatStation] | None = None,
    ) -> dict:
        """先算后校验地执行一次定压改造，并驱动受影响站收敛。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        affected = self._stations if stations is None else dict(stations)
        before = self._baseline.value_kpa()
        before_pressure = loop.pressure_kpa()
        try:
            plan = build_retrofit_plan(
                self._baseline,
                target_kpa,
                list(affected.values()),
                self._secondary_limits,
                loop.pressure_kpa(),
            )
        except PlantError as error:
            self._baseline.reject_retrofit(target_kpa)
            report = {
                "ok": False,
                "violations": [error.message],
                "baseline_kpa": round(before, 2),
                "pressure_kpa": round(before_pressure, 2),
                "consistent": pressure_consistent(before, before_pressure),
                "retrofit_type": "unknown",
                "affected_stations": sorted(affected),
                "impacted_count": len(affected),
                "impacts": [],
                "rolled_back": False,
                "rollback_mode": ROLLBACK_NONE,
                "rollback_scope": ROLLBACK_SCOPE_NONE,
                "rollback_baseline_kpa": None,
                "rollback_pressure_kpa": None,
                "rollback_stations": [],
                "precheck_rejected": True,
                "partial_failure": False,
                "unchanged": True,
            }
            self._last_report = report
            return dict(report)

        report = execute_retrofit(
            self._baseline,
            loop,
            affected,
            plan,
            self._secondary_limits,
            dt=dt,
        )
        self._settles += 1
        report["precheck_rejected"] = False
        if report["ok"]:
            report["partial_failure"] = False
            report["unchanged"] = False
        else:
            self._partial_failures += 1
            if report["rolled_back"]:
                self._rollbacks += 1
            report["partial_failure"] = True
            report["unchanged"] = False
        self._last_report = report
        return dict(report)
