"""定压基线的状态边界。

改造目标、二次网压力带和受影响站的联合预校验由 :mod:`~heatnet.pressure.retrofit`
统一完成；本类只保存已提交基线，并保留最后一道写入边界，防止绕过改造编排直接把
越界值写入基线。
"""

from __future__ import annotations

import math

from ..config import PressureLimits
from ..errors import InterlockError
from .state import PressureSnapshot


class PressureBaseline:
    """二次网定压基线。"""

    def __init__(self, limits: PressureLimits, initial_kpa: float | None = None) -> None:
        self._limits = limits
        midpoint = (limits.baseline_min_kpa + limits.baseline_max_kpa) / 2.0
        initial = midpoint if initial_kpa is None else float(initial_kpa)
        if not math.isfinite(initial) or not limits.in_range(initial):
            raise InterlockError(
                f"初始定压基线 {initial:.1f} kPa 越界 "
                f"[{limits.baseline_min_kpa:.1f}, {limits.baseline_max_kpa:.1f}]",
                component="pressure",
            )
        self._baseline_kpa = initial
        self._revisions = 0
        self._rejected = 0
        self._last_target_kpa = self._baseline_kpa

    @property
    def limits(self) -> PressureLimits:
        return self._limits

    def value_kpa(self) -> float:
        """返回当前基线。"""

        return self._baseline_kpa

    def revisions(self) -> int:
        """返回累计成功改造次数。"""

        return self._revisions

    def rejected(self) -> int:
        """返回累计被拒的改造次数。"""

        return self._rejected

    def last_target_kpa(self) -> float:
        """返回最近一次下发的目标值。"""

        return self._last_target_kpa

    def in_range(self, target_kpa: float) -> bool:
        """目标值是否落在许可范围内。"""

        return self._limits.in_range(target_kpa)

    def reject_retrofit(self, target_kpa: float) -> None:
        """记录一个未通过预校验、尚未写入的目标。"""

        try:
            self._last_target_kpa = float(target_kpa)
        except (TypeError, ValueError):
            self._last_target_kpa = float("nan")
        self._rejected += 1

    def apply_retrofit(self, target_kpa: float) -> float:
        """按改造下发更新基线：先校验，通过才写值。"""

        target = float(target_kpa)
        self._last_target_kpa = target
        if not self.in_range(target):
            self._rejected += 1
            raise InterlockError(
                f"定压基线 {target:.1f} kPa 越界 "
                f"[{self._limits.baseline_min_kpa:.1f}, {self._limits.baseline_max_kpa:.1f}]",
                component="pressure",
            )
        self._baseline_kpa = target
        self._revisions += 1
        return self._baseline_kpa

    def cancel_last_revision(self) -> None:
        """撤销执行期失败并整体退回的那次成功提交计数。"""

        if self._revisions > 0:
            self._revisions -= 1

    def restore_baseline(self, target_kpa: float) -> float:
        """执行整体退回时恢复基线，不计入成功改造次数。"""

        target = float(target_kpa)
        if not self.in_range(target):
            raise InterlockError(
                f"回退基线 {target:.1f} kPa 越界 "
                f"[{self._limits.baseline_min_kpa:.1f}, {self._limits.baseline_max_kpa:.1f}]",
                component="pressure",
            )
        self._baseline_kpa = target
        self._last_target_kpa = target
        return self._baseline_kpa

    def snapshot(self) -> PressureSnapshot:
        """返回定压基线状态快照。"""

        return PressureSnapshot(
            baseline_kpa=self._baseline_kpa,
            revisions=self._revisions,
            rejected=self._rejected,
            last_target_kpa=self._last_target_kpa,
            in_range=self.in_range(self._baseline_kpa),
        )
