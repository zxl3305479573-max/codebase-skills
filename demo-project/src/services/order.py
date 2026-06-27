"""
Order service — 订单模块

订单创建、履约、取消、查询等全生命周期管理。
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.utils.db import get_db
from src.utils.cache import redis_client

logger = logging.getLogger(__name__)

ORDER_CREATED = "created"
ORDER_PAID = "paid"
ORDER_FULFILLING = "fulfilling"
ORDER_SHIPPED = "shipped"
ORDER_COMPLETED = "completed"
ORDER_CANCELLED = "cancelled"


class OrderService:
    """订单服务"""

    def __init__(self, user_id: str):
        self.user_id = user_id

    def create_order(self, items: list[dict],
                     address_id: str,
                     coupon_code: Optional[str] = None) -> dict:
        """创建订单"""
        # 1. 校验库存
        for item in items:
            if not self._check_stock(item["sku_id"], item["quantity"]):
                raise ValueError(f"Insufficient stock for {item['sku_id']}")

        # 2. 计算价格
        subtotal = self._calculate_subtotal(items)
        discount = Decimal("0")
        if coupon_code:
            discount = self._apply_coupon(coupon_code, subtotal)
        shipping = self._calculate_shipping(address_id, items)
        total = subtotal - discount + shipping

        # 3. 创建订单记录
        order_no = self._generate_order_no()
        db = get_db()
        db.execute(
            """INSERT INTO orders (order_no, user_id, address_id, subtotal,
               discount, shipping, total, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_no, self.user_id, address_id, float(subtotal),
             float(discount), float(shipping), float(total),
             ORDER_CREATED, datetime.now()),
        )
        db.commit()

        # 4. 扣减库存
        for item in items:
            self._deduct_stock(item["sku_id"], item["quantity"])

        # 5. 清除用户订单缓存
        redis_client.delete(f"order:list:{self.user_id}")

        logger.info(f"Order created: {order_no}, total={total}")
        return {"order_no": order_no, "total": float(total)}

    def _check_stock(self, sku_id: str, quantity: int) -> bool:
        """检查库存是否充足"""
        db = get_db()
        row = db.query(
            "SELECT stock FROM inventory WHERE sku_id = ?", (sku_id,)
        )
        return row is not None and row["stock"] >= quantity

    def _calculate_subtotal(self, items: list[dict]) -> Decimal:
        """计算商品小计"""
        db = get_db()
        total = Decimal("0")
        for item in items:
            row = db.query(
                "SELECT price FROM products WHERE sku_id = ?",
                (item["sku_id"],)
            )
            if row:
                total += Decimal(str(row["price"])) * item["quantity"]
        return total

    def _apply_coupon(self, coupon_code: str, subtotal: Decimal) -> Decimal:
        """应用优惠券"""
        from src.services.coupon import validate_coupon
        coupon = validate_coupon(coupon_code, self.user_id, subtotal)
        return coupon["discount_amount"] if coupon else Decimal("0")

    def _calculate_shipping(self, address_id: str,
                            items: list[dict]) -> Decimal:
        """计算运费"""
        total_weight = sum(
            item.get("weight", 0) * item["quantity"] for item in items
        )
        if total_weight < 1.0:
            return Decimal("6.00")
        elif total_weight < 5.0:
            return Decimal("12.00")
        else:
            return Decimal("20.00")

    def _generate_order_no(self) -> str:
        """生成订单号"""
        import uuid
        return f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}"

    def _deduct_stock(self, sku_id: str, quantity: int) -> None:
        """扣减库存"""
        db = get_db()
        db.execute(
            "UPDATE inventory SET stock = stock - ? WHERE sku_id = ?",
            (quantity, sku_id),
        )


def fulfill_order(order_no: str) -> bool:
    """
    订单履约 — 支付成功后触发

    流程：锁定库存 → 生成拣货单 → 通知仓库
    """
    logger.info(f"Fulfilling order: {order_no}")
    db = get_db()
    order = db.query(
        "SELECT * FROM orders WHERE order_no = ?", (order_no,)
    )

    if not order:
        logger.error(f"Order not found for fulfillment: {order_no}")
        return False

    if order["status"] != ORDER_PAID:
        logger.warning(f"Order {order_no} is not paid, cannot fulfill")
        return False

    db.execute(
        "UPDATE orders SET status = ? WHERE order_no = ?",
        (ORDER_FULFILLING, order_no),
    )
    db.commit()

    # 生成拣货单
    picking_list = generate_picking_list(order_no)
    # 通知仓库系统
    notify_warehouse(picking_list)

    redis_client.delete(f"order:{order_no}")
    logger.info(f"Order {order_no} fulfillment started")
    return True


def cancel_fulfillment(order_no: str) -> bool:
    """取消履约 — 退款时触发"""
    logger.info(f"Cancelling fulfillment for: {order_no}")
    db = get_db()
    order = db.query(
        "SELECT * FROM orders WHERE order_no = ?", (order_no,)
    )

    if not order:
        return False

    if order["status"] not in (ORDER_FULFILLING, ORDER_SHIPPED):
        logger.warning(
            f"Cannot cancel fulfillment for order {order_no} "
            f"in status {order['status']}"
        )
        return False

    # 回滚库存
    restore_inventory(order_no)
    db.execute(
        "UPDATE orders SET status = ? WHERE order_no = ?",
        (ORDER_CANCELLED, order_no),
    )
    db.commit()
    redis_client.delete(f"order:{order_no}")
    return True


def generate_picking_list(order_no: str) -> dict:
    """生成仓库拣货单"""
    db = get_db()
    items = db.query_all(
        """SELECT oi.sku_id, oi.quantity, p.name, p.warehouse_location
           FROM order_items oi
           JOIN products p ON oi.sku_id = p.sku_id
           WHERE oi.order_no = ?""",
        (order_no,),
    )
    return {"order_no": order_no, "items": items}


def notify_warehouse(picking_list: dict) -> None:
    """通知仓库系统"""
    logger.info(f"Notifying warehouse: {picking_list['order_no']}")
    # 实际发送到仓库 WMS 系统
    pass


def restore_inventory(order_no: str) -> None:
    """回滚库存"""
    db = get_db()
    items = db.query_all(
        "SELECT sku_id, quantity FROM order_items WHERE order_no = ?",
        (order_no,),
    )
    for item in items:
        db.execute(
            "UPDATE inventory SET stock = stock + ? WHERE sku_id = ?",
            (item["quantity"], item["sku_id"]),
        )
    db.commit()
