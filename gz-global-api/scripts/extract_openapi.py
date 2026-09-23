#!/usr/bin/env python3
"""Extract the OpenAPI contract of one global-station Java interface.

Input is a Gateway URI (/os/{service}/...) or a full project URL whose
hostname belongs to a confirmed company root domain.  The script always
downloads the latest test-environment Swagger document for the target Java
sub-service, locates the operation, and prints a trimmed standard OpenAPI
JSON subdocument to stdout.  Diagnostics go to stderr only.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

COMPANY_ROOT_DOMAINS = ("guazi.com", "guazi-apps.com", "guazi-cloud.com")
SWAGGER_URL_TMPL = "https://os-{service}.guazi-cloud.com/os/{service}/v3/api-docs"
# Early-service exception confirmed by the Java side: service=product exposes
# Swagger without the /os/product base path, and its OpenAPI paths already
# contain the /os/product prefix (so matching must not strip it).
SERVICE_EXCEPTIONS = {
    "product": {
        "swagger_url": "https://os-product.guazi-cloud.com/v3/api-docs",
        "paths_include_prefix": True,
    },
}
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class InputError(Exception):
    pass


class FetchError(Exception):
    pass


class ExtractionError(Exception):
    pass


def is_company_hostname(hostname):
    """True when hostname is a company root domain or one of its subdomains.

    The check is performed on hostname labels, so evilguazi.com and
    guazi.com.example.com are rejected.
    """
    if not hostname:
        return False
    hostname = hostname.lower()
    return any(hostname == root or hostname.endswith("." + root) for root in COMPANY_ROOT_DOMAINS)


def swagger_url_for(service):
    """Return the test-environment Swagger URL for a Java sub-service."""
    exception = SERVICE_EXCEPTIONS.get(service)
    if exception:
        return exception["swagger_url"]
    return SWAGGER_URL_TMPL.format(service=service)


def openapi_path_for(service, gateway_path, operation_path):
    """Return the OpenAPI path key to match.

    Standard services expose relative operation paths, so /os/{service} is
    stripped.  Exception services (product) document the full prefix already.
    """
    exception = SERVICE_EXCEPTIONS.get(service)
    if exception and exception["paths_include_prefix"]:
        return gateway_path
    return operation_path

def parse_gateway_path(pathname):
    """Validate a /os/{service}/{operationPath} pathname and return (service, operation_path).

    The service is the first non-empty segment after /os; the operation path is
    everything after it, kept verbatim.  Empty segments and trailing slashes are
    not collapsed, so matching stays exact and never fuzzy.
    """
    if not pathname or not pathname.startswith("/os/"):
        raise InputError("pathname must start with /os/{service}/...")
    remainder = pathname[len("/os/"):]
    slash = remainder.find("/")
    if slash <= 0:
        raise InputError(
            "pathname must match /os/{service}/{operationPath} with a non-empty "
            "service and an operation path"
        )
    service = remainder[:slash]
    operation_path = remainder[slash:]
    if operation_path == "/":
        raise InputError("missing operation path after /os/{service}")
    return service, operation_path


def parse_input(raw_uri):
    """Return (gateway_path, service, operation_path) or raise InputError."""
    parts = urlsplit(raw_uri)
    if parts.scheme:
        if parts.scheme not in ("http", "https"):
            raise InputError("URL scheme must be http or https")
        if not is_company_hostname(parts.hostname):
            raise InputError(
                "hostname %r is not a confirmed company domain "
                "(guazi.com / guazi-apps.com / guazi-cloud.com or subdomain)" % parts.hostname
            )
        gateway_path = parts.path
    else:
        if parts.netloc:
            raise InputError(
                "URL without a scheme is not supported; use /os/... or a full http(s) URL"
            )
        if not raw_uri.startswith("/"):
            raise InputError("uri must be a path starting with / or a full http(s) URL")
        gateway_path = parts.path
    service, operation_path = parse_gateway_path(gateway_path)
    return gateway_path, service, operation_path


def fetch_swagger(swagger_url, source_file):
    try:
        subprocess.run(
            ["curl", "-fsSL", "--max-time", "20", swagger_url, "-o", source_file],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or ("curl exit code %d" % exc.returncode)
        raise FetchError("%s (%s)" % (swagger_url, detail))


def load_document(source_file):
    try:
        with open(source_file, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise ExtractionError("cannot read source file: %s" % exc)
    except json.JSONDecodeError as exc:
        raise ExtractionError("swagger response is not valid JSON: %s" % exc)


def normalize_ref_component(ref):
    """Map a local component $ref to (component_type, component_name), or None."""
    if not isinstance(ref, str) or not ref.startswith("#/components/"):
        return None
    keys = [key.replace("~1", "/").replace("~0", "~") for key in ref[len("#/components/"):].split("/")]
    if len(keys) < 2:
        return None
    return keys[0], keys[1]


def collect_refs(node, refs):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                refs.add(value)
            else:
                collect_refs(value, refs)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, refs)


def extract_dependencies(doc, target_path_item):
    """Return the components referenced by the target subtree (transitively)."""
    components = doc.get("components") or {}
    pending = set()
    collect_refs(target_path_item, pending)
    seen = set()
    extracted = {}
    while pending:
        ref = pending.pop()
        parsed = normalize_ref_component(ref)
        if parsed is None:
            continue
        ctype, cname = parsed
        key = (ctype, cname)
        if key in seen:
            continue
        container = components.get(ctype)
        if not isinstance(container, dict) or cname not in container:
            raise ExtractionError("unresolved local $ref: %s" % ref)
        seen.add(key)
        extracted.setdefault(ctype, {})[cname] = container[cname]
        collect_refs(container[cname], pending)
    return extracted


def build_result(doc, operation_path, path_item, method):
    if method:
        target = {}
        # Path-item level parameters apply to every operation and stay relevant.
        if isinstance(path_item.get("parameters"), list):
            target["parameters"] = path_item["parameters"]
        target[method.lower()] = path_item[method.lower()]
    else:
        target = path_item
    result = {"openapi": doc["openapi"], "paths": {operation_path: target}}
    if doc.get("info") is not None:
        result["info"] = doc["info"]
    if doc.get("servers") is not None:
        result["servers"] = doc["servers"]
    dependencies = extract_dependencies(doc, target)
    if dependencies:
        result["components"] = dependencies
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Query the OpenAPI contract of a global-station Java interface."
    )
    parser.add_argument("uri", help="Gateway URI (/os/{service}/...) or full project URL")
    parser.add_argument(
        "--method",
        help="HTTP method to select (case-insensitive); omit to return the whole path item",
    )
    args = parser.parse_args(argv)

    try:
        gateway_path, service, operation_path = parse_input(args.uri)
    except InputError as exc:
        sys.stderr.write("input error: %s\n" % exc)
        return 2

    method = args.method.upper() if args.method else None
    match_path = openapi_path_for(service, gateway_path, operation_path)
    swagger_url = swagger_url_for(service)
    tmpdir = tempfile.mkdtemp(prefix="gz-openapi-")
    source_file = os.path.join(tmpdir, "source.json")

    try:
        fetch_swagger(swagger_url, source_file)
        doc = load_document(source_file)
    except (FetchError, ExtractionError) as exc:
        sys.stderr.write("swagger fetch error: %s\n" % exc)
        sys.stderr.write("Source file: %s\n" % source_file)
        return 1

    if not isinstance(doc, dict) or "openapi" not in doc or not isinstance(doc.get("paths"), dict):
        sys.stderr.write("swagger document does not look like OpenAPI\n")
        sys.stderr.write("Source file: %s\n" % source_file)
        return 1

    paths = doc["paths"]
    if match_path not in paths:
        sys.stderr.write("OpenAPI path not found: %s\n" % match_path)
        sys.stderr.write("Source file: %s\n" % source_file)
        return 1
    path_item = paths[match_path]
    if not isinstance(path_item, dict):
        sys.stderr.write("OpenAPI path item is not an object: %s\n" % match_path)
        sys.stderr.write("Source file: %s\n" % source_file)
        return 1

    available_methods = [m for m in HTTP_METHODS if isinstance(path_item.get(m), dict)]
    if method and method.lower() not in available_methods:
        sys.stderr.write("Method not found: %s for path %s\n" % (method, match_path))
        sys.stderr.write("Available methods: %s\n" % (", ".join(sorted(available_methods)) or "none"))
        sys.stderr.write("Source file: %s\n" % source_file)
        return 1

    try:
        result = build_result(doc, match_path, path_item, method)
    except ExtractionError as exc:
        sys.stderr.write("extraction error: %s\n" % exc)
        sys.stderr.write("Source file: %s\n" % source_file)
        return 1

    sys.stderr.write("Gateway path: %s\n" % gateway_path)
    sys.stderr.write("Service: %s\n" % service)
    sys.stderr.write("Swagger URL: %s\n" % swagger_url)
    sys.stderr.write("Matched path: %s\n" % match_path)
    if method:
        sys.stderr.write("Selected method: %s\n" % method)
    sys.stderr.write("Available methods: %s\n" % (", ".join(sorted(available_methods)) or "none"))
    sys.stderr.write("Source file: %s\n" % source_file)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
