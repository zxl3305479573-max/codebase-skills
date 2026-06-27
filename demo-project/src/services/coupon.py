"""
Coupon service — 优惠券模块
"""

import logging
from datetime import datetime
from decimal import Decimal

from src.utils.db import get_db

logger = logging.getLogger(__name__)


def validate_coupon(code: str, user_id: str,
                    subtotal: Decimal) -> dict | None:
    """验证优惠券是否可用"""
    db = get_db()
    coupon = db.query(
        "SELECT * FROM coupons WHERE code = ? AND status = 'active'",
        (code,),
    )

    if not coupon:
        logger.info(f"Coupon not found or inactive: {code}")
        return None

    # 检查过期
    now = datetime.now()
    if now < coupon["start_time"] or now > coupon["end_time"]:
        logger.info(f"Coupon expired: {code}")
        return None

    # 检查最低消费
    if subtotal < Decimal(str(coupon["min_amount"])):
        logger.info(
            f"Order amount {subtotal} below coupon minimum "
            f"{coupon['min_amount']}"
        )
        return None

    # 检查用户使用次数
    usage_count = db.query(
        """SELECT COUNT(*) as cnt FROM coupon_usage
           WHERE coupon_code = ? AND user_id = ?""",
        (code, user_id),
    )
    if usage_count and usage_count["cnt"] >= coupon["max_usage_per_user"]:
        logger.info(f"User {user_id} exceeded coupon usage limit for {code}")
        return None

    discount = _calculate_discount(coupon, subtotal)
    return {
        "code": code,
        "discount_amount": discount,
        "coupon_type": coupon["type"],
        "description": coupon["description"],
    }


def _calculate_discount(coupon: dict, subtotal: Decimal) -> Decimal:
    """计算折扣金额"""
    if coupon["type"] == "fixed":
        return Decimal(str(coupon["value"]))
    elif coupon["type"] == "percentage":
        discount = subtotal * Decimal(str(coupon["value"])) / Decimal("100")
        max_discount = Decimal(str(coupon.get("max_discount", 9999)))
        return min(discount, max_discount)
    else:
        return Decimal("0")
