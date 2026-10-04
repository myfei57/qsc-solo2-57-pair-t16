"""定压控制。

定压改造是一个原子事务：先纯计算目标、安全回退值和影响站，再统一校验；
校验阶段不碰基线、二次网和站。提交后基线、二次网压力、受影响站侧压力只能由本类
在同一处一起核对。中途任一失败都按改造类型执行整体退回：升压回旧基线，降压回旧值
与目标之间的安全中间值；回退范围固定包含“基线 + 二次网 + 本次影响到的全部站”。
"""

from __future__ import annotations

from typing import Sequence

from ..config import PressureLimits
from ..errors import InterlockError, PlantError
from ..secondary.loop import SecondaryLoop
from .baseline import PressureBaseline
from .retrofit import (
    PressureStation,
    RetrofitPlan,
    build_plan,
    consistent,
)

MAX_STEPS = 600


class PressurisingController:
    """把二次网定压拉到基线附近的控制器。"""

    def __init__(self, baseline: PressureBaseline, limits: PressureLimits) -> None:
        self._baseline = baseline
        self._limits = limits
        self._settles = 0
        self._last_report: dict | None = None

    @property
    def baseline(self) -> PressureBaseline:
        return self._baseline

    @property
    def limits(self) -> PressureLimits:
        return self._limits

    def settles(self) -> int:
        """返回累计收敛次数。"""

        return self._settles

    def last_report(self) -> dict | None:
        """返回最近一次改造事务报告，供控制台展示。"""

        return None if self._last_report is None else dict(self._last_report)

    def settle(self, loop: SecondaryLoop, dt: float = 1.0) -> float:
        """把二次网定压推进到当前基线附近，返回收敛后的压力。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        target = self._baseline.value_kpa()
        for _ in range(MAX_STEPS):
            current = loop.pressure_kpa()
            gap = target - current
            if abs(gap) <= 2.0:
                break
            if gap > 0:
                loop.add_pressure(min(gap, 12.0 * dt))
            else:
                loop.bleed_pressure(min(-gap, 12.0 * dt))
        self._settles += 1
        return loop.pressure_kpa()

    def _drive_to(self, loop: SecondaryLoop, target_kpa: float, dt: float) -> float:
        if dt <= 0:
            raise ValueError("步长必须为正")
        target = float(target_kpa)
        for _ in range(MAX_STEPS):
            current = loop.pressure_kpa()
            gap = target - current
            if consistent(current, target):
                return target
            step = min(abs(gap), 12.0 * dt)
            if step <= 0:
                raise InterlockError(
                    f"二次网定压无法推进到 {target:.1f} kPa，当前 {current:.1f} kPa",
                    component="pressure",
                )
            if gap > 0:
                loop.add_pressure(step)
            else:
                loop.bleed_pressure(step)
        current = loop.pressure_kpa()
        if not consistent(current, target):
            raise InterlockError(
                f"二次网定压收敛超时：目标 {target:.1f} kPa，当前 {current:.1f} kPa",
                component="pressure",
            )
        return target

    @staticmethod
    def _station_impacts(
        plan: RetrofitPlan, stations: Sequence[PressureStation], value_kpa: float
    ) -> list[dict]:
        by_name = {station.name: station for station in stations}
        impacts: list[dict] = []
        for impact in plan.impact:
            station = by_name[impact.name]
            actual = station.pressure_kpa()
            if actual is None or not consistent(float(actual), float(value_kpa)):
                raise InterlockError(
                    f"换热站 {impact.name} 定压未同步到 {float(value_kpa):.1f} kPa",
                    component="pressure",
                )
            impacts.append(
                {
                    **impact.as_dict(),
                    "actual_kpa": round(float(actual), 2),
                }
            )
        return impacts

    def _apply_stations(
        self,
        stations: Sequence[PressureStation],
        value_kpa: float,
        changed_stations: list[str],
    ) -> None:
        applied = list(changed_stations)
        for station in sorted(stations, key=lambda item: item.name):
            station.set_pressure_kpa(value_kpa)
            actual = station.pressure_kpa()
            if actual is None or not consistent(float(actual), float(value_kpa)):
                raise InterlockError(
                    f"换热站 {station.name} 定压未同步到 {float(value_kpa):.1f} kPa",
                    component="pressure",
                )
            if station.name not in applied:
                applied.append(station.name)
            changed_stations[:] = applied

    @staticmethod
    def _station_pressure(station: PressureStation) -> float:
        pressure = station.pressure_kpa()
        if pressure is None:
            raise InterlockError(f"换热站 {station.name} 尚未同步定压", component="pressure")
        return float(pressure)

    def _rollback(
        self,
        plan: RetrofitPlan,
        loop: SecondaryLoop,
        stations: Sequence[PressureStation],
        *,
        changed_stations: Sequence[str],
        dt: float,
    ) -> tuple[float, list[str], list[dict]]:
        fallback = plan.fallback_baseline_kpa
        self._baseline.restore_baseline(fallback, requested_target_kpa=plan.target_kpa)
        self._drive_to(loop, fallback, dt)

        by_name = {station.name: station for station in stations}
        restored: list[str] = []
        rollback_steps: list[dict] = []
        for impact in plan.impact:
            station = by_name[impact.name]
            before = self._station_pressure(station)
            if not consistent(before, fallback):
                # 回退范围固定为“基线 + 二次网 + 全部受影响站”：
                # 升压回到旧基线；降压回到安全中间值，未完成的站也要一起对齐，
                # 否则会留下“有些站在目标值、有些站在旧值”的半改造状态。
                station.set_pressure_kpa(fallback)
            actual = self._station_pressure(station)
            if not consistent(actual, fallback):
                raise InterlockError(
                    f"换热站 {impact.name} 回退后定压 {actual:.1f} kPa 与安全基线 "
                    f"{fallback:.1f} kPa 不一致",
                    component="pressure",
                )
            restored.append(impact.name)
            rollback_steps.append(
                {
                    "station": impact.name,
                    "from_kpa": round(before, 2),
                    "restored_kpa": round(fallback, 2),
                    "had_committed": impact.name in changed_stations,
                }
            )
        if not consistent(loop.pressure_kpa(), self._baseline.value_kpa()):
            raise InterlockError("二次网定压回退后仍与基线不一致", component="pressure")
        return fallback, restored, rollback_steps

    def retrofit(
        self,
        loop: SecondaryLoop,
        target_kpa: float,
        dt: float = 1.0,
        stations: Sequence[PressureStation] | None = None,
    ) -> dict:
        """下发一次定压改造，原子提交基线、二次网和全部影响站。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        affected = tuple(stations or ())
        current = self._baseline.value_kpa()
        plan, violations = build_plan(
            affected,
            current,
            target_kpa,
            self._limits,
            loop.pressure_kpa(),
            secondary_min_kpa=loop.limits.makeup_min_kpa,
            secondary_max_kpa=loop.limits.makeup_max_kpa,
        )
        planned_impact = plan.as_dict()["impact"]
        if violations:
            self._baseline.note_rejected(target_kpa)
            report = {
                "ok": False,
                "status": "rejected",
                "rollback_ok": True,
                "violations": list(violations),
                "target_kpa": round(float(target_kpa), 2),
                "baseline_kpa": round(current, 2),
                "pressure_kpa": round(loop.pressure_kpa(), 2),
                "rolled_back": False,
                "rollback_scope": "none",
                "rollback_baseline_kpa": round(current, 2),
                "restored_stations": [],
                "changed_stations": [],
                "affected_stations": plan.station_names(),
                "impact": planned_impact,
                "retrofit_type": plan.retrofit_type,
                "failure_policy": plan.failure_policy,
            }
            self._last_report = report
            return report

        changed_stations: list[str] = []
        try:
            self._baseline.commit_retrofit(plan.target_kpa)
            self._drive_to(loop, plan.target_kpa, dt)
            self._apply_stations(affected, plan.target_kpa, changed_stations)
            self._station_impacts(plan, affected, plan.target_kpa)
        except PlantError as error:
            fallback, restored, rollback_steps = self._rollback(
                plan,
                loop,
                affected,
                changed_stations=changed_stations,
                dt=dt,
            )
            report = {
                "ok": False,
                "status": "rolled_back",
                "rollback_ok": True,
                "violations": [error.message],
                "target_kpa": round(plan.target_kpa, 2),
                "baseline_kpa": round(fallback, 2),
                "pressure_kpa": round(loop.pressure_kpa(), 2),
                "rolled_back": True,
                "rollback_scope": "baseline_secondary_affected_stations",
                "rollback_baseline_kpa": round(fallback, 2),
                "restored_stations": restored,
                "changed_stations": list(changed_stations),
                "affected_stations": plan.station_names(),
                "impact": self._final_impacts(plan, affected, fallback),
                "rollback_steps": rollback_steps,
                "retrofit_type": plan.retrofit_type,
                "failure_policy": plan.failure_policy,
            }
            self._last_report = report
            return report

        # 基线、二次网与站在同一出口做最后一致性核对。
        baseline = self._baseline.value_kpa()
        if not consistent(baseline, plan.target_kpa) or not consistent(
            loop.pressure_kpa(), baseline
        ):
            fallback, restored, rollback_steps = self._rollback(
                plan,
                loop,
                affected,
                changed_stations=changed_stations,
                dt=dt,
            )
            report = {
                "ok": False,
                "status": "rolled_back",
                "rollback_ok": True,
                "violations": ["提交后基线、二次网或站侧定压不一致"],
                "target_kpa": round(plan.target_kpa, 2),
                "baseline_kpa": round(fallback, 2),
                "pressure_kpa": round(loop.pressure_kpa(), 2),
                "rolled_back": True,
                "rollback_scope": "baseline_secondary_affected_stations",
                "rollback_baseline_kpa": round(fallback, 2),
                "restored_stations": restored,
                "changed_stations": list(changed_stations),
                "affected_stations": plan.station_names(),
                "impact": self._final_impacts(plan, affected, fallback),
                "rollback_steps": rollback_steps,
                "retrofit_type": plan.retrofit_type,
                "failure_policy": plan.failure_policy,
            }
            self._last_report = report
            return report

        self._baseline.mark_revision()
        self._settles += 1
        report = {
            "ok": True,
            "status": "committed",
            "rollback_ok": True,
            "violations": [],
            "target_kpa": round(plan.target_kpa, 2),
            "baseline_kpa": round(baseline, 2),
            "pressure_kpa": round(loop.pressure_kpa(), 2),
            "rolled_back": False,
            "rollback_scope": "none",
            "rollback_baseline_kpa": round(plan.safe_kpa, 2),
            "restored_stations": [],
            "changed_stations": list(changed_stations),
            "affected_stations": plan.station_names(),
            "impact": self._final_impacts(plan, affected, baseline),
            "retrofit_type": plan.retrofit_type,
            "failure_policy": plan.failure_policy,
        }
        self._last_report = report
        return report

    def _final_impacts(
        self, plan: RetrofitPlan, stations: Sequence[PressureStation], final_kpa: float
    ) -> list[dict]:
        by_name = {station.name: station for station in stations}
        impacts: list[dict] = []
        for impact in plan.impact:
            station = by_name[impact.name]
            actual = station.pressure_kpa()
            if actual is None or not consistent(float(actual), float(final_kpa)):
                raise InterlockError(
                    f"换热站 {impact.name} 最终定压 {actual} 与基线 {final_kpa:.1f} kPa 不一致",
                    component="pressure",
                )
            impacts.append(
                {
                    **impact.as_dict(),
                    "actual_kpa": round(float(actual), 2),
                }
            )
        return impacts
