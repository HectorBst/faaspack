import json
from typing import TYPE_CHECKING

import boto3  # provided by the Lambda runtime, deliberately not declared
import httpx
from pydantic import BaseModel

try:
    import orjson  # optional accelerator
except ImportError:
    orjson = None

if TYPE_CHECKING:
    import mypy_boto3_s3  # noqa: F401


class Event(BaseModel):
    name: str


def handler(event: dict, context: object) -> dict:
    parsed = Event.model_validate(event)
    return {
        "statusCode": 200,
        "body": json.dumps(
            {"hello": parsed.name, "httpx": httpx.__version__, "boto3": boto3.__version__}
        ),
    }
