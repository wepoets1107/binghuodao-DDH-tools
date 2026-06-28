from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import AppConfig, AssetConfig


@dataclass
class HedgeDecision:
    currency: str
    reason: str
    should_trade: bool
    message: str
    net_delta: float
    target_delta: float
    delta_gap: float
    active_threshold: float
    side: str
    amount: float
    requested_coin_amount: float
    coin_amount: float
    exchange_amount: float
    contract_size: float
    min_trade_amount: float
    min_coin_unit: float
    contract_coin_unit: float
    price: float | None
    order_type: str
    instrument_name: str
    estimated_post_delta: float

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "reason": self.reason,
            "should_trade": self.should_trade,
            "message": self.message,
            "net_delta": self.net_delta,
            "target_delta": self.target_delta,
            "delta_gap": self.delta_gap,
            "active_threshold": self.active_threshold,
            "side": self.side,
            "amount": self.amount,
            "requested_coin_amount": self.requested_coin_amount,
            "coin_amount": self.coin_amount,
            "exchange_amount": self.exchange_amount,
            "contract_size": self.contract_size,
            "min_trade_amount": self.min_trade_amount,
            "min_coin_unit": self.min_coin_unit,
            "contract_coin_unit": self.contract_coin_unit,
            "price": self.price,
            "order_type": self.order_type,
            "instrument_name": self.instrument_name,
            "estimated_post_delta": self.estimated_post_delta,
        }


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def round_price_to_tick(value: float, tick_size: float, side: str) -> float:
    if tick_size <= 0:
        return round(value, 2)
    units = value / tick_size
    if side == "buy":
        rounded = math.ceil(units - 1e-12) * tick_size
    else:
        rounded = math.floor(units + 1e-12) * tick_size
    return round(rounded, 8)


def build_limit_price(side: str, best_bid: float, best_ask: float, slippage_bps: float, tick_size: float) -> float | None:
    if side == "buy" and best_bid > 0:
        return round_price_to_tick(best_bid, tick_size, side)
    if side == "sell" and best_ask > 0:
        return round_price_to_tick(best_ask, tick_size, side)
    return None


def hedge_coin_amount_from_delta(delta_gap: float, asset: AssetConfig) -> float:
    return abs(delta_gap) * asset.hedge_ratio


def exchange_amount_from_coin_amount(coin_amount: float, index_price: float, contract_size: float) -> float:
    if coin_amount <= 0 or index_price <= 0:
        return 0.0
    raw_exchange_amount = coin_amount * index_price
    return round_step(raw_exchange_amount, contract_size)


def coin_amount_from_exchange_amount(exchange_amount: float, index_price: float) -> float:
    if exchange_amount <= 0 or index_price <= 0:
        return 0.0
    return exchange_amount / index_price


def estimated_delta_change(side: str, exchange_amount: float, index_price: float) -> float:
    if exchange_amount <= 0 or index_price <= 0:
        return 0.0
    change = exchange_amount / index_price
    return change if side == "buy" else -change


def decide_hedge(
    currency: str,
    asset: AssetConfig,
    config: AppConfig,
    net_delta: float,
    market: dict,
    reason: str,
    force: bool = False,
) -> HedgeDecision:
    target_delta = asset.target_delta
    delta_gap = net_delta - target_delta
    side = "sell" if delta_gap > 0 else "buy"
    index_price = float(market.get("index_price") or market.get("mark_price") or 0)
    contract_size = float(market.get("contract_size") or 1.0)
    min_trade_amount = float(market.get("min_trade_amount") or contract_size)
    min_coin_unit = coin_amount_from_exchange_amount(min_trade_amount, index_price)
    contract_coin_unit = coin_amount_from_exchange_amount(contract_size, index_price)
    requested_coin_amount = hedge_coin_amount_from_delta(delta_gap, asset)
    capped_coin_amount = requested_coin_amount
    if asset.max_order_amount > 0:
        capped_coin_amount = min(requested_coin_amount, asset.max_order_amount)
    exchange_amount = exchange_amount_from_coin_amount(capped_coin_amount, index_price, contract_size)
    coin_amount = coin_amount_from_exchange_amount(exchange_amount, index_price)
    active_threshold = asset.positive_trigger_delta if delta_gap >= 0 else asset.negative_trigger_delta
    threshold_hit = abs(delta_gap) >= active_threshold
    should_trade = threshold_hit or force
    message = "Ready."

    if not asset.enabled and not force:
        should_trade = False
        message = "Threshold hedge is disabled."
    elif index_price <= 0:
        should_trade = False
        message = "Missing index price."
    elif not force and not threshold_hit:
        should_trade = False
        message = "Delta is inside threshold."
    elif asset.max_order_amount <= 0:
        should_trade = False
        message = "Maximum order amount must be positive."
    elif requested_coin_amount < asset.min_order_amount:
        should_trade = False
        message = "Calculated coin amount is below minimum."
    elif exchange_amount < min_trade_amount:
        should_trade = False
        message = "Calculated Deribit order amount is below exchange minimum."
    elif requested_coin_amount > asset.max_order_amount:
        message = "Order amount is capped by maximum coin amount."

    price = None
    if config.order_type == "limit":
        price = build_limit_price(
            side,
            float(market.get("best_bid") or 0),
            float(market.get("best_ask") or 0),
            config.slippage_bps,
            float(market.get("tick_size") or 0.01),
        )
        if should_trade and price is None:
            should_trade = False
            message = "Missing best bid/ask for limit order."

    delta_change = estimated_delta_change(side, exchange_amount, index_price)
    return HedgeDecision(
        currency=currency,
        reason=reason,
        should_trade=should_trade,
        message=message,
        net_delta=net_delta,
        target_delta=target_delta,
        delta_gap=delta_gap,
        active_threshold=active_threshold,
        side=side,
        amount=exchange_amount,
        requested_coin_amount=requested_coin_amount,
        coin_amount=coin_amount,
        exchange_amount=exchange_amount,
        contract_size=contract_size,
        min_trade_amount=min_trade_amount,
        min_coin_unit=min_coin_unit,
        contract_coin_unit=contract_coin_unit,
        price=price,
        order_type=config.order_type,
        instrument_name=asset.hedge_instrument,
        estimated_post_delta=net_delta + delta_change,
    )


def order_label(currency: str, reason: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"ddh-{currency.lower()}-{reason}-{stamp}"
