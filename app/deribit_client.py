from __future__ import annotations

import asyncio
from contextlib import suppress
import math
import time
from typing import Any

import websockets

from app.config import DERIBIT_WS_HOSTS, AppConfig, AssetConfig, EnvConfig


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        output = float(value)
        if math.isnan(output) or math.isinf(output):
            return default
        return output
    except (TypeError, ValueError):
        return default


class DeribitClient:
    def __init__(self, config: AppConfig, env: EnvConfig) -> None:
        self.config = config
        self.env = env
        self.ws_url = DERIBIT_WS_HOSTS[config.mode]
        self._access_token = ""
        self._token_expires_at = 0.0
        self._ws = None
        self._receiver_task = None
        self._pending: dict[int, Any] = {}
        self._request_id = 0
        self._send_lock = None
        self._connect_lock = None
        self._restoring_subscriptions = False
        self._subscription_handlers = []
        self._subscription_channels: set[str] = set()
        self._active_subscription_channels: set[str] = set()
        self.last_disconnect_error = ""
        self.last_subscription_error = ""
        self.connection_generation = 0

    @property
    def has_credentials(self) -> bool:
        return bool(self.env.deribit_client_id and self.env.deribit_client_secret)

    @property
    def is_connected(self) -> bool:
        if not self._ws:
            return False
        closed = getattr(self._ws, "closed", False)
        state = str(getattr(self._ws, "state", "")).upper()
        return not closed and "CLOSED" not in state and "CLOSING" not in state

    def has_active_subscriptions(self, channels: list[str]) -> bool:
        return self.is_connected and set(channels).issubset(self._active_subscription_channels)

    async def connect(self) -> None:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        if self.is_connected:
            return

        async with self._connect_lock:
            if self.is_connected:
                return
            last_error: Exception | None = None
            for attempt, delay in enumerate((0.0, 1.0, 2.0, 4.0), start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    self._ws = await websockets.connect(
                        self.ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                    )
                    self._receiver_task = asyncio.create_task(self._receive_loop())
                    self._access_token = ""
                    self._token_expires_at = 0.0
                    self._active_subscription_channels.clear()
                    self.last_disconnect_error = ""
                    self.connection_generation += 1
                    return
                except Exception as exc:
                    last_error = exc
                    self.last_disconnect_error = f"connect attempt {attempt} failed: {exc}"
                    await self.close()
            if last_error:
                raise last_error

    async def close(self) -> None:
        if self._receiver_task:
            self._receiver_task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await self._receiver_task
        if self._ws:
            with suppress(Exception):
                await self._ws.close()
        self._ws = None
        self._receiver_task = None
        self._pending.clear()
        self._access_token = ""
        self._token_expires_at = 0.0
        self._active_subscription_channels.clear()

    async def _receive_loop(self) -> None:
        import json

        try:
            async for raw in self._ws:
                data = json.loads(raw)
                request_id = data.get("id")
                if request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if not future.done():
                        future.set_result(data)
                    continue
                if data.get("method") == "subscription":
                    for handler in list(self._subscription_handlers):
                        try:
                            handler(data.get("params") or {})
                        except Exception as exc:
                            self.last_subscription_error = str(exc)
        except Exception as exc:
            self.last_disconnect_error = str(exc)
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)
        finally:
            self._pending.clear()
            self._ws = None
            self._access_token = ""
            self._token_expires_at = 0.0
            self._active_subscription_channels.clear()

    async def rpc(self, method: str, params: dict[str, Any] | None = None, auth: bool = False) -> Any:
        import json

        async def send_once() -> Any:
            await self.connect()
            if auth:
                await self.get_access_token()
                if method != "private/subscribe":
                    await self.restore_subscriptions()
            self._request_id += 1
            request_id = self._request_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending[request_id] = future
            async with self._send_lock:
                await self._ws.send(json.dumps(payload))
            data = await asyncio.wait_for(future, timeout=20)
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("result")

        try:
            return await send_once()
        except Exception:
            await self.close()
            return await send_once()

    async def subscribe(self, channels: list[str], handler) -> Any:
        if handler not in self._subscription_handlers:
            self._subscription_handlers.append(handler)
        self._subscription_channels.update(channels)
        return await self.restore_subscriptions(channels)

    async def restore_subscriptions(self, channels: list[str] | None = None) -> Any:
        if self._restoring_subscriptions:
            return None
        target = set(channels or self._subscription_channels)
        missing = sorted(target - self._active_subscription_channels)
        if not missing:
            return None
        self._restoring_subscriptions = True
        try:
            result = await self.rpc("private/subscribe", {"channels": missing}, auth=True)
            self._active_subscription_channels.update(missing)
            self.last_subscription_error = ""
            return result
        except Exception as exc:
            self.last_subscription_error = str(exc)
            raise
        finally:
            self._restoring_subscriptions = False

    async def get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 30:
            return self._access_token
        if not self.has_credentials:
            raise RuntimeError("Deribit API credentials are not configured.")
        result = await self.rpc(
            "public/auth",
            {
                "grant_type": "client_credentials",
                "client_id": self.env.deribit_client_id,
                "client_secret": self.env.deribit_client_secret,
            },
        )
        self._access_token = str(result.get("access_token", ""))
        self._token_expires_at = time.time() + safe_float(result.get("expires_in"), 300)
        if not self._access_token:
            raise RuntimeError("Deribit auth did not return an access token.")
        return self._access_token

    async def index_price(self, currency: str) -> float:
        index_name = f"{currency.lower()}_usd"
        result = await self.rpc("public/get_index_price", {"index_name": index_name})
        return safe_float(result.get("index_price"))

    async def order_book(self, instrument_name: str) -> dict[str, Any]:
        result = await self.rpc("public/get_order_book", {"instrument_name": instrument_name, "depth": 1})
        instrument = await self.rpc("public/get_instrument", {"instrument_name": instrument_name})
        best_bid = safe_float((result.get("bids") or [[None]])[0][0])
        best_ask = safe_float((result.get("asks") or [[None]])[0][0])
        return {
            "instrument_name": instrument_name,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "index_price": safe_float(result.get("index_price")),
            "mark_price": safe_float(result.get("mark_price")),
            "contract_size": safe_float(instrument.get("contract_size"), 1.0),
            "min_trade_amount": safe_float(instrument.get("min_trade_amount"), 0.0),
            "tick_size": safe_float(instrument.get("tick_size"), 0.01),
            "settlement_currency": instrument.get("settlement_currency", ""),
            "quote_currency": instrument.get("quote_currency", ""),
        }

    async def positions(self, currency: str) -> list[dict[str, Any]]:
        result = await self.rpc("private/get_positions", {"currency": currency}, auth=True)
        return list(result or [])

    async def account_summary(self, currency: str) -> dict[str, Any]:
        result = await self.rpc("private/get_account_summary", {"currency": currency, "extended": True}, auth=True)
        return dict(result or {})

    async def open_orders_by_currency(self, currency: str) -> list[dict[str, Any]]:
        result = await self.rpc("private/get_open_orders_by_currency", {"currency": currency}, auth=True)
        return list(result or [])

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        result = await self.rpc("private/cancel", {"order_id": order_id}, auth=True)
        return dict(result or {})

    async def cancel_ddh_orders(self, currency: str, order_ids: set[str] | None = None) -> list[dict[str, Any]]:
        prefix = f"ddh-{currency.lower()}-"
        cancelled: list[dict[str, Any]] = []
        for order in await self.open_orders_by_currency(currency):
            label = str(order.get("label") or "")
            order_id = str(order.get("order_id") or "")
            if label.startswith(prefix) and order_id and (order_ids is None or order_id in order_ids):
                cancelled.append(await self.cancel_order(order_id))
        return cancelled

    async def portfolio_delta(self, currency: str, asset_config: AssetConfig) -> dict[str, Any]:
        if not self.has_credentials:
            return {
                "source": "not_configured",
                "net_delta": 0.0,
                "positions": [],
                "message": "API credentials are not configured.",
            }
        positions = await self.positions(currency)
        summary = await self.account_summary(currency)
        enriched = []
        position_delta_sum = 0.0
        for row in positions:
            delta = self.position_delta(row)
            if self.is_zero_position(row, delta):
                continue
            position_delta_sum += delta
            enriched.append(
                {
                    "instrument_name": row.get("instrument_name", ""),
                    "kind": row.get("kind", self.instrument_kind(str(row.get("instrument_name") or ""))),
                    "instrument_type": self.instrument_kind(str(row.get("instrument_name") or "")),
                    "size": safe_float(row.get("size")),
                    "coin_size": self.position_coin_size(row, delta),
                    "delta": delta,
                    "direction": row.get("direction", ""),
                    "average_price": safe_float(row.get("average_price")),
                    "mark_price": safe_float(row.get("mark_price")),
                    "floating_profit_loss": safe_float(row.get("floating_profit_loss")),
                    "total_profit_loss": safe_float(row.get("total_profit_loss")),
                }
            )
        pa_delta = safe_float(summary.get("delta_total"))
        projected_pa_delta = safe_float(summary.get("projected_delta_total"))
        options_delta = safe_float(summary.get("options_delta"))
        return {
            "source": "deribit_account_summary",
            "net_delta": pa_delta,
            "pa_delta": pa_delta,
            "projected_pa_delta": projected_pa_delta,
            "options_delta": options_delta,
            "position_delta_sum": position_delta_sum,
            "delta_total_map": summary.get("delta_total_map") or {},
            "positions": enriched,
            "message": "",
        }

    def instrument_kind(self, instrument_name: str) -> str:
        if instrument_name.endswith("-PERPETUAL"):
            return "perpetual"
        if instrument_name.endswith("-C") or instrument_name.endswith("-P"):
            return "option"
        if instrument_name:
            return "future"
        return ""

    def position_delta(self, row: dict[str, Any]) -> float:
        for key in ("delta", "total_delta"):
            if key in row and row[key] is not None:
                return safe_float(row[key])
        name = str(row.get("instrument_name") or "")
        if name.endswith("-PERPETUAL"):
            size = safe_float(row.get("size"))
            index_price = safe_float(row.get("index_price") or row.get("mark_price"))
            if index_price > 0:
                return size / index_price
        return 0.0

    def is_zero_position(self, row: dict[str, Any], delta: float) -> bool:
        size = safe_float(row.get("size"))
        direction = str(row.get("direction") or "").lower()
        return abs(size) < 1e-12 and abs(delta) < 1e-12 and direction in {"", "zero"}

    def position_coin_size(self, row: dict[str, Any], delta: float) -> float:
        name = str(row.get("instrument_name") or "")
        if not name.endswith("-PERPETUAL"):
            return safe_float(row.get("size"))
        if delta:
            return delta
        size = safe_float(row.get("size"))
        index_price = safe_float(row.get("index_price") or row.get("mark_price"))
        if size <= 0 or index_price <= 0:
            return 0.0
        coin_size = size / index_price
        direction = str(row.get("direction") or "").lower()
        return -coin_size if direction == "sell" else coin_size

    async def place_order(
        self,
        side: str,
        instrument_name: str,
        amount: float,
        order_type: str,
        price: float | None,
        label: str,
    ) -> dict[str, Any]:
        method = "private/buy" if side == "buy" else "private/sell"
        params: dict[str, Any] = {
            "instrument_name": instrument_name,
            "amount": amount,
            "type": order_type,
            "label": label,
        }
        if order_type == "limit":
            if price is None or price <= 0:
                raise RuntimeError("A positive price is required for limit orders.")
            params["price"] = price
            params["post_only"] = True
            params["time_in_force"] = "good_til_cancelled"
        result = await self.rpc(method, params, auth=True)
        return dict(result or {})
