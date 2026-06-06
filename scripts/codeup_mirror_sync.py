"""Trigger Alibaba Cloud CodeUp repository mirror sync.

This is for CodeUp repos imported from GitHub or another remote Git host.
It replaces the manual "log in and click sync" path with the DevOps OpenAPI.
"""
import os
import sys
from urllib.parse import urlparse

from alibabacloud_devops20210625.client import Client
from alibabacloud_devops20210625 import models
from alibabacloud_tea_openapi.models import Config


REGION = os.environ.get("CODEUP_REGION", "cn-hangzhou")
ENDPOINT = os.environ.get("CODEUP_ENDPOINT", f"devops.{REGION}.aliyuncs.com")


def getenv_required(name):
    value = os.environ.get(name)
    if not value:
        print(f"missing required environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def optional_env(name):
    value = os.environ.get(name)
    return value if value else None


def value_to_map(value):
    if value is None:
        return None
    if hasattr(value, "to_map"):
        return value.to_map()
    if isinstance(value, list):
        return [value_to_map(item) for item in value]
    if isinstance(value, dict):
        return {key: value_to_map(item) for key, item in value.items()}
    return value


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def first_present(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_repository_path(value):
    if not value:
        return ""

    text = value.strip().replace("\\", "/")
    if text.startswith("git@codeup.aliyun.com:"):
        text = text.split(":", 1)[1]
    elif "://" in text:
        parsed = urlparse(text)
        text = parsed.path

    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if text.endswith(".git"):
        text = text[:-4]

    organization_id = os.environ.get("CODEUP_ORGANIZATION_ID", "").strip()
    if organization_id and text.startswith(f"{organization_id}/"):
        text = text[len(organization_id) + 1 :]

    return text.lower()


def repository_name_from_path(path):
    normalized = normalize_repository_path(path)
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def candidate_repository_id(candidate):
    for mapping in iter_dicts(candidate):
        value = first_present(
            mapping,
            [
                "id",
                "Id",
                "ID",
                "repositoryId",
                "repository_id",
                "RepositoryId",
                "repositoryID",
            ],
        )
        if value not in (None, ""):
            return str(value)
    return None


def candidate_matches(candidate, target_path):
    normalized_target = normalize_repository_path(target_path)
    for value in iter_strings(candidate):
        normalized_value = normalize_repository_path(value)
        if normalized_value == normalized_target:
            return True
    return False


def candidate_label(candidate):
    for mapping in iter_dicts(candidate):
        value = first_present(
            mapping,
            [
                "pathWithNamespace",
                "path_with_namespace",
                "nameWithNamespace",
                "name_with_namespace",
                "webUrl",
                "web_url",
                "sshUrlToRepo",
                "ssh_url_to_repo",
                "httpUrlToRepo",
                "http_url_to_repo",
                "path",
                "name",
            ],
        )
        if value:
            return str(value)
    return "<unknown>"


def repositories_from_response(response):
    payload = value_to_map(response)
    for mapping in iter_dicts(payload):
        result = mapping.get("result")
        if isinstance(result, list):
            return result
        for key in ("repositories", "repositoryList", "repository_list", "data", "list"):
            value = mapping.get(key)
            if isinstance(value, list):
                return value
    return []


def list_repository_candidates(client, search):
    request = models.ListRepositoriesRequest(
        organization_id=getenv_required("CODEUP_ORGANIZATION_ID"),
        search=search,
        page=1,
        per_page=100,
    )
    candidates = repositories_from_response(client.list_repositories(request))
    if candidates:
        return candidates

    search_request = models.ListSearchRepositoryRequest(
        organization_id=getenv_required("CODEUP_ORGANIZATION_ID"),
        keyword=search,
        page=1,
        page_size=100,
    )
    return repositories_from_response(client.list_search_repository(search_request))


def resolve_repository_id(client, path):
    normalized_path = normalize_repository_path(path)
    if not normalized_path:
        print("missing CodeUp repository path", file=sys.stderr)
        sys.exit(2)

    search = repository_name_from_path(normalized_path)
    candidates = list_repository_candidates(client, search)
    matches = [candidate for candidate in candidates if candidate_matches(candidate, normalized_path)]

    if not matches:
        print(f"no CodeUp repository matched path: {normalized_path}", file=sys.stderr)
        if candidates:
            print("candidates:", file=sys.stderr)
            for candidate in candidates:
                print(f"- {candidate_repository_id(candidate) or '?'} {candidate_label(candidate)}", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"multiple CodeUp repositories matched path: {normalized_path}", file=sys.stderr)
        for candidate in matches:
            print(f"- {candidate_repository_id(candidate) or '?'} {candidate_label(candidate)}", file=sys.stderr)
        sys.exit(1)

    repository_id = candidate_repository_id(matches[0])
    if not repository_id:
        print(f"matched repository has no id: {candidate_label(matches[0])}", file=sys.stderr)
        sys.exit(1)

    print(f"resolved:{normalized_path}:repositoryId={repository_id}")
    return repository_id


def repository_ids_from_args(client):
    if len(sys.argv) > 1:
        return sys.argv[1:]

    repository_id = os.environ.get("CODEUP_REPOSITORY_ID")
    if repository_id:
        return [repository_id]

    repository_path = os.environ.get("CODEUP_REPOSITORY_PATH")
    if repository_path:
        return [resolve_repository_id(client, repository_path)]

    ids = os.environ.get("CODEUP_REPOSITORY_IDS")
    if ids:
        return [part.strip() for part in ids.split(",") if part.strip()]

    default_repository_id = os.environ.get("CODEUP_DEFAULT_REPOSITORY_ID")
    if default_repository_id:
        return [default_repository_id]

    print(
        "missing required environment variable: CODEUP_REPOSITORY_ID, "
        "CODEUP_REPOSITORY_PATH, CODEUP_REPOSITORY_IDS, or CODEUP_DEFAULT_REPOSITORY_ID",
        file=sys.stderr,
    )
    sys.exit(2)


def create_client():
    return Client(
        Config(
            access_key_id=getenv_required("ALIBABA_ACCESS_KEY_ID"),
            access_key_secret=getenv_required("ALIBABA_ACCESS_KEY_SECRET"),
            region_id=REGION,
            endpoint=ENDPOINT,
        )
    )


def trigger_sync(client, repository_id):
    request = models.TriggerRepositoryMirrorSyncRequest(
        organization_id=getenv_required("CODEUP_ORGANIZATION_ID"),
        access_token=optional_env("CODEUP_ACCESS_TOKEN"),
        account=optional_env("CODEUP_REMOTE_ACCOUNT"),
        token=optional_env("CODEUP_REMOTE_TOKEN"),
    )
    response = client.trigger_repository_mirror_sync(repository_id, request)
    body = response.body
    result = body.result.result if body and body.result else None
    success = bool(body and body.success and result)
    request_id = body.request_id if body else ""

    if success:
        print(f"OK:{repository_id}:requestId={request_id}")
        return True

    error_code = body.error_code if body else ""
    error_message = body.error_message if body else ""
    print(
        f"FAIL:{repository_id}:requestId={request_id}:"
        f"errorCode={error_code}:errorMessage={error_message}",
        file=sys.stderr,
    )
    return False


def main():
    client = create_client()
    all_ok = True
    for repository_id in repository_ids_from_args(client):
        if not trigger_sync(client, repository_id):
            all_ok = False
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
