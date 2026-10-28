/** Production Cloud defaults baked into gwiki. Override via env or ~/.config/gwiki/config.yaml. */
export const GWIKI_DEFAULT_CLOUD_URL = "http://10.16.192.73/llm-wiki-cloud/";

/** @deprecated Legacy shared token disabled for auth testing. Per-user tokens are required. */
// export const GWIKI_DEFAULT_SERVICE_TOKEN = "cloud-local-token-prod-2026-06-10";
export const GWIKI_DEFAULT_SERVICE_TOKEN = "";

export const GWIKI_REGISTER_PATH = "/api/tokens/register";
export const GWIKI_SSO_EXCHANGE_PATH = "/api/tokens/sso-exchange";
export const GWIKI_AUTH_PATH = "/auth";
