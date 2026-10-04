"""定压基线的校验与回滚。

改造事务先由 :mod:`heatnet.pressure.retrofit` 纯计算目标、安全值与影响面，
再由控制器完成校验和统一提交。这里的写入方法只服务于这个事务：校验阶段不调用，
因此越界目标不会先进入基线。
"""

from __future__ import annotations

from ..config import PressureLimits
from ..errors import InterlockError
from .state import PressureSnapshot


class PressureBaseline:
    """二次网定压基线。"""

    def __init__(self, limits: PressureLimits) -> None:
        self._limits = limits
        self._baseline_kpa = (limits.baseline_min_kpa + limits.baseline_max_kpa) / 2.0
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
        """返回最近一次下发或被拒的目标值。"""

        return self._last_target_kpa

    def in_range(self, target_kpa: float) -> bool:
        """目标值是否落在许可范围内。"""

        return self._limits.in_range(target_kpa)

    def note_rejected(self, target_kpa: float) -> float:
        """记录一次预校验拒绝；不修改基线本体。"""

        target = float(target_kpa)
        self._last_target_kpa = target
        self._rejected += 1
        return target

    def commit_retrofit(self, target_kpa: float) -> float:
        """提交已经通过事务预校验的新基线；成功计数由事务完成后统一标记。"""

        target = float(target_kpa)
        if not self.in_range(target):
            raise InterlockError(
                f"定压基线 {target:.1f} kPa 越界 "
                f"[{self._limits.baseline_min_kpa:.1f}, {self._limits.baseline_max_kpa:.1f}]",
                component="pressure",
            )
        self._baseline_kpa = target
        self._last_target_kpa = target
        return self._baseline_kpa

    def mark_revision(self) -> int:
        """在整条改造事务成功后记一次改造。"""

        self._revisions += 1
        return self._revisions

    def restore_baseline(
        self,
        baseline_kpa: float,
        *,
        requested_target_kpa: float | None = None,
    ) -> float:
        """把基线恢复到事务指定的旧值或安全中间值，不计成功改造。"""

        baseline = float(baseline_kpa)
        if not self.in_range(baseline):
            raise InterlockError(
                f"回退定压基线 {baseline:.1f} kPa 越界 "
                f"[{self._limits.baseline_min_kpa:.1f}, {self._limits.baseline_max_kpa:.1f}]",
                component="pressure",
            )
        self._baseline_kpa = baseline
        if requested_target_kpa is not None:
            self._last_target_kpa = float(requested_target_kpa)
        return self._baseline_kpa

    def apply_retrofit(self, target_kpa: float) -> float:
        """只操作基线时的兼容入口：先校验，通过才写值。"""

        target = float(target_kpa)
        if not self.in_range(target):
            self.note_rejected(target)
            raise InterlockError(
                f"定压基线 {target:.1f} kPa 越界 "
                f"[{self._limits.baseline_min_kpa:.1f}, {self._limits.baseline_max_kpa:.1f}]",
                component="pressure",
            )
        value = self.commit_retrofit(target)
        self.mark_revision()
        return value

    def snapshot(self) -> PressureSnapshot:
        """返回定压基线状态快照。"""

        return PressureSnapshot(
            baseline_kpa=self._baseline_kpa,
            revisions=self._revisions,
            rejected=self._rejected,
            last_target_kpa=self._last_target_kpa,
            in_range=self.in_range(self._baseline_kpa),
        )
