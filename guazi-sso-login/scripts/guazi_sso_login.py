#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler


ENV_ORDER = ("test", "pre", "online")
DEFAULT_BASE_URLS = {
  "test": "http://quality-insurance.guazi-cloud.com",
  "pre": "http://quality-insurance-preview.guazi-apps.com",
  "online": "http://quality-insurance.guazi-apps.com",
}
AUTHORIZE_URL = (
  "https://staff.guazi.com/oauth/authorizeCode"
  "?access_type=offline&scope=guazi.com,guazi-corp.com&response_type=code"
  "&redirect_uri=https://cwiki.guazi.com/plugins/servlet/oauth/callback"
  "&client_id=sso_c5136d622a&include_granted_scopes=true"
)
WIKI_VALIDATE_URL = "https://cwiki.guazi.com/rest/api/group"
CHDSSO_VALIDATE_URLS = {
  "test": "https://sso-server-dev-a.guazi-cloud.com/sso/getUserInfoByToken",
  "pre": "https://sso-server-dev-a.guazi-cloud.com/sso/getUserInfoByToken",
  "online": "https://sso-server.guazi.com/sso/getUserInfoByToken",
}


class NoRedirectHandler(HTTPRedirectHandler):
  def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
    return None


@dataclass(slots=True)
class LoginError(Exception):
  code: str
  message: str
  suggestion: str = ""

  def __str__(self) -> str:
    return f"{self.code}: {self.message}"


def normalize(value: Any) -> str:
  if value is None:
    return ""
  return str(value).strip()


def today_key() -> str:
  return datetime.now().strftime("%Y-%m-%d")


def should_force_refresh(force_refresh: bool, refresh_reason: str | None) -> bool:
  if force_refresh:
    return True
  reason = normalize(refresh_reason).lower()
  return bool(reason and re.search(r"失效|过期|invalid|expired|refresh|重新获取|重新登录", reason, re.I))


def cache_file_path() -> Path:
  cache_file = normalize(os.getenv("GUAZI_SSO_CACHE_FILE")) or "sso-cache.json"
  if os.path.isabs(cache_file):
    return Path(cache_file)
  cache_dir = normalize(os.getenv("GUAZI_SSO_CACHE_DIR")) or "~/.agents/cache/guazi-sso-login"
  return Path(cache_dir).expanduser() / cache_file


def empty_store() -> dict[str, Any]:
  return {"version": 1, "updatedAt": datetime.now().isoformat(), "records": {}}


def read_cache_store() -> dict[str, Any]:
  path = cache_file_path()
  try:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
  except FileNotFoundError:
    data = empty_store()
  except Exception as exc:
    raise LoginError("E_CACHE_ERROR", f"读取缓存文件失败：{exc}", "可删除缓存文件后重试。") from exc

  if not isinstance(data, dict):
    data = empty_store()
  if not isinstance(data.get("records"), dict):
    data["records"] = {}
  data["_cacheFile"] = str(path)
  return data


def write_cache_store(store: dict[str, Any]) -> None:
  path = cache_file_path()
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = {key: value for key, value in store.items() if not key.startswith("_")}
    output["updatedAt"] = datetime.now().isoformat()
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
      path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
      pass
  except Exception as exc:
    raise LoginError("E_CACHE_ERROR", f"写入缓存文件失败：{exc}", "请检查缓存目录权限。") from exc


def cache_key(scope: str, env: str, user_name: str, apply_phone: str) -> str:
  return "::".join([scope or "sso", env or "", user_name or "", apply_phone or ""])


def get_today_cached(scope: str, env: str, user_name: str, apply_phone: str) -> dict[str, Any] | None:
  store = read_cache_store()
  item = store["records"].get(cache_key(scope, env, user_name, apply_phone))
  if not isinstance(item, dict):
    return None
  if item.get("date") != today_key() or not item.get("data"):
    return None
  item = dict(item)
  item["cacheFile"] = store["_cacheFile"]
  return item


def save_today_cached(
  *,
  scope: str,
  env: str,
  user_name: str,
  apply_phone: str,
  data: str,
  response: Any,
  extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
  store = read_cache_store()
  record = {
    "env": env,
    "userName": user_name,
    "applyPhone": apply_phone,
    "date": today_key(),
    "data": data,
    "response": response,
    "updatedAt": datetime.now().isoformat(),
  }
  if scope == "sso":
    record["token"] = data.replace("GUAZISSO=", "", 1).rstrip(";").strip()
  if extra:
    record.update(extra)
  store["records"][cache_key(scope, env, user_name, apply_phone)] = record
  write_cache_store(store)
  record = dict(record)
  record["cacheFile"] = str(cache_file_path())
  return record


def get_cached_credentials() -> dict[str, str] | None:
  creds = read_cache_store().get("credentials")
  if not isinstance(creds, dict):
    return None
  return {
    "userName": normalize(creds.get("userName")),
    "password": normalize(creds.get("password")),
    "applyPhone": normalize(creds.get("applyPhone")),
  }


def save_cached_credentials(user_name: str, password: str, apply_phone: str) -> None:
  store = read_cache_store()
  store["credentials"] = {"userName": user_name, "password": password, "applyPhone": apply_phone}
  write_cache_store(store)


def clear_cached_credentials() -> None:
  store = read_cache_store()
  if "credentials" in store:
    del store["credentials"]
    write_cache_store(store)


def env_var_name(prefix: str, env: str, suffix: str) -> str:
  return f"{prefix}_{env.upper()}_{suffix}"


def get_cached_chdsso_credentials(env: str) -> dict[str, str] | None:
  creds_by_env = read_cache_store().get("chdssoCredentials")
  if not isinstance(creds_by_env, dict):
    return None
  creds = creds_by_env.get(env)
  if not isinstance(creds, dict):
    return None
  return {
    "phone": normalize(creds.get("phone")),
    "code": normalize(creds.get("code")),
  }


def save_cached_chdsso_credentials(env: str, phone: str, code: str) -> None:
  store = read_cache_store()
  creds_by_env = store.get("chdssoCredentials")
  if not isinstance(creds_by_env, dict):
    creds_by_env = {}
  creds_by_env[env] = {"phone": phone, "code": code, "updatedAt": datetime.now().isoformat()}
  store["chdssoCredentials"] = creds_by_env
  write_cache_store(store)


def clear_cached_chdsso_credentials() -> None:
  store = read_cache_store()
  if "chdssoCredentials" in store:
    del store["chdssoCredentials"]
    write_cache_store(store)


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
  user_name = normalize(getattr(args, "user_name", None) or os.getenv("GUAZI_SSO_USER_NAME"))
  password = normalize(getattr(args, "password", None) or os.getenv("GUAZI_SSO_PASSWORD"))
  apply_phone = normalize(getattr(args, "apply_phone", None) or os.getenv("GUAZI_SSO_APPLY_PHONE"))

  cached = get_cached_credentials()
  if cached:
    user_name = user_name or cached["userName"]
    password = password or cached["password"]
    apply_phone = apply_phone or cached["applyPhone"]

  missing = []
  if not password:
    missing.append("password")
  if not user_name:
    missing.append("userName")
  if not apply_phone:
    missing.append("applyPhone")
  if missing:
    raise LoginError(
      "E_MISSING_CREDENTIALS",
      "缺少登录信息：" + ", ".join(missing),
      "请让用户在对话中提供缺失项，然后调用 guazi-sso-login init 写入凭据缓存；也可通过参数或环境变量直接传入。",
    )
  return user_name, password, apply_phone


def init_credentials(args: argparse.Namespace) -> dict[str, Any]:
  user_name = normalize(getattr(args, "user_name", None) or os.getenv("GUAZI_SSO_USER_NAME"))
  password = normalize(getattr(args, "password", None) or os.getenv("GUAZI_SSO_PASSWORD"))
  apply_phone = normalize(getattr(args, "apply_phone", None) or os.getenv("GUAZI_SSO_APPLY_PHONE"))

  missing = []
  if not password:
    missing.append("password")
  if not user_name:
    missing.append("userName")
  if not apply_phone:
    missing.append("applyPhone")
  if missing:
    raise LoginError(
      "E_MISSING_CREDENTIALS",
      "缺少登录信息：" + ", ".join(missing),
      "请让用户在对话中提供缺失项，然后再次调用 init；不要在最终答复中泄露这些值。",
    )

  save_cached_credentials(user_name, password, apply_phone)
  return {
    "status": "ok",
    "message": "credentials cached",
    "cacheFile": str(cache_file_path()),
    "credentials": {
      "userName": user_name,
      "applyPhone": apply_phone,
      "passwordCached": True,
    },
  }


def resolve_chdsso_credentials(args: argparse.Namespace, env: str) -> tuple[str, str]:
  phone = normalize(
    getattr(args, "phone", None)
    or os.getenv(env_var_name("GUAZI_CHDSSO", env, "PHONE"))
    or os.getenv("GUAZI_CHDSSO_PHONE")
  )
  code = normalize(
    getattr(args, "code", None)
    or os.getenv(env_var_name("GUAZI_CHDSSO", env, "CODE"))
    or os.getenv("GUAZI_CHDSSO_CODE")
  )

  cached = get_cached_chdsso_credentials(env)
  if cached:
    phone = phone or cached["phone"]
    code = code or cached["code"]

  missing = []
  if not phone:
    missing.append("phone")
  if not code:
    missing.append("code")
  if missing:
    raise LoginError(
      "E_MISSING_CREDENTIALS",
      f"缺少 {env} 环境 CHDSSO 登录信息：" + ", ".join(missing),
      "请让用户提供缺失项，然后调用 guazi-sso-login init-chdsso 写入对应环境凭据缓存；也可通过参数或环境变量直接传入。",
    )
  return phone, code


def init_chdsso_credentials(args: argparse.Namespace) -> dict[str, Any]:
  env, _ = resolve_env(getattr(args, "env", None))
  phone = normalize(
    getattr(args, "phone", None)
    or os.getenv(env_var_name("GUAZI_CHDSSO", env, "PHONE"))
    or os.getenv("GUAZI_CHDSSO_PHONE")
  )
  code = normalize(
    getattr(args, "code", None)
    or os.getenv(env_var_name("GUAZI_CHDSSO", env, "CODE"))
    or os.getenv("GUAZI_CHDSSO_CODE")
  )

  missing = []
  if not phone:
    missing.append("phone")
  if not code:
    missing.append("code")
  if missing:
    raise LoginError(
      "E_MISSING_CREDENTIALS",
      f"缺少 {env} 环境 CHDSSO 登录信息：" + ", ".join(missing),
      "请让用户提供缺失项，然后再次调用 init-chdsso；不要在最终答复中泄露这些值。",
    )

  save_cached_chdsso_credentials(env, phone, code)
  return {
    "status": "ok",
    "message": "chdsso credentials cached",
    "cacheFile": str(cache_file_path()),
    "credentials": {
      "env": env,
      "phone": phone,
      "codeCached": True,
    },
  }


def http_request(
  url: str,
  *,
  headers: dict[str, str] | None = None,
  redirect: bool = True,
  data: bytes | None = None,
  method: str | None = None,
) -> dict[str, Any]:
  request = Request(url, data=data, headers=headers or {}, method=method or ("POST" if data is not None else "GET"))
  opener = build_opener() if redirect else build_opener(NoRedirectHandler)
  try:
    with opener.open(request, timeout=30) as response:
      body = response.read().decode("utf-8", errors="replace")
      return {
        "ok": 200 <= response.status < 300,
        "status": response.status,
        "statusText": response.reason,
        "headers": {key.lower(): value for key, value in response.headers.items()},
        "setCookies": response.headers.get_all("Set-Cookie") or [],
        "body": body,
        "url": response.geturl(),
      }
  except HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    if not redirect and exc.code in {301, 302, 303, 307, 308}:
      return {
        "ok": False,
        "status": exc.code,
        "statusText": exc.reason,
        "headers": {key.lower(): value for key, value in exc.headers.items()},
        "setCookies": exc.headers.get_all("Set-Cookie") or [],
        "body": body,
        "url": url,
      }
    raise LoginError("E_HTTP_ERROR", f"请求失败({exc.code})：{body or exc.reason}", "请检查网络、账号权限或远端服务状态。") from exc
  except URLError as exc:
    raise LoginError("E_HTTP_ERROR", f"请求失败：{exc.reason}", "请检查网络或远端服务状态。") from exc


def request_json(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, method: str | None = None) -> Any:
  meta = http_request(url, headers=headers, redirect=True, data=data, method=method)
  body = normalize(meta.get("body"))
  try:
    return json.loads(body) if body else None
  except json.JSONDecodeError:
    return body


def extract_token_payload(raw: Any) -> Any:
  if isinstance(raw, dict):
    for key in ("data", "result"):
      if key in raw and raw[key] is not None:
        return raw[key]
    if "token" in raw or "ssoToken" in raw:
      return raw
  return raw


def normalize_prefixed_token(value: Any, prefixes: tuple[str, ...]) -> str:
  text = normalize(value)
  if not text:
    return ""
  for prefix in prefixes:
    if text.lower().startswith(prefix.lower()):
      text = text[len(prefix):]
      break
  return text.rstrip(";").strip()


def normalize_sso_token(value: Any) -> str:
  return normalize_prefixed_token(value, ("GUAZISSO=",))


def normalize_chdsso_token(value: Any) -> str:
  return normalize_prefixed_token(value, ("CHDSSO=", "chdsso="))


def extract_token_string(raw: Any) -> str:
  payload = extract_token_payload(raw)
  if isinstance(payload, dict):
    for key in ("cookie", "cookieString", "cookieValue", "token", "ssoToken", "value"):
      if key in payload:
        token = normalize_sso_token(payload[key])
        if token:
          return token
  return normalize_sso_token(payload)


def extract_chdsso_token_string(raw: Any) -> str:
  payload = extract_token_payload(raw)
  if isinstance(payload, dict):
    for key in ("access_token", "chdsso", "CHDSSO", "cookie", "cookieString", "cookieValue", "token", "ssoToken", "value"):
      if key in payload:
        token = normalize_chdsso_token(payload[key])
        if token:
          return token
  return normalize_chdsso_token(payload)


def resolve_env(env: str | None) -> tuple[str, str]:
  key = normalize(env or "test").lower()
  if key not in ENV_ORDER:
    raise LoginError("E_INVALID_ENV", f"不支持的环境：{env}", "env 只能是 test、pre 或 online。")
  return key, DEFAULT_BASE_URLS[key]


def get_sso_cookie(args: argparse.Namespace, *, env_override: str | None = None, force_refresh_override: bool | None = None) -> dict[str, Any]:
  env, base_url = resolve_env(env_override or getattr(args, "env", None))
  user_name, password, apply_phone = resolve_credentials(args)
  must_refresh = should_force_refresh(
    bool(force_refresh_override if force_refresh_override is not None else getattr(args, "force_refresh", False)),
    getattr(args, "refresh_reason", None),
  )

  request_info = {"env": env, "userName": user_name, "applyPhone": apply_phone}
  if not must_refresh:
    cached = get_today_cached("sso", env, user_name, apply_phone)
    if cached:
      token = normalize_sso_token(cached.get("token") or cached.get("data"))
      return {
        "status": "ok",
        "source": "cache",
        "request": request_info,
        "response": cached.get("response"),
        "data": cached["data"],
        "token": token,
        "cache": {"hit": True, "date": cached["date"], "file": cached["cacheFile"], "forceRefresh": False},
      }

  query = urlencode({"password": password, "applyPhone": apply_phone, "userName": user_name})
  url = f"{base_url.rstrip('/')}/datamanager/testAccount/getTokenForSso?{query}"
  raw = request_json(url, headers={"accept": "*/*", "ssoUserName": user_name})

  login_message = ""
  if isinstance(raw, dict):
    data = raw.get("data")
    if isinstance(data, dict):
      login_message = normalize(data.get("msg"))
    login_message = login_message or normalize(raw.get("msg"))
  if login_message:
    clear_cached_credentials()
    raise LoginError("E_LOGIN_FAILED", f"登录失败：{login_message}", "已清除缓存凭据，请重新提供 password、userName、applyPhone。")

  token = extract_token_string(raw)
  if not token or token == "[object Object]":
    clear_cached_credentials()
    raise LoginError("E_TOKEN_PARSE_FAILED", "获取 token 失败。", "已清除缓存凭据，请重新登录后重试。")

  cookie = f"GUAZISSO={token}"
  cache_record = save_today_cached(
    scope="sso",
    env=env,
    user_name=user_name,
    apply_phone=apply_phone,
    data=cookie,
    response=raw,
  )
  save_cached_credentials(user_name, password, apply_phone)

  return {
    "status": "ok",
    "source": "remote",
    "request": request_info,
    "response": raw,
    "data": cookie,
    "token": token,
    "cache": {"hit": False, "date": cache_record["date"], "file": cache_record["cacheFile"], "forceRefresh": must_refresh},
  }


def validate_chdsso_token(token: str, env: str = "test") -> tuple[bool, dict[str, Any]]:
  env, _ = resolve_env(env)
  try:
    form = urlencode({"takeHeaderFirst": "true"}).encode("utf-8")
    meta = http_request(
      CHDSSO_VALIDATE_URLS[env],
      headers={
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "chdsso": token,
        "content-type": "application/x-www-form-urlencoded",
      },
      redirect=True,
      data=form,
    )
    body = normalize(meta.get("body"))
    try:
      payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
      payload = body
    valid = bool(meta.get("ok"))
    if isinstance(payload, dict):
      success_value = payload.get("success")
      code_value = normalize(payload.get("code"))
      if success_value is False or code_value in {"401", "403"}:
        valid = False
    return valid, {
      "ok": bool(meta.get("ok")),
      "valid": valid,
      "status": meta.get("status"),
      "url": meta.get("url"),
      "response": payload,
    }
  except LoginError as exc:
    return False, {"ok": False, "valid": False, "error": exc.code, "message": exc.message}


def get_chdsso_token(args: argparse.Namespace) -> dict[str, Any]:
  env, base_url = resolve_env(getattr(args, "env", None) or "test")
  phone, code = resolve_chdsso_credentials(args, env)
  must_refresh = should_force_refresh(bool(getattr(args, "force_refresh", False)), getattr(args, "refresh_reason", None))
  should_validate = bool(getattr(args, "validate", False))
  request_info = {"env": env, "phone": phone}

  if not must_refresh:
    cached = get_today_cached("chdsso", env, phone, code)
    if cached:
      token = normalize_chdsso_token(cached.get("token") or cached.get("data"))
      validation = None
      if should_validate:
        valid, validation = validate_chdsso_token(token, env)
        if not valid:
          must_refresh = True
        else:
          return {
            "status": "ok",
            "source": "cache",
            "request": request_info,
            "response": cached.get("response"),
            "data": token,
            "token": token,
            "header": {"chdsso": token},
            "cache": {"hit": True, "date": cached["date"], "file": cached["cacheFile"], "forceRefresh": False},
            "validation": validation,
          }
      else:
        return {
          "status": "ok",
          "source": "cache",
          "request": request_info,
          "response": cached.get("response"),
          "data": token,
          "token": token,
          "header": {"chdsso": token},
          "cache": {"hit": True, "date": cached["date"], "file": cached["cacheFile"], "forceRefresh": False},
          "validation": validation,
        }

  query = urlencode({"phone": phone, "code": code})
  url = f"{base_url.rstrip('/')}/datamanager/testAccount/getTokenForGuazi?{query}"
  raw = request_json(url, headers={"accept": "*/*"})

  login_message = ""
  if isinstance(raw, dict):
    data = raw.get("data")
    if isinstance(data, dict):
      login_message = normalize(data.get("msg"))
    login_message = login_message or normalize(raw.get("msg"))
  if login_message:
    raise LoginError("E_LOGIN_FAILED", f"CHDSSO 登录失败：{login_message}", "请重新提供当前环境的 phone 和 code。")

  token = extract_chdsso_token_string(raw)
  if not token or token == "[object Object]":
    raise LoginError("E_TOKEN_PARSE_FAILED", "获取 CHDSSO token 失败。", "请重新提供当前环境的 phone 和 code 后重试。")

  save_cached_chdsso_credentials(env, phone, code)
  validation = None
  if should_validate:
    valid, validation = validate_chdsso_token(token, env)
    if not valid:
      raise LoginError("E_CHDSSO_VALIDATE_FAILED", "CHDSSO token 校验失败。", "请检查 phone/code 是否正确，或稍后重试测试环境 SSO 服务。")

  cache_record = save_today_cached(
    scope="chdsso",
    env=env,
    user_name=phone,
    apply_phone=code,
    data=token,
    response=raw,
    extra={"token": token, "phone": phone, "codeCached": True},
  )

  return {
    "status": "ok",
    "source": "remote",
    "request": request_info,
    "response": raw,
    "data": token,
    "token": token,
    "header": {"chdsso": token},
    "cache": {"hit": False, "date": cache_record["date"], "file": cache_record["cacheFile"], "forceRefresh": must_refresh},
    "validation": validation,
  }


def normalize_set_cookie(raw: str) -> str:
  return normalize(raw).split(";", 1)[0].strip()


def merge_cookie_header(cookies: list[str]) -> str:
  merged: dict[str, str] = {}
  for raw in cookies:
    item = normalize_set_cookie(raw)
    if not item or "=" not in item:
      continue
    name = item.split("=", 1)[0].strip()
    if name:
      merged[name] = item
  return "; ".join(merged.values())


def validate_wiki_cookie(cookie: str) -> tuple[bool, dict[str, Any]]:
  try:
    meta = http_request(
      WIKI_VALIDATE_URL,
      headers={"Cookie": cookie, "Content-Type": "application/json"},
      redirect=True,
    )
    body = normalize(meta.get("body"))
    try:
      payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
      payload = {}

    results = payload.get("results") if isinstance(payload, dict) else None
    group_count = len(results) if isinstance(results, list) else None
    valid = bool(meta.get("ok")) and isinstance(results, list)
    return valid, {
      "ok": bool(meta.get("ok")),
      "valid": valid,
      "status": meta.get("status"),
      "url": meta.get("url"),
      "groupCount": group_count,
    }
  except LoginError as exc:
    return False, {"ok": False, "error": exc.code, "message": exc.message}


def get_wiki_cookie(args: argparse.Namespace) -> dict[str, Any]:
  must_refresh = should_force_refresh(bool(getattr(args, "force_refresh", False)), getattr(args, "refresh_reason", None))
  should_validate = bool(getattr(args, "validate", False))
  sso = get_sso_cookie(args, env_override="online", force_refresh_override=must_refresh)
  user_name = normalize(sso.get("request", {}).get("userName"))
  apply_phone = normalize(sso.get("request", {}).get("applyPhone"))
  if not user_name or not apply_phone:
    raise LoginError("E_WIKI_LOGIN_FAILED", "wiki 登录缺少 userName 或 applyPhone，无法构建缓存键。", "请重新提供完整登录信息。")

  if not must_refresh:
    cached = get_today_cached("wiki", "online", user_name, apply_phone)
    if cached:
      validation = None
      if should_validate:
        valid, validation = validate_wiki_cookie(cached["data"])
        if not valid:
          must_refresh = True
        else:
          return {
            "status": "ok",
            "source": "cache",
            "env": "online",
            "data": cached["data"],
            "cookie": cached["data"],
            "sso": {"source": sso["source"], "data": sso["data"], "cache": sso["cache"]},
            "cache": {"hit": True, "date": cached["date"], "file": cached["cacheFile"], "forceRefresh": False},
            "validation": validation,
            "response": {"step1": cached.get("step1"), "step2": cached.get("step2")},
          }

      return {
        "status": "ok",
        "source": "cache",
        "env": "online",
        "data": cached["data"],
        "cookie": cached["data"],
        "sso": {"source": sso["source"], "data": sso["data"], "cache": sso["cache"]},
        "cache": {"hit": True, "date": cached["date"], "file": cached["cacheFile"], "forceRefresh": False},
        "validation": validation,
        "response": {"step1": cached.get("step1"), "step2": cached.get("step2")},
      }

  if must_refresh and not bool(getattr(args, "force_refresh", False)):
    sso = get_sso_cookie(args, env_override="online", force_refresh_override=True)

  sso_cookie = normalize(sso.get("data"))
  if "=" not in sso_cookie:
    raise LoginError("E_WIKI_LOGIN_FAILED", "获取 SSO cookie 失败。", "请强制刷新 SSO 后重试。")

  step1 = http_request(
    AUTHORIZE_URL,
    headers={
      "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "cookie": sso_cookie,
    },
    redirect=False,
  )
  location = normalize(step1["headers"].get("location"))
  if not location:
    raise LoginError("E_WIKI_LOGIN_FAILED", "wiki 登录第一步未返回 location 跳转地址。", "请检查 SSO cookie 是否有效。")

  callback_url = urljoin(AUTHORIZE_URL, location)
  step1_cookie_header = merge_cookie_header([sso_cookie, *step1.get("setCookies", [])])
  step2 = http_request(
    callback_url,
    headers={
      "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "cookie": step1_cookie_header,
      "referer": "https://sso-web.guazi.com/",
    },
    redirect=False,
  )

  final_cookie = merge_cookie_header([sso_cookie, *step2.get("setCookies", [])])
  if not final_cookie:
    raise LoginError("E_WIKI_LOGIN_FAILED", "wiki 登录第二步未返回可用 cookie。", "请强制刷新后重试。")

  step1_summary = {"status": step1["status"], "location": location, "setCookies": step1.get("setCookies", [])}
  step2_summary = {"status": step2["status"], "setCookies": step2.get("setCookies", [])}
  cache_record = save_today_cached(
    scope="wiki",
    env="online",
    user_name=user_name,
    apply_phone=apply_phone,
    data=final_cookie,
    response={"location": location, "callbackUrl": callback_url},
    extra={"step1": step1_summary, "step2": step2_summary},
  )

  return {
    "status": "ok",
    "source": "remote",
    "env": "online",
    "data": final_cookie,
    "cookie": final_cookie,
    "sso": {"source": sso["source"], "data": sso["data"], "cache": sso["cache"]},
    "cache": {"hit": False, "date": cache_record["date"], "file": cache_record["cacheFile"], "forceRefresh": must_refresh},
    "validation": {"refreshedAfterInvalidCache": should_validate and must_refresh},
    "response": {"step1": step1_summary, "step2": step2_summary},
  }


def check_status() -> dict[str, Any]:
  path = cache_file_path()
  store = read_cache_store()
  credentials = store.get("credentials")
  chdsso_credentials = store.get("chdssoCredentials")
  records = store.get("records") if isinstance(store.get("records"), dict) else {}
  today_records = [
    key for key, value in records.items()
    if isinstance(value, dict) and value.get("date") == today_key() and value.get("data")
  ]
  return {
    "status": "ok",
    "cacheFile": str(path),
    "cacheExists": path.exists(),
    "hasCredentials": isinstance(credentials, dict)
    and bool(credentials.get("userName"))
    and bool(credentials.get("password"))
    and bool(credentials.get("applyPhone")),
    "chdssoCredentialEnvs": sorted(chdsso_credentials.keys()) if isinstance(chdsso_credentials, dict) else [],
    "todayRecords": today_records,
  }


def emit_result(result: dict[str, Any], *, plain: bool) -> None:
  if plain:
    print(result.get("data") or result.get("cookie") or "")
    return
  print(json.dumps(result, ensure_ascii=False, indent=2))


def emit_error(error: LoginError) -> None:
  print(f"[ERROR] {error.code}", file=sys.stderr)
  print(f"原因：{error.message}", file=sys.stderr)
  if error.suggestion:
    print(f"建议：{error.suggestion}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Get Guazi SSO or Wiki login cookies.")
  subparsers = parser.add_subparsers(dest="command", required=True)

  def add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--user-name", dest="user_name", help="登录用户名")
    subparser.add_argument("--password", help="登录密码")
    subparser.add_argument("--apply-phone", dest="apply_phone", help="手机号")
    subparser.add_argument("--force-refresh", action="store_true", help="忽略当天缓存并强制刷新")
    subparser.add_argument("--refresh-reason", help="刷新原因，如 token 失效/过期")
    subparser.add_argument("--validate", action="store_true", help="返回缓存前先校验登录态；失效时自动刷新")
    subparser.add_argument("--plain", action="store_true", help="只输出 cookie 字符串")

  def add_credential_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--user-name", dest="user_name", help="登录用户名")
    subparser.add_argument("--password", help="登录密码")
    subparser.add_argument("--apply-phone", dest="apply_phone", help="手机号")

  def add_chdsso_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--env", choices=ENV_ORDER, default="test", help="目标环境，默认 test")
    subparser.add_argument("--phone", help="CHDSSO 登录手机号")
    subparser.add_argument("--code", help="CHDSSO 登录验证码")
    subparser.add_argument("--force-refresh", action="store_true", help="忽略当天缓存并强制刷新")
    subparser.add_argument("--refresh-reason", help="刷新原因，如 token 失效/过期")
    subparser.add_argument("--validate", action="store_true", help="返回缓存前先校验 CHDSSO 登录态；失效时自动刷新")
    subparser.add_argument("--plain", action="store_true", help="只输出 chdsso token 字符串")

  sso = subparsers.add_parser("sso", help="获取 GUAZISSO cookie")
  add_common(sso)
  sso.add_argument("--env", choices=ENV_ORDER, default="online", help="目标环境")

  wiki = subparsers.add_parser("wiki", help="获取 cwiki.guazi.com 可用 cookie")
  add_common(wiki)

  chdsso = subparsers.add_parser("chdsso", help="获取 CHDSSO token")
  add_chdsso_args(chdsso)

  init = subparsers.add_parser("init", help="写入本地 SSO 凭据缓存，不发起远端登录")
  add_credential_args(init)

  init_chdsso = subparsers.add_parser("init-chdsso", help="写入本地 CHDSSO 凭据缓存，不发起远端登录")
  init_chdsso.add_argument("--env", choices=ENV_ORDER, default="test", help="目标环境，默认 test")
  init_chdsso.add_argument("--phone", help="CHDSSO 登录手机号")
  init_chdsso.add_argument("--code", help="CHDSSO 登录验证码")

  subparsers.add_parser("check", help="检查缓存和凭据状态")
  subparsers.add_parser("clear-credentials", help="清除本地缓存凭据")
  return parser


def main(argv: list[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  try:
    if args.command == "sso":
      emit_result(get_sso_cookie(args), plain=args.plain)
    elif args.command == "wiki":
      emit_result(get_wiki_cookie(args), plain=args.plain)
    elif args.command == "chdsso":
      emit_result(get_chdsso_token(args), plain=args.plain)
    elif args.command == "init":
      emit_result(init_credentials(args), plain=False)
    elif args.command == "init-chdsso":
      emit_result(init_chdsso_credentials(args), plain=False)
    elif args.command == "check":
      emit_result(check_status(), plain=False)
    elif args.command == "clear-credentials":
      clear_cached_credentials()
      clear_cached_chdsso_credentials()
      emit_result({"status": "ok", "message": "cached credentials cleared", "cacheFile": str(cache_file_path())}, plain=False)
    else:
      parser.error(f"unknown command: {args.command}")
  except LoginError as exc:
    emit_error(exc)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
