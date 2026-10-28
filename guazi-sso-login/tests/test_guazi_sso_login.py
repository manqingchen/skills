from pathlib import Path
import os
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import guazi_sso_login as login  # noqa: E402


class GuaziSsoLoginTests(unittest.TestCase):
  def test_extract_token_from_cookie_payload(self):
    raw = {"data": {"cookie": "GUAZISSO=abc123;"}}

    self.assertEqual(login.extract_token_string(raw), "abc123")

  def test_extract_token_from_token_payload(self):
    raw = {"result": {"token": "abc123"}}

    self.assertEqual(login.extract_token_string(raw), "abc123")

  def test_extract_chdsso_from_cookie_payload(self):
    raw = {"data": {"chdsso": "chdsso=chd123;"}}

    self.assertEqual(login.extract_chdsso_token_string(raw), "chd123")

  def test_extract_chdsso_from_access_token_payload(self):
    raw = {"data": {"access_token": "chd-access-token"}}

    self.assertEqual(login.extract_chdsso_token_string(raw), "chd-access-token")

  def test_merge_cookie_header_deduplicates_by_name(self):
    merged = login.merge_cookie_header([
      "GUAZISSO=old; Path=/",
      "JSESSIONID=one; Path=/",
      "GUAZISSO=new; Path=/",
    ])

    self.assertEqual(merged, "GUAZISSO=new; JSESSIONID=one")

  def test_force_refresh_reason_keywords(self):
    self.assertTrue(login.should_force_refresh(False, "token 已过期"))
    self.assertTrue(login.should_force_refresh(False, "invalid cookie"))
    self.assertFalse(login.should_force_refresh(False, ""))

  def test_sso_env_defaults_to_online(self):
    args = login.build_parser().parse_args(["sso"])

    self.assertEqual(args.env, "online")

  def test_sso_env_allows_explicit_override(self):
    args = login.build_parser().parse_args(["sso", "--env", "pre"])

    self.assertEqual(args.env, "pre")

  def test_chdsso_env_defaults_to_test(self):
    args = login.build_parser().parse_args(["chdsso"])

    self.assertEqual(args.env, "test")

  def test_validate_wiki_cookie_accepts_group_response(self):
    old_http_request = login.http_request

    def fake_http_request(url, *, headers=None, redirect=True):  # noqa: ANN001, ANN202
      self.assertEqual(url, "https://cwiki.guazi.com/rest/api/group")
      self.assertEqual(headers["Cookie"], "GUAZISSO=sso; JSESSIONID=wiki")
      return {
        "ok": True,
        "status": 200,
        "url": url,
        "body": '{"results":[{"type":"group","name":"team"}]}',
      }

    login.http_request = fake_http_request  # type: ignore[assignment]
    try:
      valid, validation = login.validate_wiki_cookie("GUAZISSO=sso; JSESSIONID=wiki")
    finally:
      login.http_request = old_http_request  # type: ignore[assignment]

    self.assertTrue(valid)
    self.assertTrue(validation["valid"])
    self.assertEqual(validation["groupCount"], 1)

  def test_validate_wiki_cookie_rejects_non_group_success_response(self):
    old_http_request = login.http_request

    def fake_http_request(url, *, headers=None, redirect=True):  # noqa: ANN001, ANN202
      return {
        "ok": True,
        "status": 200,
        "url": url,
        "body": '{"type":"known","username":"alice"}',
      }

    login.http_request = fake_http_request  # type: ignore[assignment]
    try:
      valid, validation = login.validate_wiki_cookie("GUAZISSO=bad")
    finally:
      login.http_request = old_http_request  # type: ignore[assignment]

    self.assertFalse(valid)
    self.assertFalse(validation["valid"])
    self.assertEqual(validation["groupCount"], None)

  def test_init_credentials_writes_cache(self):
    old_cache_dir = os.environ.get("GUAZI_SSO_CACHE_DIR")
    with tempfile.TemporaryDirectory() as tmp_dir:
      os.environ["GUAZI_SSO_CACHE_DIR"] = tmp_dir
      args = type("Args", (), {
        "user_name": "alice",
        "password": "secret",
        "apply_phone": "13800000000",
      })()

      result = login.init_credentials(args)
      cached = login.get_cached_credentials()

      self.assertEqual(result["status"], "ok")
      self.assertEqual(cached["userName"], "alice")
      self.assertEqual(cached["password"], "secret")
      self.assertEqual(cached["applyPhone"], "13800000000")

    if old_cache_dir is None:
      os.environ.pop("GUAZI_SSO_CACHE_DIR", None)
    else:
      os.environ["GUAZI_SSO_CACHE_DIR"] = old_cache_dir

  def test_init_chdsso_credentials_writes_cache_by_env(self):
    old_cache_dir = os.environ.get("GUAZI_SSO_CACHE_DIR")
    with tempfile.TemporaryDirectory() as tmp_dir:
      os.environ["GUAZI_SSO_CACHE_DIR"] = tmp_dir
      args_test = type("Args", (), {
        "env": "test",
        "phone": "18801235191",
        "code": "999111",
      })()
      args_pre = type("Args", (), {
        "env": "pre",
        "phone": "18801235192",
        "code": "999222",
      })()

      result = login.init_chdsso_credentials(args_test)
      login.init_chdsso_credentials(args_pre)

      self.assertEqual(result["status"], "ok")
      self.assertEqual(login.get_cached_chdsso_credentials("test")["phone"], "18801235191")
      self.assertEqual(login.get_cached_chdsso_credentials("test")["code"], "999111")
      self.assertEqual(login.get_cached_chdsso_credentials("pre")["phone"], "18801235192")
      self.assertEqual(login.get_cached_chdsso_credentials("pre")["code"], "999222")

    if old_cache_dir is None:
      os.environ.pop("GUAZI_SSO_CACHE_DIR", None)
    else:
      os.environ["GUAZI_SSO_CACHE_DIR"] = old_cache_dir

  def test_validate_chdsso_token_posts_header_token(self):
    old_http_request = login.http_request

    def fake_http_request(url, *, headers=None, redirect=True, data=None, method=None):  # noqa: ANN001, ANN202
      self.assertEqual(url, "https://sso-server-dev-a.guazi-cloud.com/sso/getUserInfoByToken")
      self.assertEqual(headers["chdsso"], "chd123")
      self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")
      self.assertEqual(data, b"takeHeaderFirst=true")
      return {
        "ok": True,
        "status": 200,
        "url": url,
        "body": '{"success":true,"data":{"userName":"alice"}}',
      }

    login.http_request = fake_http_request  # type: ignore[assignment]
    try:
      valid, validation = login.validate_chdsso_token("chd123", "test")
    finally:
      login.http_request = old_http_request  # type: ignore[assignment]

    self.assertTrue(valid)
    self.assertTrue(validation["valid"])

  def test_chdsso_returns_valid_cache(self):
    old_cache_dir = os.environ.get("GUAZI_SSO_CACHE_DIR")
    with tempfile.TemporaryDirectory() as tmp_dir:
      os.environ["GUAZI_SSO_CACHE_DIR"] = tmp_dir
      login.save_cached_chdsso_credentials("test", "18801235191", "999111")
      login.save_today_cached(
        scope="chdsso",
        env="test",
        user_name="18801235191",
        apply_phone="999111",
        data="chd123",
        response={},
        extra={"token": "chd123"},
      )
      validate_calls = []
      old_validate = login.validate_chdsso_token

      def fake_validate(token, env="test"):  # noqa: ANN001, ANN202
        validate_calls.append((token, env))
        return True, {"ok": True, "valid": True, "status": 200}

      login.validate_chdsso_token = fake_validate  # type: ignore[assignment]
      args = type("Args", (), {
        "env": "test",
        "phone": None,
        "code": None,
        "force_refresh": False,
        "refresh_reason": None,
        "validate": True,
      })()

      try:
        result = login.get_chdsso_token(args)
      finally:
        login.validate_chdsso_token = old_validate  # type: ignore[assignment]

      self.assertEqual(result["source"], "cache")
      self.assertEqual(result["data"], "chd123")
      self.assertEqual(result["header"], {"chdsso": "chd123"})
      self.assertEqual(validate_calls, [("chd123", "test")])

    if old_cache_dir is None:
      os.environ.pop("GUAZI_SSO_CACHE_DIR", None)
    else:
      os.environ["GUAZI_SSO_CACHE_DIR"] = old_cache_dir

  def test_wiki_validate_returns_valid_cache(self):
    old_cache_dir = os.environ.get("GUAZI_SSO_CACHE_DIR")
    with tempfile.TemporaryDirectory() as tmp_dir:
      os.environ["GUAZI_SSO_CACHE_DIR"] = tmp_dir
      login.save_cached_credentials("alice", "secret", "13800000000")
      login.save_today_cached(
        scope="sso",
        env="online",
        user_name="alice",
        apply_phone="13800000000",
        data="GUAZISSO=sso",
        response={},
      )
      login.save_today_cached(
        scope="wiki",
        env="online",
        user_name="alice",
        apply_phone="13800000000",
        data="GUAZISSO=sso; JSESSIONID=wiki",
        response={},
      )
      validate_calls = []
      old_validate = login.validate_wiki_cookie

      def fake_validate(cookie):  # noqa: ANN001, ANN202
        validate_calls.append(cookie)
        return True, {"ok": True, "valid": True, "status": 200, "groupCount": 1}

      login.validate_wiki_cookie = fake_validate  # type: ignore[assignment]
      args = type("Args", (), {
        "user_name": None,
        "password": None,
        "apply_phone": None,
        "force_refresh": False,
        "refresh_reason": None,
        "validate": True,
      })()

      try:
        result = login.get_wiki_cookie(args)
      finally:
        login.validate_wiki_cookie = old_validate  # type: ignore[assignment]

      self.assertEqual(result["source"], "cache")
      self.assertEqual(result["validation"]["status"], 200)
      self.assertEqual(validate_calls, ["GUAZISSO=sso; JSESSIONID=wiki"])

    if old_cache_dir is None:
      os.environ.pop("GUAZI_SSO_CACHE_DIR", None)
    else:
      os.environ["GUAZI_SSO_CACHE_DIR"] = old_cache_dir


if __name__ == "__main__":
  unittest.main()
