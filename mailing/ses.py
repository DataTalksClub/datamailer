from django.conf import settings


def send_email(*, ses_client, source, to_email, subject, html_body, text_body=""):
    body = {"Html": {"Charset": "UTF-8", "Data": html_body}}
    if text_body:
        body["Text"] = {"Charset": "UTF-8", "Data": text_body}

    params = {
        "Source": source,
        "Destination": {"ToAddresses": [to_email]},
        "Message": {
            "Subject": {"Charset": "UTF-8", "Data": subject},
            "Body": body,
        },
    }
    if settings.AWS_SES_CONFIGURATION_SET:
        params["ConfigurationSetName"] = settings.AWS_SES_CONFIGURATION_SET

    return ses_client.send_email(**params)["MessageId"]
