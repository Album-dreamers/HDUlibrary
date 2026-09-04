"""BookingResult data model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class BookingResult:
    """Result of a booking attempt."""
    success: bool
    code: str
    message: str
    plan_id: Optional[str] = None
    seat_num: Optional[str] = None
    room_name: Optional[str] = None
    target_date: Optional[str] = None

    @property
    def already_reserved(self) -> bool:
        """The desired state exists, but this request did not create it."""
        return self.success and "已有预约" in str(self.message)

    @property
    def created_by_this_run(self) -> bool:
        """The booking API confirms that this request created a reservation."""
        return self.success and str(self.code) == "ok"

    def __str__(self) -> str:
        if self.already_reserved:
            status = "已有预约（非本次创建）"
        else:
            status = "成功" if self.success else "失败"
        parts = [f"[{status}]"]
        if self.plan_id:
            parts.append(f"方案: {self.plan_id}")
        if self.room_name and self.seat_num:
            parts.append(f"座位: {self.room_name}-{self.seat_num}")
        if self.target_date:
            parts.append(f"日期: {self.target_date}")
        if self.success:
            parts.append(self.message)
        else:
            parts.append(f"{self.message} (CODE={self.code})")
        return " | ".join(parts)

    @classmethod
    def from_api_response(cls, resp: dict, plan_id: str = None, seat_num: str = None,
                          room_name: str = None, target_date: str = None) -> "BookingResult":
        code = resp.get("CODE", "unknown")
        message = resp.get("MESSAGE", "")
        # The API reports an existing reservation as ParamError.  For an
        # idempotent automation run this is already the desired end state and
        # must stop retries instead of hammering the endpoint.
        success = code == "ok" or "已有预约" in str(message)
        return cls(
            success=success,
            code=code,
            message=message,
            plan_id=plan_id,
            seat_num=seat_num,
            room_name=room_name,
            target_date=target_date,
        )
