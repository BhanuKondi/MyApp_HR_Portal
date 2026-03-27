from flask_mail import Message
from flask import current_app
from extensions import mail # adjust import if needed

def send_email(subject, recipients, body):
    try:
        msg = Message(
            subject=subject,
            sender=current_app.config.get('MAIL_USERNAME'),
            recipients=recipients,
            body=body
        )
        mail.send(msg)
        print("Email sent successfully")
    except Exception as e:
        print("Email failed:", str(e))