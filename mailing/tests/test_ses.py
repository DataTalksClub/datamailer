import boto3
from botocore.stub import Stubber
from django.test import override_settings

from mailing.ses import send_email


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_send_email_uses_expected_ses_payload():
    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-123"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"},
                        "Text": {"Charset": "UTF-8", "Data": "Hello"},
                    },
                },
            },
        )

        message_id = send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
            text_body="Hello",
        )

    assert message_id == "message-123"
