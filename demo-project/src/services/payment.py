"""
Payment service — 支付模块

处理支付、退款、回调通知等核心支付逻辑。
"""

import logging
from decimal import Decimal
from typing import Optional

from src.utils.db import get_db
from src.utils.cache import redis_client

logger = logging.getLogger(__name__)

# 支付状态常量
PAYMENT_PENDING = "pending"
PAYMENT_SUCCESS = "success"
PAYMENT_FAILED = "failed"
PAYMENT_REFUNDED = "refunded"


class PaymentGateway:
    """第三方支付网关封装"""

    def __init__(self, api_key: str, merchant_id: str):
        self.api_key = api_key
        self.merchant_id = merchant_id

    def create_order(self, amount: Decimal, order_no: str,
                     currency: str = "CNY") -> dict:
        """创建支付订单"""
        logger.info(f"Creating payment order: {order_no}, amount={amount}")
        params = {
            "merchant_id": self.merchant_id,
            "order_no": order_no,
            "amount": float(amount),
            "currency": currency,
        }
        response = self._call_api("/v1/pay/create", params)
        return response

    def query_order(self, order_no: str) -> dict:
        """查询订单支付状态"""
        params = {"merchant_id": self.merchant_id, "order_no": order_no}
        return self._call_api("/v1/pay/query", params)

    def refund(self, order_no: str, amount: Optional[Decimal] = None,
               reason: str = "") -> dict:
        """发起退款"""
        logger.info(f"Refunding order: {order_no}, amount={amount}")
        params = {
            "merchant_id": self.merchant_id,
            "order_no": order_no,
            "reason": reason,
        }
        if amount:
            params["amount"] = float(amount)
        response = self._call_api("/v1/pay/refund", params)
        return response

    def _call_api(self, endpoint: str, params: dict) -> dict:
        """调用支付网关 API"""
        import hashlib
        sign = hashlib.md5(
            f"{params}{self.api_key}".encode()
        ).hexdigest()
        params["sign"] = sign
        # 实际项目中使用 requests.post
        logger.debug(f"Calling {endpoint} with {params}")
        return {"code": 0, "msg": "ok", "data": params}


def handle_payment_callback(payload: dict) -> bool:
    """
    处理支付回调通知

    BUG: 偶发空指针异常 — 当 payload 中缺少 'order_no' 字段时，
    process_payment_result 会抛出 AttributeError。
    复现条件：第三方支付网关超时重发时回调体不完整。
    """
    event_type = payload.get("event_type")
    order_no = payload.get("order_no")
    trade_no = payload.get("trade_no")

    logger.info(f"Payment callback: event={event_type}, order={order_no}")

    if event_type == "payment.success":
        return process_payment_success(order_no, trade_no)
    elif event_type == "payment.refund":
        return process_refund(order_no, trade_no)
    else:
        logger.warning(f"Unknown event type: {event_type}")
        return False


def process_payment_success(order_no: str, trade_no: str) -> bool:
    """处理支付成功"""
    db = get_db()
    order = db.query("SELECT * FROM orders WHERE order_no = ?", (order_no,))
    if not order:
        logger.error(f"Order not found: {order_no}")
        return False

    db.execute(
        "UPDATE orders SET status = ?, trade_no = ?, paid_at = NOW() WHERE order_no = ?",
        (PAYMENT_SUCCESS, trade_no, order_no),
    )
    db.commit()

    # 清除缓存
    redis_client.delete(f"order:{order_no}")
    redis_client.delete(f"order:list:{order['user_id']}")

    # 触发后续流程
    from src.services.order import fulfill_order
    fulfill_order(order_no)

    logger.info(f"Payment success processed: {order_no}")
    return True


def process_refund(order_no: str, trade_no: str) -> bool:
    """处理退款回调"""
    db = get_db()
    order = db.query("SELECT * FROM orders WHERE order_no = ?", (order_no,))

    if not order:
        logger.error(f"Order not found for refund: {order_no}")
        return False

    if order["status"] != PAYMENT_SUCCESS:
        logger.warning(
            f"Cannot refund non-paid order: {order_no}, status={order['status']}"
        )
        return False

    db.execute(
        "UPDATE orders SET status = ?, refund_at = NOW() WHERE order_no = ?",
        (PAYMENT_REFUNDED, order_no),
    )
    db.commit()

    redis_client.delete(f"order:{order_no}")

    from src.services.order import cancel_fulfillment
    cancel_fulfillment(order_no)

    logger.info(f"Refund processed: {order_no}")
    return True


def verify_payment_sign(payload: dict, sign: str) -> bool:
    """验证支付回调签名"""
    import hashlib
    expected = hashlib.md5(
        f"{payload}{PAYMENT_SECRET}".encode()
    ).hexdigest()
    return expected == sign


PAYMENT_SECRET = "sk_live_xxxxxxxxxxxxx"
