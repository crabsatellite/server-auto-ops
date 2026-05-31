"""Trigger Alibaba Cloud CodeUp repository mirror sync.

This is for CodeUp repos imported from GitHub or another remote Git host.
It replaces the manual "log in and click sync" path with the DevOps OpenAPI.
"""
import os
import sys

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


def repository_ids_from_args():
    if len(sys.argv) > 1:
        return sys.argv[1:]

    repository_id = os.environ.get("CODEUP_REPOSITORY_ID")
    if repository_id:
        return [repository_id]

    ids = os.environ.get("CODEUP_REPOSITORY_IDS")
    if ids:
        return [part.strip() for part in ids.split(",") if part.strip()]

    print("missing required environment variable: CODEUP_REPOSITORY_ID or CODEUP_REPOSITORY_IDS", file=sys.stderr)
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
    for repository_id in repository_ids_from_args():
        if not trigger_sync(client, repository_id):
            all_ok = False
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
