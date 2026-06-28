from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.config import AppConfig, CONFIG_PATH, get_env_config
from app.deribit_client import DeribitClient
from app.scheduler import ScheduleGate
from app.store import append_event, load_config, recent_events, save_config, utc_now_iso
from app.strategy import coin_amount_from_exchange_amount, decide_hedge, exchange_amount_from_coin_amount, order_label


REBALANCE_SETTLE_SECONDS = 1.0
MAX_SPLIT_ORDERS = 20
MAX_MAKER_REFRESH_RECOVERY_SECONDS = 300


class DDHRuntime:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.running = False
        self.last_error: str | None = None
        self.last_check_at: str | None = None
        self.last_results: dict[str, Any] = {}
        self.last_trade_at: dict[str, datetime] = {}
        self.schedule_gate = ScheduleGate()
        self.client: DeribitClient | None = None
        self.client_signature: tuple[str, str, str] | None = None
        self.open_order_cache: dict[str, dict[str, Any]] = {}
        self.order_stream_signature: tuple[Any, ...] | None = None
        self.fill_refresh_tasks: dict[str, asyncio.Task] = {}
        self.state_lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        config = load_config()
        env = get_env_config(config.mode)
        return {
            "running": self.running,
            "last_error": self.last_error,
            "last_check_at": self.last_check_at,
            "last_results": self.last_results,
            "events": recent_events(80),
            "config_path": str(CONFIG_PATH),
            "credentials": {
                "configured": bool(env.deribit_client_id and env.deribit_client_secret),
            },
        }

    async def open_ddh_orders(self) -> list[dict[str, Any]]:
        await self.ensure_open_order_stream()
        return sorted(
            self.open_order_cache.values(),
            key=lambda row: row.get("creation_timestamp") or 0,
            reverse=True,
        )

    async def ensure_open_order_stream(self) -> None:
        config = load_config()
        client = await self.get_client(config)
        env = get_env_config(config.mode)
        instruments = tuple(sorted(asset.hedge_instrument for asset in config.assets.values()))
        signature = (config.mode, env.deribit_client_id, env.deribit_client_secret, instruments)
        channels = [f"user.orders.{instrument}.raw" for instrument in instruments]
        if self.order_stream_signature == signature and client.has_active_subscriptions(channels):
            return

        self.open_order_cache = {}
        for currency in config.assets:
            for order in await client.open_orders_by_currency(currency):
                self.apply_order_update(order, currency=currency, record_fill=False)

        await client.subscribe(channels, self.handle_order_subscription)
        self.order_stream_signature = signature

    def handle_order_subscription(self, params: dict[str, Any]) -> None:
        data = params.get("data")
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if isinstance(row, dict):
                self.apply_order_update(row, record_fill=True)

    def apply_order_update(self, order: dict[str, Any], currency: str | None = None, record_fill: bool = True) -> None:
        label = str(order.get("label") or "")
        if not label.startswith("ddh-"):
            return
        order_id = str(order.get("order_id") or label)
        state = str(order.get("order_state") or "")
        instrument = str(order.get("instrument_name") or "")
        inferred_currency = self.infer_currency(order, currency)
        previous = self.open_order_cache.get(order_id, {})
        previous_filled = float(previous.get("filled_amount") or 0)
        filled_amount = float(order.get("filled_amount") or 0)
        if state and state != "open":
            filled_changed = record_fill and filled_amount > previous_filled
            if record_fill and filled_amount > previous_filled:
                self.record_fill_event(order, inferred_currency, filled_amount - previous_filled)
            self.open_order_cache.pop(order_id, None)
            if filled_changed and state == "filled" and not self.has_open_orders_for_currency(inferred_currency):
                self.request_fill_refresh(inferred_currency)
            return
        amount = float(order.get("amount") or 0)
        price = float(order.get("price") or order.get("average_price") or 0)
        if record_fill and filled_amount > previous_filled:
            self.record_fill_event(order, inferred_currency, filled_amount - previous_filled)
        self.open_order_cache[order_id] = {
            "order_id": order_id,
            "currency": inferred_currency,
            "label": label,
            "instrument_name": instrument,
            "direction": order.get("direction", ""),
            "amount": amount,
            "coin_amount": amount / price if price > 0 else 0.0,
            "filled_amount": filled_amount,
            "remaining_amount": max(amount - filled_amount, 0.0),
            "remaining_coin_amount": max(amount - filled_amount, 0.0) / price if price > 0 else 0.0,
            "price": price,
            "order_state": state or "open",
            "post_only": bool(order.get("post_only")),
            "creation_timestamp": order.get("creation_timestamp"),
        }

    def has_open_orders_for_currency(self, currency: str) -> bool:
        return any(
            str(row.get("currency") or "").upper() == currency.upper()
            for row in self.open_order_cache.values()
        )

    def request_fill_refresh(self, currency: str) -> None:
        if not currency:
            return
        existing = self.fill_refresh_tasks.get(currency)
        if existing and not existing.done():
            return
        self.fill_refresh_tasks[currency] = asyncio.create_task(self.refresh_after_full_fill(currency))

    async def refresh_after_full_fill(self, currency: str) -> None:
        try:
            await asyncio.sleep(REBALANCE_SETTLE_SECONDS)
            await self.run_once(reason="fill_refresh", force=False, execute=False, currencies=[currency])
            append_event("info", "fill_refresh_completed", {"currency": currency})
        except Exception as exc:
            self.last_error = str(exc)
            append_event("error", "fill_refresh_failed", {"currency": currency, "message": str(exc)})
        finally:
            current = asyncio.current_task()
            if self.fill_refresh_tasks.get(currency) is current:
                self.fill_refresh_tasks.pop(currency, None)

    def infer_currency(self, order: dict[str, Any], currency: str | None = None) -> str:
        if currency:
            return str(currency).upper()
        instrument = str(order.get("instrument_name") or "")
        if "-" in instrument:
            return instrument.split("-", 1)[0].upper()
        label = str(order.get("label") or "")
        parts = label.split("-")
        if len(parts) >= 2 and parts[0] == "ddh":
            return parts[1].upper()
        return ""

    def record_fill_event(self, order: dict[str, Any], currency: str | None, fill_amount: float) -> None:
        price = float(order.get("average_price") or order.get("price") or 0)
        append_event(
            "info",
            "order_filled",
            {
                "currency": currency or "",
                "label": order.get("label", ""),
                "order_id": order.get("order_id", ""),
                "instrument_name": order.get("instrument_name", ""),
                "direction": order.get("direction", ""),
                "fill_amount": fill_amount,
                "coin_amount": fill_amount / price if price > 0 else 0.0,
                "price": price,
                "filled_amount": float(order.get("filled_amount") or 0),
                "order_state": order.get("order_state", ""),
            },
        )

    async def cancel_orders_for_config_change(self, old_config: AppConfig, new_config: AppConfig) -> list[dict[str, Any]]:
        changed = []
        for currency, old_asset in old_config.assets.items():
            new_asset = new_config.assets.get(currency)
            if not new_asset:
                changed.append(currency)
                continue
            if (
                old_asset.target_delta != new_asset.target_delta
                or old_asset.hedge_instrument != new_asset.hedge_instrument
                or old_asset.max_order_amount != new_asset.max_order_amount
                or old_asset.min_order_amount != new_asset.min_order_amount
                or old_asset.hedge_ratio != new_asset.hedge_ratio
            ):
                changed.append(currency)
        if not changed:
            return []
        client = await self.get_client(old_config)
        cancelled = []
        for currency in changed:
            for order in await client.cancel_ddh_orders(currency):
                self.apply_order_update(order, currency=currency, record_fill=True)
                cancelled.append(order)
        if cancelled:
            append_event("info", "config_change_orders_cancelled", {"currencies": changed, "orders": cancelled})
        return cancelled

    async def maintain_stale_maker_orders(self, config: AppConfig) -> None:
        await self.ensure_open_order_stream()
        now_ms = datetime.now(UTC).timestamp() * 1000
        stale_order_ids_by_currency: dict[str, set[str]] = {}
        refresh_by_currency: set[str] = set()
        cancel_only_by_currency: set[str] = set()
        recovery_window_ms = max(config.maker_wait_seconds * 3, MAX_MAKER_REFRESH_RECOVERY_SECONDS) * 1000
        for order in list(self.open_order_cache.values()):
            created = float(order.get("creation_timestamp") or 0)
            if not created:
                continue
            age_ms = now_ms - created
            if age_ms < config.maker_wait_seconds * 1000:
                continue
            currency = str(order.get("currency") or "")
            order_id = str(order.get("order_id") or "")
            if not currency or not order_id:
                continue
            stale_order_ids_by_currency.setdefault(currency, set()).add(order_id)
            if age_ms <= recovery_window_ms:
                refresh_by_currency.add(currency)
            else:
                cancel_only_by_currency.add(currency)
        if not stale_order_ids_by_currency:
            return
        client = await self.get_client(config)
        for currency, order_ids in stale_order_ids_by_currency.items():
            cancelled = await client.cancel_ddh_orders(currency, order_ids=order_ids)
            for order in cancelled:
                self.apply_order_update(order, currency=currency, record_fill=True)
            if cancelled:
                should_refresh = currency in refresh_by_currency
                append_event(
                    "info",
                    "maker_orders_refreshed" if should_refresh else "maker_orders_expired_cancelled",
                    {"currency": currency, "cancelled": cancelled},
                )
                if not should_refresh:
                    continue
                await self.run_once(reason="maker_refresh", force=True, execute=True, currencies=[currency])

    async def start(self) -> dict[str, Any]:
        if self.task and not self.task.done():
            self.running = True
            return {"running": True, "message": "DDH is already running."}
        self.running = True
        self.task = asyncio.create_task(self._loop())
        append_event("info", "runtime_started", {})
        return {"running": True, "message": "DDH has started."}

    async def stop(self) -> dict[str, Any]:
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.close()
            self.client = None
            self.client_signature = None
            self.order_stream_signature = None
            self.open_order_cache = {}
        for task in self.fill_refresh_tasks.values():
            task.cancel()
        self.fill_refresh_tasks = {}
        append_event("info", "runtime_stopped", {})
        return {"running": False, "message": "DDH has stopped."}

    async def get_client(self, config: AppConfig) -> DeribitClient:
        env = get_env_config(config.mode)
        signature = (config.mode, env.deribit_client_id, env.deribit_client_secret)
        if self.client and self.client_signature == signature:
            return self.client
        if self.client:
            await self.client.close()
        self.client = DeribitClient(config, env)
        self.client_signature = signature
        return self.client

    async def _loop(self) -> None:
        while self.running:
            config = load_config()
            try:
                await self.maintain_stale_maker_orders(config)
                for currency, asset in config.assets.items():
                    due, due_key = self.schedule_gate.due(asset.schedule, namespace=currency)
                    await self.run_once(
                        reason="schedule",
                        force=asset.schedule.force_rebalance,
                        execute=True,
                        only_if_due=True,
                        due=due,
                        due_key=due_key,
                        currencies=[currency],
                    )
                await self.run_once(reason="threshold", force=False, execute=True)
            except Exception as exc:
                self.last_error = str(exc)
                append_event("error", "loop_error", {"message": str(exc)})
            await asyncio.sleep(config.loop_interval_seconds)

    async def run_once(
        self,
        reason: str = "manual_preview",
        force: bool = False,
        execute: bool = False,
        only_if_due: bool = False,
        due: bool = True,
        due_key: str = "",
        currencies: list[str] | None = None,
    ) -> dict[str, Any]:
        if only_if_due and not due:
            return {"skipped": True, "reason": reason, "message": "Schedule is not due."}
        config = load_config()
        client = await self.get_client(config)
        results: dict[str, Any] = {}
        selected = [item.upper() for item in currencies] if currencies else list(config.assets.keys())
        for currency in selected:
            asset = config.assets.get(currency)
            if asset is None:
                results[currency] = {"enabled": False, "error": "Unsupported currency."}
                continue
            result = await self.evaluate_asset(client, config, currency, reason, force, execute)
            results[currency] = result
        checked_at = utc_now_iso()
        async with self.state_lock:
            self.last_check_at = checked_at
            if set(selected) >= set(config.assets.keys()):
                self.last_results = results
            else:
                merged = dict(self.last_results)
                merged.update(results)
                self.last_results = merged
        append_event(
            "info",
            "check_completed",
            {"reason": reason, "force": force, "execute": execute, "due_key": due_key, "currencies": selected, "results": results},
        )
        return {"checked_at": checked_at, "results": results}

    async def evaluate_asset(
        self,
        client: DeribitClient,
        config: AppConfig,
        currency: str,
        reason: str,
        force: bool,
        execute: bool,
    ) -> dict[str, Any]:
        asset = config.assets[currency]
        cancelled_orders: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []

        async def read_state():
            current_portfolio = await client.portfolio_delta(currency, asset)
            current_market = await client.order_book(asset.hedge_instrument)
            if not current_market.get("index_price"):
                current_market["index_price"] = await client.index_price(currency)
            current_decision = decide_hedge(
                currency,
                asset,
                config,
                float(current_portfolio["net_delta"]),
                current_market,
                reason,
                force=force,
            )
            return current_portfolio, current_market, current_decision

        portfolio, market, decision = await read_state()
        risk_message = self.risk_message(
            config,
            currency,
            decision.as_dict(),
            execute,
            ignore_cooldown=reason in {"immediate", "maker_refresh"},
        )
        order_result: dict[str, Any] | None = None
        executed = False

        if execute and decision.should_trade and not risk_message:
            order_plan = self.order_plan(decision.as_dict(), asset, market)
            if not order_plan:
                risk_message = "Calculated order amount is below minimum after contract rounding."
            if order_plan and not config.dry_run:
                cancelled_orders = await client.cancel_ddh_orders(currency)
                if cancelled_orders:
                    for order in cancelled_orders:
                        self.apply_order_update(order, currency=currency)
                    append_event(
                        "info",
                        "stale_orders_cancelled",
                        {"currency": currency, "count": len(cancelled_orders), "orders": cancelled_orders},
                    )

            if order_plan:
                base_label = order_label(currency, reason)
                for attempt_index, planned_decision in enumerate(order_plan):
                    label = f"{base_label}-{attempt_index + 1:02d}" if len(order_plan) > 1 else base_label
                    if config.dry_run:
                        order_result = {"dry_run": True, "label": label, "order": planned_decision}
                        executed = True
                    else:
                        order_result = await client.place_order(
                            planned_decision["side"],
                            planned_decision["instrument_name"],
                            planned_decision["amount"],
                            "limit",
                            planned_decision["price"],
                            label,
                        )
                        executed = True
                        if isinstance(order_result, dict):
                            self.apply_order_update(order_result.get("order") or {}, currency=currency)

                    attempt = {
                        "attempt": attempt_index + 1,
                        "label": label,
                        "decision": planned_decision,
                        "result": order_result,
                    }
                    attempts.append(attempt)
                    append_event(
                        "warning" if config.dry_run else "info",
                        "order_submitted",
                        {"currency": currency, "dry_run": config.dry_run, **attempt},
                    )

            if executed:
                self.last_trade_at[currency] = datetime.now(UTC)
                if not config.dry_run:
                    await asyncio.sleep(REBALANCE_SETTLE_SECONDS)
                    portfolio, market, decision = await read_state()
                    risk_message = self.risk_message(
                        config,
                        currency,
                        decision.as_dict(),
                        execute,
                        ignore_cooldown=True,
                    )

        return {
            "portfolio": portfolio,
            "market": market,
            "decision": decision.as_dict(),
            "risk_message": risk_message,
            "executed": executed,
            "order_result": order_result,
            "attempts": attempts,
            "cancelled_orders": cancelled_orders,
        }

    def order_plan(self, decision: dict[str, Any], asset, market: dict[str, Any]) -> list[dict[str, Any]]:
        if not decision.get("should_trade"):
            return []
        index_price = float(market.get("index_price") or market.get("mark_price") or 0)
        contract_size = float(decision.get("contract_size") or 1.0)
        min_trade_amount = float(decision.get("min_trade_amount") or contract_size)
        requested_coin_amount = float(decision.get("requested_coin_amount") or 0.0)
        max_coin_amount = float(asset.max_order_amount or 0.0)
        min_coin_amount = float(asset.min_order_amount or 0.0)
        if index_price <= 0 or requested_coin_amount <= 0 or max_coin_amount <= 0:
            return []

        remaining = requested_coin_amount
        plan: list[dict[str, Any]] = []
        for _ in range(MAX_SPLIT_ORDERS):
            if remaining < min_coin_amount:
                break
            chunk_coin = min(remaining, max_coin_amount)
            exchange_amount = exchange_amount_from_coin_amount(chunk_coin, index_price, contract_size)
            coin_amount = coin_amount_from_exchange_amount(exchange_amount, index_price)
            if exchange_amount < min_trade_amount or coin_amount < min_coin_amount:
                break
            planned = dict(decision)
            planned.update(
                {
                    "amount": exchange_amount,
                    "requested_coin_amount": chunk_coin,
                    "coin_amount": coin_amount,
                    "exchange_amount": exchange_amount,
                    "order_type": "limit",
                    "message": "Maker split order planned.",
                }
            )
            plan.append(planned)
            remaining -= coin_amount
        return plan

    def risk_message(
        self,
        config: AppConfig,
        currency: str,
        decision: dict[str, Any],
        execute: bool,
        ignore_cooldown: bool = False,
    ) -> str:
        if not execute:
            return ""
        if not decision.get("should_trade"):
            return str(decision.get("message") or "No trade required.")
        if not config.dry_run and not config.live_trading_armed:
            return "Live trading is locked. Enable live_trading_armed before real orders."
        last = self.last_trade_at.get(currency)
        if last and not ignore_cooldown:
            elapsed = (datetime.now(UTC) - last).total_seconds()
            if elapsed < config.cooldown_seconds:
                return f"Cooldown active: {elapsed:.0f}s elapsed."
        return ""


runtime = DDHRuntime()
