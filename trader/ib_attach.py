from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class IBAttachBackoff:
    retry_after: timedelta
    next_attempt_at: datetime | None = None
    attached: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.retry_after <= timedelta(0):
            raise ValueError("retry_after must be > 0")

    def due(self, now: datetime) -> bool:
        if self.attached:
            return False
        return self.next_attempt_at is None or now >= self.next_attempt_at

    def record_failure(self, now: datetime) -> None:
        self.attached = False
        self.next_attempt_at = now + self.retry_after

    def record_success(self) -> None:
        self.attached = True
