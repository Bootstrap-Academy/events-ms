import random
import string
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import aiosmtplib
import email_validator
from jinja2 import Environment, FileSystemLoader

from .async_thread import run_in_thread
from ..logger import get_logger
from ..services.auth import get_email
from ..settings import settings


logger = get_logger(__name__)


TEMPLATES = Path(__file__).parent / "../../templates"

env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)

# The logo is embedded into the mail instead of being loaded from a static
# host: rendering a message must not cause any request and therefore must not
# be able to disclose the recipient's IP address.
env.globals["logo_base64"] = b64encode((TEMPLATES / "logo-text.png").read_bytes()).decode()


def readable_event_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        return value
    parsed = parsed.astimezone(timezone.utc)
    clock = "%H:%M:%S" if parsed.second or parsed.microsecond else "%H:%M"
    return parsed.strftime(f"%d.%m.%Y um {clock} Uhr (UTC)")


env.filters["event_time"] = readable_event_time


@dataclass
class Message:
    title: str
    template: str

    async def send(self, recipient: str, *, reply_to: str | None = None, **kwargs: Any) -> None:
        content = env.get_template(self.template).render(**kwargs)
        await send_email(recipient, self.title, content, reply_to=reply_to)


async def notify(message: Message, user_id: str, **kwargs: Any) -> None:
    """
    Send a message to a user, if the auth service knows an address for them.

    The caller supplies only the booking observations it can support. Neither
    sending a notice nor recording a claim proves that money has moved. Rendering
    or SMTP failure is logged without undoing the caller's durable booking work.
    """

    try:
        if email := await get_email(user_id):
            await message.send(email, **kwargs)
    except Exception:
        logger.exception("could not send %s to user %s", message.template, user_id)


BOOKED_WEBINAR = Message(title="Anmeldungsbestätigung - Bootstrap Academy", template="booked_webinar.html")
BOOKED_COACHING = Message(title="Anmeldungsbestätigung - Bootstrap Academy", template="booked_coaching.html")

# Cancellation notices. The participant of an event and its lecturer are told about different things, so each of them
# has its own template.
CANCELLED_WEBINAR = Message(title="Stornierung deiner Buchung - Bootstrap Academy", template="cancelled_webinar.html")
CANCELLED_WEBINAR_LECTURER = Message(
    title="Stornierung eines Termins - Bootstrap Academy", template="cancelled_webinar_lecturer.html"
)
CANCELLED_COACHING = Message(title="Stornierung deiner Buchung - Bootstrap Academy", template="cancelled_coaching.html")
CANCELLED_COACHING_LECTURER = Message(
    title="Stornierung eines Termins - Bootstrap Academy", template="cancelled_coaching_lecturer.html"
)
COMMERCIAL_CANCELLATION = Message(
    title="Information zu einer Stornierung - Bootstrap Academy", template="commercial_cancellation.html"
)


@run_in_thread
def check_email_deliverability(email: str) -> bool:
    try:
        email_validator.validate_email(email)
    except email_validator.EmailNotValidError:
        return False
    return True


async def send_email(recipient: str, title: str, body: str, *, reply_to: str | None = None) -> None:
    if not await check_email_deliverability(recipient):
        raise ValueError("Invalid email address")

    logger.debug(f"Sending email to {recipient} ({title})")

    message = MIMEMultipart()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = title
    if reply_to:
        message["Reply-To"] = reply_to
    message.attach(MIMEText(body, "html"))

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        use_tls=settings.smtp_tls,
        start_tls=settings.smtp_starttls,
        timeout=20,
    )


def generate_verification_code() -> str:
    return "-".join(
        "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4)) for _ in range(4)  # noqa: S311
    )


async def notify_commercial(user_id: str, reference: str, *, notice: dict[str, Any] | None = None) -> bool:
    """True means SMTP accepted this notice, never that the claim was satisfied.

    Live contact is read without the ordinary identity cache. An erased or
    unverified contact needs the backend's retained-contact process; it remains
    pending here instead of reusing a historical cached address.
    """
    from api.services.internal import InternalService

    try:
        async with InternalService.AUTH.client as client:
            response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return False
        account = response.json()
        if account.get("email_verified") is not True or not isinstance(account.get("email"), str):
            return False
        await COMMERCIAL_CANCELLATION.send(account["email"], reference=reference, notice=notice)
        return True
    except Exception:
        logger.exception("Commercial notice remains pending for %s", user_id)
        return False
