"""Compatibility facade for the durable execute-order queue adapter."""

from trader.infrastructure.queue.order_handler import (
    make_execute_order_handler as make_execute_order_handler,
)

__all__ = ["make_execute_order_handler"]
