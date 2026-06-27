"""
Payment API — 支付相关接口
"""

from decimal import Decimal
from typing import Optional

from src.services.payment import PaymentGateway, handle_payment_callback
from src.services.auth import AuthService
from src.utils.db import get_db

auth_service = AuthService()


def create_payment(token: str, order_no: str, amount: float,
                   currency: str = "CNY") -> dict:
    """POST /api/v1/payment/create"""
    # 验证用户身份
    user_id = auth_service.verify_token(token)
    if not user_id:
        return {"code": 401, "msg": "Unauthorized"}

    # 验证订单归属
    db = get_db()
    order = db.query(
        "SELECT * FROM orders WHERE order_no = ? AND user_id = ?",
        (order_no, user_id),
    )
    if not order:
        return {"code": 404, "msg": "Order not found"}

    # 创建支付
    gateway = PaymentGateway(
        api_key="sk_test_xxx",
        merchant_id="MCH_123456",
    )
    result = gateway.create_order(
        amount=Decimal(str(amount)),
        order_no=order_no,
        currency=currency,
    )

    return {"code": 0, "msg": "ok", "data": result}


def payment_callback(payload: dict) -> dict:
    """POST /api/v1/payment/callback — 第三方支付回调入口"""
    result = handle_payment_callback(payload)
    if result:
        return {"code": 0, "msg": "ok"}
    else:
        return {"code": 500, "msg": "callback processing failed"}


def query_payment(token: str, order_no: str) -> dict:
    """GET /api/v1/payment/query"""
    user_id = auth_service.verify_token(token)
    if not user_id:
        return {"code": 401, "msg": "Unauthorized"}

    gateway = PaymentGateway(api_key="sk_test_xxx", merchant_id="MCH_123456")
    result = gateway.query_order(order_no)
    return {"code": 0, "msg": "ok", "data": result}


def refund_payment(token: str, order_no: str,
                   reason: str = "") -> dict:
    """POST /api/v1/payment/refund — 申请退款"""
    user_id = auth_service.verify_token(token)
    if not user_id:
        return {"code": 401, "msg": "Unauthorized"}

    gateway = PaymentGateway(api_key="sk_test_xxx", merchant_id="MCH_123456")
    result = gateway.refund(order_no, reason=reason)
    return {"code": 0, "msg": "ok", "data": result}
