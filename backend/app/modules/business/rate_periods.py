"""结算时间轴的唯一变换与取值入口；截止日包含当天，None比例明确待配置。"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RatePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date | None = None
    rate: Decimal | None = Field(default=None, ge=0, le=1)


class RateOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["set", "edit", "end"] = "set"
    start_date: date
    end_date: date | None = None
    rate: Decimal | None = Field(
        default=None, ge=0, le=1, max_digits=12, decimal_places=8
    )
    target_start_date: date | None = None
    successor_rate: Decimal | None = Field(
        default=None, ge=0, le=1, max_digits=12, decimal_places=8
    )
    successor_end_date: date | None = None
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")
        if self.action != "end" and self.rate is None:
            raise ValueError("请填写比例")
        if self.action != "set" and self.target_start_date is None:
            raise ValueError("请选择要修改的时间段")
        if self.action == "set" and self.target_start_date is not None:
            raise ValueError("新增比例不能指定原时间段")
        if self.action == "end":
            if self.end_date is None or self.end_date == date.max:
                raise ValueError("请填写有效截止日期")
            if self.rate is not None or self.start_date != self.target_start_date:
                raise ValueError("结束操作不能移动开始日期或修改当前比例")
            if self.successor_end_date is not None and (
                self.successor_rate is None or self.successor_end_date <= self.end_date
            ):
                raise ValueError("接续比例结束日期必须晚于当前截止日期")
        elif self.successor_rate is not None or self.successor_end_date is not None:
            raise ValueError("只有结束操作可以同时设置接续比例")
        return self


def _end(period: RatePeriod) -> date:
    return period.end_date or date.max


def _normalise(periods: list[RatePeriod]) -> list[RatePeriod]:
    """保持最早触及日之后完整覆盖；显式空档不会因编辑而退回默认0%。"""
    result: list[RatePeriod] = []
    for current in sorted(periods, key=lambda p: p.start_date):
        if result and _end(result[-1]) >= current.start_date:
            raise ValueError("比例时间段不能重叠")
        if result and _end(result[-1]) + timedelta(days=1) < current.start_date:
            result.append(
                RatePeriod(
                    start_date=_end(result[-1]) + timedelta(days=1),
                    end_date=current.start_date - timedelta(days=1),
                )
            )
        if (
            result
            and result[-1].rate == current.rate
            and _end(result[-1]) + timedelta(days=1) == current.start_date
        ):
            result[-1] = result[-1].model_copy(update={"end_date": current.end_date})
        else:
            result.append(current)
    if result and _end(result[-1]) < date.max:
        result.append(RatePeriod(start_date=_end(result[-1]) + timedelta(days=1)))
    return result


def _overlay(periods: list[RatePeriod], incoming: RatePeriod) -> list[RatePeriod]:
    result = []
    stop = _end(incoming)
    for old in periods:
        if _end(old) < incoming.start_date or old.start_date > stop:
            result.append(old)
            continue
        if old.start_date < incoming.start_date:
            result.append(
                old.model_copy(
                    update={"end_date": incoming.start_date - timedelta(days=1)}
                )
            )
        if _end(old) > stop:
            result.append(
                old.model_copy(update={"start_date": stop + timedelta(days=1)})
            )
    return _normalise([*result, incoming])


def transform_periods(
    periods: list[RatePeriod], operation: RateOperation
) -> list[RatePeriod]:
    result = list(periods)
    if operation.action != "set":
        target = next(
            (
                p
                for p in periods
                if p.start_date == operation.target_start_date and p.rate is not None
            ),
            None,
        )
        if target is None:
            raise ValueError("原时间段已变化，请刷新后重新选择")
        if operation.action == "end":
            assert operation.end_date is not None
            if operation.end_date > _end(target):
                raise ValueError("截止日期不能晚于原结束日期；延长请使用修改")
        # 先将原段置为明确空档；移动或缩短不会复活已被替代的历史记录。
        result = [
            p.model_copy(update={"rate": None}) if p == target else p for p in result
        ]
        if operation.action == "end":
            assert operation.end_date is not None
            result = _overlay(
                result, target.model_copy(update={"end_date": operation.end_date})
            )
            if operation.successor_rate is not None:
                result = _overlay(
                    result,
                    RatePeriod(
                        start_date=operation.end_date + timedelta(days=1),
                        end_date=operation.successor_end_date,
                        rate=operation.successor_rate,
                    ),
                )
            return result
    return _overlay(
        result,
        RatePeriod(
            start_date=operation.start_date,
            end_date=operation.end_date,
            rate=operation.rate,
        ),
    )


def period_rate(periods: list[RatePeriod], day: date) -> Decimal | None:
    for p in periods:
        if p.start_date <= day <= _end(p):
            return p.rate
    # 首次触及日期之前沿用原默认；首次触及之后始终有显式有效或待配置段。
    return Decimal(0)
