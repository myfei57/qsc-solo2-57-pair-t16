"""定压改造的统一计划、校验与整体退回。

改造入口只允许按一个固定顺序工作：先从当前基线计算目标、类型和回退策略，再由本
模块完成基线范围与二次网压力带的同一套校验；校验通过前不写基线、不动二次网和站。
执行期任何一站失败都属于部分失败，必须把基线、二次网和全部受影响站一起退到由改造
类型预先确定的安全点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import PressureLimits, SecondaryLimits
from ..errors import InterlockError
from ..secondary.loop import SecondaryLoop
from ..station.station import HeatStation
from .baseline import PressureBaseline

PRESSURE_TOLERANCE_KPA = 2.0
MAX_PRESSURE_STEPS = 600

RAISE = "raise"
LOWER = "lower"
HOLD = "hold"

# 升压失败时旧压力仍是最近一个已验证状态，整体退回旧基线。
ROLLBACK_OLD_BASELINE = "old_baseline"
# 降压中途失败不能重新打回高压，停在旧值与目标值之间的安全中间值。
ROLLBACK_SAFE_INTERMEDIATE = "safe_intermediate"
ROLLBACK_NONE = "none"
ROLLBACK_SCOPE_NONE = "none"
ROLLBACK_SCOPE_ALL = "baseline_loop_stations"


def pressure_consistent(
    target_kpa: float, actual_kpa: float, tolerance_kpa: float = PRESSURE_TOLERANCE_KPA
) -> bool:
    """基线与实际二次网定压是否符合同一条收敛口径。"""

    return abs(float(actual_kpa) - float(target_kpa)) <= tolerance_kpa


@dataclass(frozen=True)
class RetrofitPlan:
    """一次改造在任何写入前算出的完整计划。"""

    target_kpa: float
    old_baseline_kpa: float
    retrofit_type: str
    rollback_mode: str
    rollback_baseline_kpa: float
    safe_intermediate_kpa: float | None
    affected_stations: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "target_kpa": round(self.target_kpa, 2),
            "old_baseline_kpa": round(self.old_baseline_kpa, 2),
            "retrofit_type": self.retrofit_type,
            "rollback_mode": self.rollback_mode,
            "rollback_baseline_kpa": round(self.rollback_baseline_kpa, 2),
            "safe_intermediate_kpa": (
                None if self.safe_intermediate_kpa is None else round(self.safe_intermediate_kpa, 2)
            ),
            "affected_stations": list(self.affected_stations),
        }


@dataclass(frozen=True)
class StationPressureImpact:
    """一个受影响站改造后的压力评估结果。"""

    station: str
    zone: str
    before_kpa: float
    target_kpa: float
    after_kpa: float
    deviation_kpa: float
    settled: bool

    def as_dict(self) -> dict:
        return {
            "station": self.station,
            "zone": self.zone,
            "before_kpa": round(self.before_kpa, 2),
            "target_kpa": round(self.target_kpa, 2),
            "after_kpa": round(self.after_kpa, 2),
            "deviation_kpa": round(self.deviation_kpa, 2),
            "settled": self.settled,
        }


def build_retrofit_plan(
    baseline: PressureBaseline,
    target_kpa: float,
    affected_stations: list[HeatStation],
    secondary_limits: SecondaryLimits | None = None,
    current_pressure_kpa: float | None = None,
) -> RetrofitPlan:
    """先算目标、类型、影响面和回退点，再执行唯一一处一致性校验。"""

    try:
        target = float(target_kpa)
    except (TypeError, ValueError) as error:
        raise InterlockError("定压改造目标必须是数值", component="pressure") from error

    old_baseline = baseline.value_kpa()
    if target > old_baseline + PRESSURE_TOLERANCE_KPA:
        retrofit_type = RAISE
    elif target < old_baseline - PRESSURE_TOLERANCE_KPA:
        retrofit_type = LOWER
    else:
        retrofit_type = HOLD

    safe_intermediate: float | None = None
    if retrofit_type == LOWER:
        safe_intermediate = _clamp_safe_baseline(
            (old_baseline + target) / 2.0,
            baseline.limits,
            secondary_limits,
        )
        rollback_mode = ROLLBACK_SAFE_INTERMEDIATE
        rollback_baseline = safe_intermediate
    else:
        rollback_mode = ROLLBACK_OLD_BASELINE
        rollback_baseline = old_baseline

    violations: list[str] = []
    if not math.isfinite(target):
        violations.append("定压改造目标必须是有限数值")
    if not baseline.limits.in_range(target):
        violations.append(
            f"定压基线 {target:.1f} kPa 越界 "
            f"[{baseline.limits.baseline_min_kpa:.1f}, {baseline.limits.baseline_max_kpa:.1f}]"
        )
    if secondary_limits is not None:
        if target < secondary_limits.makeup_min_kpa or target > secondary_limits.makeup_max_kpa:
            violations.append(
                f"二次网定压 {target:.1f} kPa 越界 "
                f"[{secondary_limits.makeup_min_kpa:.1f}, {secondary_limits.makeup_max_kpa:.1f}]"
            )
    if current_pressure_kpa is not None and not pressure_consistent(
        old_baseline, current_pressure_kpa
    ):
        violations.append(
            f"基线 {old_baseline:.1f} kPa 与二次网当前定压 "
            f"{float(current_pressure_kpa):.1f} kPa 不一致"
        )
    if violations:
        raise InterlockError("；".join(violations), component="pressure")

    return RetrofitPlan(
        target_kpa=target,
        old_baseline_kpa=old_baseline,
        retrofit_type=retrofit_type,
        rollback_mode=rollback_mode,
        rollback_baseline_kpa=rollback_baseline,
        safe_intermediate_kpa=safe_intermediate,
        affected_stations=tuple(
            station.name
            for station in sorted(affected_stations, key=lambda item: item.name)
        ),
    )


def drive_loop_pressure(
    loop: SecondaryLoop,
    target_kpa: float,
    dt: float = 1.0,
    tolerance_kpa: float = PRESSURE_TOLERANCE_KPA,
) -> float:
    """把二次网压力推进到目标，返回最终压力。"""

    if dt <= 0:
        raise ValueError("步长必须为正")
    target = float(target_kpa)
    for _ in range(MAX_PRESSURE_STEPS):
        current = loop.pressure_kpa()
        gap = target - current
        if pressure_consistent(target, current, tolerance_kpa):
            return current
        if gap > 0:
            loop.add_pressure(min(gap, 12.0 * dt))
        else:
            loop.bleed_pressure(min(-gap, 12.0 * dt))
    return loop.pressure_kpa()


def drive_station_pressure(
    station: HeatStation,
    target_kpa: float,
    dt: float = 1.0,
    tolerance_kpa: float = PRESSURE_TOLERANCE_KPA,
) -> tuple[StationPressureImpact, Exception | None]:
    """驱动单个站并返回其压力影响评估。"""

    before = station.pressure_kpa()
    target = float(target_kpa)
    for _ in range(MAX_PRESSURE_STEPS):
        current = station.pressure_kpa()
        if pressure_consistent(target, current, tolerance_kpa):
            break
        try:
            station.drive_pressure_kpa(target, 12.0, dt)
        except Exception as error:
            after = station.pressure_kpa()
            return StationPressureImpact(
                station=station.name,
                zone=station.zone(),
                before_kpa=before,
                target_kpa=target,
                after_kpa=after,
                deviation_kpa=after - target,
                settled=False,
            ), error
    after = station.pressure_kpa()
    deviation = after - target
    return StationPressureImpact(
        station=station.name,
        zone=station.zone(),
        before_kpa=before,
        target_kpa=target,
        after_kpa=after,
        deviation_kpa=deviation,
        settled=pressure_consistent(target, after, tolerance_kpa),
    ), None


def execute_retrofit(
    baseline: PressureBaseline,
    loop: SecondaryLoop,
    stations: dict[str, HeatStation],
    plan: RetrofitPlan,
    secondary_limits: SecondaryLimits | None,
    dt: float = 1.0,
) -> dict:
    """提交已校验计划；执行期失败时整体退回基线、二次网和全部受影响站。"""

    before_pressure = loop.pressure_kpa()
    impacts: list[StationPressureImpact] = []
    violations: list[str] = []
    committed = False
    try:
        new_baseline = baseline.apply_retrofit(plan.target_kpa)
        committed = True
        pressure = drive_loop_pressure(loop, new_baseline, dt=dt)
        if not pressure_consistent(new_baseline, pressure):
            violations.append(f"二次网定压 {pressure:.1f} kPa 未收敛到基线 {new_baseline:.1f} kPa")
        if secondary_limits is not None and (
            pressure < secondary_limits.makeup_min_kpa or pressure > secondary_limits.makeup_max_kpa
        ):
            violations.append(f"二次网定压 {pressure:.1f} kPa 超出补水定压带")

        for station_name in plan.affected_stations:
            impact, station_error = drive_station_pressure(
                stations[station_name], new_baseline, dt=dt
            )
            impacts.append(impact)
            if station_error is not None:
                violations.append(f"换热站 {station_name} 改造失败: {station_error}")
            elif not impact.settled:
                violations.append(
                    f"换热站 {station_name} 定压 {impact.after_kpa:.1f} kPa 未收敛"
                )

        if violations:
            raise InterlockError("；".join(violations), component="pressure")
        return {
            "ok": True,
            "violations": [],
            "baseline_kpa": round(new_baseline, 2),
            "pressure_kpa": round(pressure, 2),
            "consistent": pressure_consistent(new_baseline, pressure),
            "retrofit_type": plan.retrofit_type,
            "affected_stations": list(plan.affected_stations),
            "impacted_count": len(plan.affected_stations),
            "impacts": [impact.as_dict() for impact in impacts],
            "rolled_back": False,
            "rollback_mode": ROLLBACK_NONE,
            "rollback_scope": ROLLBACK_SCOPE_NONE,
            "rollback_baseline_kpa": None,
            "rollback_pressure_kpa": None,
            "rollback_stations": [],
            "plan": plan.as_dict(),
        }
    except Exception as error:  # 部分失败也要把已经动过的站全部拉回安全点。
        if isinstance(error, InterlockError):
            failure_message = error.message
        else:
            failure_message = f"定压改造执行失败: {error}"
        if failure_message and failure_message not in violations:
            violations.append(failure_message)
        rollback = rollback_retrofit(
            baseline, loop, stations, plan, dt=dt, committed=committed
        )
        return {
            "ok": False,
            "violations": violations,
            "baseline_kpa": round(rollback["baseline_kpa"], 2),
            "pressure_kpa": round(rollback["pressure_kpa"], 2),
            "consistent": pressure_consistent(
                rollback["baseline_kpa"], rollback["pressure_kpa"]
            ),
            "retrofit_type": plan.retrofit_type,
            "affected_stations": list(plan.affected_stations),
            "impacted_count": len(plan.affected_stations),
            "impacts": [impact.as_dict() for impact in impacts],
            "rolled_back": rollback["ok"],
            "rollback_mode": plan.rollback_mode,
            "rollback_scope": ROLLBACK_SCOPE_ALL,
            "rollback_baseline_kpa": round(rollback["baseline_kpa"], 2),
            "rollback_pressure_kpa": round(rollback["pressure_kpa"], 2),
            "rollback_stations": rollback["stations"],
            "rollback_violations": rollback["violations"],
            "before_pressure_kpa": round(before_pressure, 2),
            "plan": plan.as_dict(),
        }


def rollback_retrofit(
    baseline: PressureBaseline,
    loop: SecondaryLoop,
    stations: dict[str, HeatStation],
    plan: RetrofitPlan,
    dt: float = 1.0,
    committed: bool = False,
) -> dict:
    """按计划固定策略，把基线、二次网和全部受影响站一起退回。"""

    fallback = plan.rollback_baseline_kpa
    violations: list[str] = []
    try:
        restored_baseline = baseline.restore_baseline(fallback)
        if committed:
            baseline.cancel_last_revision()
    except Exception as error:
        return {
            "ok": False,
            "baseline_kpa": baseline.value_kpa(),
            "pressure_kpa": loop.pressure_kpa(),
            "stations": [],
            "violations": [f"基线回退失败: {error}"],
        }
    try:
        pressure = drive_loop_pressure(loop, restored_baseline, dt=dt)
        if not pressure_consistent(restored_baseline, pressure):
            violations.append("回退后二次网定压未收敛")
    except Exception as error:
        pressure = loop.pressure_kpa()
        violations.append(f"二次网定压回退失败: {error}")

    restored: list[dict] = []
    for station_name in plan.affected_stations:
        try:
            impact, station_error = drive_station_pressure(
                stations[station_name], restored_baseline, dt=dt
            )
        except Exception as error:
            violations.append(f"换热站 {station_name} 回退失败: {error}")
            continue
        restored.append(impact.as_dict())
        if station_error is not None:
            violations.append(f"换热站 {station_name} 回退失败: {station_error}")
        elif not impact.settled:
            violations.append(f"换热站 {station_name} 回退后定压未收敛")
    return {
        "ok": not violations,
        "baseline_kpa": restored_baseline,
        "pressure_kpa": pressure,
        "stations": restored,
        "violations": violations,
    }


def _clamp_safe_baseline(
    value: float,
    pressure_limits: PressureLimits,
    secondary_limits: SecondaryLimits | None,
) -> float:
    lower = pressure_limits.baseline_min_kpa
    upper = pressure_limits.baseline_max_kpa
    if secondary_limits is not None:
        lower = max(lower, secondary_limits.makeup_min_kpa)
        upper = min(upper, secondary_limits.makeup_max_kpa)
    return min(upper, max(lower, float(value)))
