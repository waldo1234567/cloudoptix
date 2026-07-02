"""
Cognito PreSignUp trigger.

Auto-confirms new sign-ups (and marks their email verified) so users can sign in
immediately without an email confirmation code. Open self-service signup for the
CloudOptix demo — no invite required.
"""


def lambda_handler(event, context):
    event["response"]["autoConfirmUser"] = True

    attrs = event.get("request", {}).get("userAttributes", {})
    if "email" in attrs:
        event["response"]["autoVerifyEmail"] = True
    if "phone_number" in attrs:
        event["response"]["autoVerifyPhone"] = True

    return event
