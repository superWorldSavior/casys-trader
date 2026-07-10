"""Narrow control-flow contracts for task handlers and queue workers."""


class RetryableError(Exception):
    """Signal a transient handler failure that can be safely requeued."""

    def __init__(self, *args: object, is_overload: bool = False) -> None:
        super().__init__(*args)
        self.is_overload = is_overload
