/**
 * 登录态：Medusa 认的是 cookie `GUAZISSO`。
 *
 * 取法优先级：
 *   1. 环境变量 MEDUSA_COOKIE（可直接给 "GUAZISSO=xxx"）
 *   2. 本机 guazi-sso-login skill（用已缓存的账号密码换 token，完全不碰浏览器）
 *
 * 注意：token 是个人身份的，不要写进仓库或日志。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { AuthError } from './medusa.mjs';

const SSO_SCRIPT_CANDIDATES = [
  path.join(os.homedir(), '.zcode/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
  path.join(os.homedir(), '.agents/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
  path.join(os.homedir(), '.claude/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
];

function findSsoScript() {
  const override = process.env.ONLINE_RELEASE_SSO_SCRIPT;
  if (override && fs.existsSync(override)) return override;
  const hit = SSO_SCRIPT_CANDIDATES.find((file) => fs.existsSync(file));
  if (!hit) {
    throw new AuthError(
      '没找到 guazi-sso-login skill。安装后重试，或直接设置 MEDUSA_COOKIE="GUAZISSO=xxx"。',
    );
  }
  return hit;
}

function pythonBin() {
  return fs.existsSync('/usr/bin/python3') ? '/usr/bin/python3' : 'python3';
}

/** 用 guazi-sso-login 换取 GUAZISSO cookie（该 skill 自带当天缓存与校验） */
export function readCookieFromSsoSkill() {
  const script = findSsoScript();
  let out;
  try {
    out = execFileSync(pythonBin(), [script, 'sso', '--validate', '--plain'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 120000,
    }).trim();
  } catch (error) {
    throw new AuthError(`换取 GUAZISSO 失败：${(error.stderr || error.message || '').toString().slice(0, 200)}`);
  }
  const match = out.match(/GUAZISSO=[^\s;]+/);
  if (!match) throw new AuthError(`SSO 返回内容里没找到 GUAZISSO：${out.slice(0, 120)}`);
  return match[0];
}

export async function resolveCookie() {
  if (process.env.MEDUSA_COOKIE) return process.env.MEDUSA_COOKIE;
  return readCookieFromSsoSkill();
}
