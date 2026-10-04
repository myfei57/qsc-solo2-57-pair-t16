"""补水定压基线。

定压基线是二次网运行的压力目标，改造后可以整体上移或下移。改造入口必须
"先算计划、再统一校验、最后写值"：写在基线与二次网压力带校验之前，越界目标会先落
进基线，等报错时旧值已经没了；执行期部分失败则必须把基线、二次网和受影响站整体
退回。
"""

from __future__ import annotations

from .baseline import PressureBaseline
from .control import PressurisingController
from .retrofit import (
    ROLLBACK_OLD_BASELINE,
    ROLLBACK_SAFE_INTERMEDIATE,
    RetrofitPlan,
    build_retrofit_plan,
    pressure_consistent,
)
from .state import PressureSnapshot

__all__ = [
    "PressureBaseline",
    "PressureSnapshot",
    "PressurisingController",
    "RetrofitPlan",
    "ROLLBACK_OLD_BASELINE",
    "ROLLBACK_SAFE_INTERMEDIATE",
    "build_retrofit_plan",
    "pressure_consistent",
]
