"""定压改造的影响面计算与策略定义。

这里只做“先算”的工作：根据当前基线、目标基线和站侧已同步压力生成计划，
不写基线、不驱动二次网、不碰换热站。真正提交和回退统一由
:class:`heatnet.pressure.control.PressurisingController` 执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..config import PressureLimits

CONSISTENCY_TOLERANCE_KPA = 0.01

RAISE = "raise"
LOWER = "lower"
HOLD = "hold"

OLD_BASELINE = "old_baseline"
SAFE_INTERMEDIATE = "safe_intermediate"


class PressureStation(Protocol):
    """改造需要同步的换热站最小接口。"""

    @property
    def name(self) -> str: ...

    def zone(self) -> str: ...

    def pressure_kpa(self) -> float | None: ...
    def set_pressure_kpa(self, pressure_kpa: float) -> float: ...


@dataclass(frozen=True)
class StationImpact:
    """一个站在本次改造中的压力变化。"""

    name: str
    zone: str
    before_kpa: float | None
    planned_kpa: float

    def delta_kpa(self) -> float | None:
        if self.before_kpa is None:
            return None
        return self.planned_kpa - self.before_kpa

    def as_dict(self) -> dict:
        delta = self.delta_kpa()
        return {
            "station": self.name,
            "zone": self.zone,
            "before_kpa": None if self.before_kpa is None else round(self.before_kpa, 2),
            "planned_kpa": round(self.planned_kpa, 2),
            "delta_kpa": None if delta is None else round(delta, 2),
        }


@dataclass(frozen=True)
class RetrofitPlan:
    """一份无副作用的定压改造计划。"""

    target_kpa: float
    current_kpa: float
    safe_kpa: float
    fallback_baseline_kpa: float
    retrofit_type: str
    failure_policy: str
    impact: tuple[StationImpact, ...]

    def station_names(self) -> list[str]:
        """返回受影响站名。"""

        return [item.name for item in self.impact]

    def as_dict(self) -> dict:
        return {
            "target_kpa": round(self.target_kpa, 2),
            "current_kpa": round(self.current_kpa, 2),
            "safe_kpa": round(self.safe_kpa, 2),
            "fallback_baseline_kpa": round(self.fallback_baseline_kpa, 2),
            "retrofit_type": self.retrofit_type,
            "failure_policy": self.failure_policy,
            "affected_stations": self.station_names(),
            "impact": [item.as_dict() for item in self.impact],
        }


def consistent(a_kpa: float, b_kpa: float) -> bool:
    """基线与二次网/站侧压力是否在同一公差内一致。"""

    return abs(float(a_kpa) - float(b_kpa)) <= CONSISTENCY_TOLERANCE_KPA


def classify(current_kpa: float, target_kpa: float) -> str:
    """按目标与旧基线的方向确定改造类型。"""

    if target_kpa > current_kpa + CONSISTENCY_TOLERANCE_KPA:
        return RAISE
    if target_kpa < current_kpa - CONSISTENCY_TOLERANCE_KPA:
        return LOWER
    return HOLD


def build_plan(
    stations: Sequence[PressureStation],
    current_kpa: float,
    target_kpa: float,
    limits: PressureLimits,
    observed_pressure_kpa: float | None = None,
    secondary_min_kpa: float | None = None,
    secondary_max_kpa: float | None = None,
) -> tuple[RetrofitPlan, list[str]]:
    """计算改造计划和违规原因；本函数不修改任何现场值。"""

    target = float(target_kpa)
    current = float(current_kpa)
    violations: list[str] = []
    if not limits.in_range(current):
        violations.append(
            f"当前定压基线 {current:.1f} kPa 越界 "
            f"[{limits.baseline_min_kpa:.1f}, {limits.baseline_max_kpa:.1f}]"
        )
    if not limits.in_range(target):
        violations.append(
            f"定压基线 {target:.1f} kPa 越界 "
            f"[{limits.baseline_min_kpa:.1f}, {limits.baseline_max_kpa:.1f}]"
        )
    if secondary_min_kpa is not None and target < float(secondary_min_kpa):
        violations.append(
            f"目标定压 {target:.1f} kPa 低于二次网补水门限 {float(secondary_min_kpa):.1f} kPa"
        )
    if secondary_max_kpa is not None and target > float(secondary_max_kpa):
        violations.append(
            f"目标定压 {target:.1f} kPa 高于二次网超压门限 {float(secondary_max_kpa):.1f} kPa"
        )

    observed = current if observed_pressure_kpa is None else float(observed_pressure_kpa)
    if not consistent(observed, current):
        violations.append(
            f"二次网定压 {observed:.1f} kPa 与基线 {current:.1f} kPa 不一致，禁止叠加改造"
        )
    for station in stations:
        before = station.pressure_kpa()
        if before is not None and not consistent(float(before), current):
            violations.append(
                f"换热站 {station.name} 定压 {float(before):.1f} kPa 与基线 {current:.1f} kPa 不一致"
            )

    retrofit_type = classify(current, target)
    if retrofit_type == LOWER:
        safe_kpa = (current + target) / 2.0
        failure_policy = SAFE_INTERMEDIATE
        fallback_baseline_kpa = safe_kpa
    else:
        safe_kpa = current
        failure_policy = OLD_BASELINE
        fallback_baseline_kpa = current
    safe_kpa = min(limits.baseline_max_kpa, max(limits.baseline_min_kpa, float(safe_kpa)))
    if secondary_min_kpa is not None:
        safe_kpa = max(float(secondary_min_kpa), safe_kpa)
    if secondary_max_kpa is not None:
        safe_kpa = min(float(secondary_max_kpa), safe_kpa)
    fallback_baseline_kpa = safe_kpa if retrofit_type == LOWER else current

    ordered = sorted(stations, key=lambda item: item.name)
    impact = tuple(
        StationImpact(
            name=station.name,
            zone=station.zone(),
            before_kpa=station.pressure_kpa(),
            planned_kpa=target,
        )
        for station in ordered
    )
    return (
        RetrofitPlan(
            target_kpa=target,
            current_kpa=current,
            safe_kpa=safe_kpa,
            fallback_baseline_kpa=fallback_baseline_kpa,
            retrofit_type=retrofit_type,
            failure_policy=failure_policy,
            impact=impact,
        ),
        violations,
    )
