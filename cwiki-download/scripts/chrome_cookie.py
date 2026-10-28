import asyncio
import json
import urllib.request
from typing import Any

import websockets


CHROME_DEBUG_PORT_DEFAULT = 9222


def _get_ws_debugger_url(port: int) -> str:
  with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
    targets = json.loads(response.read().decode("utf-8"))

  if not isinstance(targets, list) or not targets:
    raise RuntimeError("Chrome DevTools 未返回可用页面，请确认 Chrome 已开启远程调试")

  for item in targets:
    if not isinstance(item, dict):
      continue
    if item.get("type") != "page":
      continue
    target_url = str(item.get("url") or "")
    ws_url = item.get("webSocketDebuggerUrl")
    if ws_url and ("cwiki" in target_url or "confluence" in target_url):
      return str(ws_url)

  for item in targets:
    if not isinstance(item, dict):
      continue
    if item.get("type") != "page":
      continue
    ws_url = item.get("webSocketDebuggerUrl")
    if ws_url:
      return str(ws_url)

  raise RuntimeError("未找到可用的 Chrome 页面调试连接")


async def _cdp_call(
  websocket: websockets.ClientConnection,
  *,
  method: str,
  params: dict[str, Any] | None = None,
  timeout_s: float = 15.0,
) -> Any:
  _cdp_call._next_id += 1  # type: ignore[attr-defined]
  message_id = _cdp_call._next_id  # type: ignore[attr-defined]

  payload: dict[str, Any] = {"id": message_id, "method": method}
  if params is not None:
    payload["params"] = params

  await websocket.send(json.dumps(payload))

  while True:
    raw = await asyncio.wait_for(websocket.recv(), timeout=timeout_s)
    data = json.loads(raw)
    if data.get("id") != message_id:
      continue
    if "error" in data:
      raise RuntimeError(f"CDP 调用失败：{method}: {data['error']}")
    return data.get("result")


_cdp_call._next_id = 0  # type: ignore[attr-defined]


async def get_cookie_header_for_domain(
  domain_pattern: str,
  *,
  chrome_debug_port: int = CHROME_DEBUG_PORT_DEFAULT,
  confluence_base_url: str,
) -> str:
  ws_url = _get_ws_debugger_url(chrome_debug_port)

  async with websockets.connect(ws_url, ping_interval=None) as websocket:
    await _cdp_call(websocket, method="Network.enable", params={})

    result = None
    try:
      result = await _cdp_call(
        websocket,
        method="Network.getCookies",
        params={"urls": [confluence_base_url]},
      )
    except Exception:
      result = None

    cookies = []
    if result:
      cookies = result.get("cookies") or result.get("value") or []

    if not cookies:
      result = await _cdp_call(websocket, method="Network.getAllCookies", params={})
      cookies = result.get("cookies") or result.get("value") or []

    matched = [
      item for item in cookies
      if isinstance(item, dict)
      and isinstance(item.get("domain"), str)
      and domain_pattern in item["domain"]
    ]
    if not matched:
      raise RuntimeError("未找到 Confluence Cookie，请确认 Chrome 已登录目标站点")

    return "; ".join(
      f"{item['name']}={item['value']}"
      for item in matched
      if item.get("name") is not None
    )
