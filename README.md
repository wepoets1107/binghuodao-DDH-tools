# Binghuodao DDH Tools

本项目是一个本地优先的 Deribit Dynamic Delta Hedging 工作台：后台脚本直连 Deribit，前端浏览器工作台用于查看状态、修改参数、启停 DDH、查看挂单和成交记录。

This is a local-first Deribit Dynamic Delta Hedging workbench. A Python backend connects directly to Deribit, while the browser UI manages parameters, runtime control, open orders, fills, and position visibility.

> 风险提示 / Risk notice: This project is for research and operational tooling. It can place real Deribit orders after API keys and live settings are configured. Review the code and test on Deribit testnet before any mainnet use. Nothing here is financial advice.

## Features / 功能

- BTC and ETH support with separate parameter tabs.
- Net Delta (PA delta) based hedge decisions.
- Threshold hedging with separate positive and negative trigger levels.
- Scheduled hedging by asset; scheduled hedges pull Delta back to the target by default.
- Immediate hedge button for the selected asset.
- Perpetual futures as the default hedge instrument.
- Maker limit orders with open-order tracking.
- WebSocket-based Deribit access for account, order, and fill updates.
- Local browser workbench for parameters, runtime status, positions, open orders, and 7-day fill history.
- Separate testnet and mainnet API credential storage in local `.env`.

## Safety Defaults / 安全设计

- Secrets stay local in `.env`; the UI only shows masked API IDs.
- `.env`, `data/`, `vendor/`, logs, and Python cache files are ignored by Git.
- Testnet is the recommended first environment.
- Mainnet use requires the user's own API keys and explicit configuration.
- Existing DDH orders are managed by labels prefixed with `ddh-`.
- Stale maker order refresh is limited and targets specific DDH order IDs.

## Quick Start / 快速启动

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your own Deribit API credentials:

```text
DERIBIT_TESTNET_CLIENT_ID=your_testnet_client_id
DERIBIT_TESTNET_CLIENT_SECRET=your_testnet_client_secret

DERIBIT_MAINNET_CLIENT_ID=your_mainnet_client_id
DERIBIT_MAINNET_CLIENT_SECRET=your_mainnet_client_secret
```

Start in the foreground:

```powershell
.\start.bat
```

Or start in the background:

```powershell
.\start-background.bat
```

Open:

```text
http://127.0.0.1:8888
```

Stop:

```powershell
.\stop.bat
```

## Project Structure / 目录

```text
app/
  config.py          configuration and .env credential helpers
  deribit_client.py  Deribit WebSocket JSON-RPC client
  runtime.py         DDH runtime, schedules, order management
  scheduler.py       hourly/daily/custom schedule gate
  store.py           local config and event storage
  strategy.py        hedge decision and order sizing logic
  static/            browser workbench
run_server.py        FastAPI entry point
start*.bat/ps1       Windows launch helpers
stop.bat             Windows stop helper for port 8888
```

## Notes / 说明

- The app creates local runtime files under `data/`.
- Fill history is retained locally for recent review and is not committed.
- The bundled `vendor/` directory, if present locally, is only a convenience cache and is not part of the open-source release.
- For macOS/Linux users, run `python run_server.py` after installing dependencies, then open `http://127.0.0.1:8888`.

## License

MIT
