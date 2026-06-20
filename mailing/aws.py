import boto3
from django.conf import settings


def aws_client(service_name, *, endpoint_url=None):
    return boto3.client(
        service_name,
        region_name=settings.AWS_REGION,
        endpoint_url=endpoint_url if endpoint_url is not None else settings.AWS_ENDPOINT_URL or None,
    )


def sqs_client(*, endpoint_url=None):
    return aws_client("sqs", endpoint_url=endpoint_url)


def ses_client(*, endpoint_url=None):
    return aws_client("ses", endpoint_url=endpoint_url)


def s3_client(*, endpoint_url=None):
    return aws_client("s3", endpoint_url=endpoint_url)
