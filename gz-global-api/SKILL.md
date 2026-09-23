---
name: gz-global-api
description: Query OpenAPI contracts for global-station Java interfaces. Use when the user asks about an interface's request/response structure, provides a /os/... URI or a full project URL containing /os/..., or needs to confirm interface schemas or fields during development. Fetches the latest test-environment Swagger for the target Java sub-service and returns a standard OpenAPI sub-document containing the target operation and its actual dependencies. The user need not mention Swagger or OpenAPI; do not use solely because /os appears in code.
---

# gz-global-api

Fetch the OpenAPI contract of a global-station Java interface. Given a Gateway URI or full project URL, download the latest test-environment Swagger for the target sub-service, extract the target operation and its transitive dependencies, and print standard OpenAPI JSON to stdout.

Contract query only. Answering, analysis, code generation, or business changes are decided by the agent per the current task.

## When to use

Use when you need to pin down the schema contract of a Java interface in this project, including:

- User asks about an interface's request params or response structure.
- User provides a `/os/...` URI or a full project URL containing `/os/...`.
- You have an interface URI and need request/response types or fields.
- Interface integration or issue analysis requires confirming fields against the Java OpenAPI contract.

No need for the user to mention Swagger or OpenAPI. If the task does not need an interface schema, do not run just because `/os` appears in code.

## Domain rules

### Company domains

```text
Production (public):   *.guazi.com
Production (intranet): *.guazi-apps.com
Pre-prod (intranet):   *-preview.guazi-apps.com
Test (intranet):       *.guazi-cloud.com
```

A full URL hostname must be `guazi.com`, `guazi-apps.com`, or `guazi-cloud.com`, or a subdomain of one. Validate by hostname labels: `evilguazi.com` and `guazi.com.example.com` are invalid. The script validates; do not allow other domains.

### Gateway to Java mapping

```text
Gateway path = /os/{service}/{operationPath}
service      = first non-empty segment after /os
Java op path = remaining path after stripping /os/{service}
Swagger URL  = https://os-{service}.guazi-cloud.com/os/{service}/v3/api-docs
```

Exception (confirmed by the Java side): service `product` is an early service.
Its Swagger endpoint has no `/os/product` base path:

```text
https://os-product.guazi-cloud.com/v3/api-docs
```

For `product` only, matching also differs: its OpenAPI `paths` already contain
the `/os/product` prefix (and `servers` has no base path), so use the full
Gateway path verbatim instead of stripping `/os/product`.

Example: `/os/user/sso/account/login` → service `user`, OpenAPI path `/sso/account/login`, Swagger `https://os-user.guazi-cloud.com/os/user/v3/api-docs`.

No service whitelist; do not guess services. If the constructed Swagger URL returns 404, fail: the interface does not follow this skill's convention. Do not try other domains or services.

## Usage

1. Decide whether the task needs an interface schema.
2. Get the URI/URL from user input or dev context (host-less path is fine).
3. Pass `--method` if the HTTP method is known (case-insensitive).
4. Run:

```bash
python3 scripts/extract_openapi.py "/os/user/sso/account/login" --method POST
python3 scripts/extract_openapi.py "https://en.guazi.com/os/user/sso/account/login"
```

5. Read stdout for the OpenAPI JSON (target path/operation plus actual dependencies only).
6. Read stderr for the mapping and `Source file` path.
7. If no method was given and the path has multiple operations, confirm with the user first.
8. Use the contract per the current task.

## CLI

```text
python3 scripts/extract_openapi.py <uri> [--method <HTTP_METHOD>]
```

- `<uri>`: `/os/{service}/...` URI or full project URL (http/https, company hostname required). Query and fragment are ignored for matching.
- `--method`: optional, case-insensitive. Omit to return all HTTP operations on the path; if the method does not exist, exit non-zero and list available methods.

Only these two arguments. Do not pass cookies, tokens, environment, or domain params; the Swagger URL is always derived from the service.

## stdout / stderr

- stdout: only the trimmed standard OpenAPI JSON. No logs, prompts, temp paths, or wrapper structures.
- stderr: diagnostics, at least `Gateway path`, `Service`, `Swagger URL`, `Matched path`, available/selected methods, `Source file`.

To get a pure JSON file:

```bash
python3 scripts/extract_openapi.py "/os/user/sso/account/login" > result.json
```

## Output semantics

- Keep the Java OpenAPI path verbatim (e.g. `/sso/account/login`); do not rewrite it to the Gateway path. Keep original `servers` so the Java base path is not lost.
- Mapping to remember: `Gateway path = /os/{service} + OpenAPI path`.
- Querying A returns only what is needed to understand A: the target path, a single operation when a method is given (keep path-level `parameters`), all operations when not, plus transitively referenced `components`. No other paths or unrelated definitions.
- No field renaming, translation, or business interpretation.

## Failure and degradation

- Input error (bad URI/URL, non-company domain, invalid path structure, method not found): non-zero exit, stderr explains, and lists available methods when the method is missing. Explain to the user.
- Fetch failure (curl failure, Swagger 404, network unreachable, non-JSON): non-zero exit, stderr shows the URL and error. Explain to the user; without a full document there is no full-document fallback.
- Extraction failure (full doc downloaded but extraction incomplete): non-zero exit, stderr shows the reason and the `source.json` path. You may read the full doc for degraded analysis, but must tell the user "script extraction failed; degraded to full-document analysis" and stay focused on the target interface.
- Never fabricate fields, guess other services or Swagger URLs, interpret a 404 as anything beyond "interface not found", or degrade silently.

Each run writes the full doc to a unique system temp dir (`source.json`) and keeps it for the OS to clean up.

## Dependencies

`python3` and `curl` only. Verify before running; if missing, report the environment blocker. Do not substitute other languages or tools.
