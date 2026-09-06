import pytest

from api.utils.email import (
    BOOKED_COACHING,
    BOOKED_WEBINAR,
    CANCELLED_COACHING,
    CANCELLED_COACHING_LECTURER,
    CANCELLED_WEBINAR,
    CANCELLED_WEBINAR_LECTURER,
    Message,
    env,
)


MESSAGES = [
    BOOKED_WEBINAR,
    BOOKED_COACHING,
    CANCELLED_WEBINAR,
    CANCELLED_WEBINAR_LECTURER,
    CANCELLED_COACHING,
    CANCELLED_COACHING_LECTURER,
]


def render(message: Message, **kwargs: object) -> str:
    return " ".join(env.get_template(message.template).render(**kwargs).split())


@pytest.mark.parametrize("message", MESSAGES, ids=lambda m: m.template)
def test__templates__embed_the_logo(message: Message) -> None:
    """No mail may load the logo from a host, because that would disclose the ip address of the recipient."""

    content = render(message)

    assert 'src="data:image/png;base64,' in content
    # every src must be a data uri; hyperlinks the reader clicks on themselves are fine
    assert 'src="http' not in content


@pytest.mark.parametrize("message", MESSAGES, ids=lambda m: m.template)
def test__templates__end_with_the_signature(message: Message) -> None:
    content = render(message)

    assert "Dein Bootstrap-Academy-Team" in content
    assert "bootstrap academy GmbH" in content


def test__cancelled_webinar__cancelled_by_the_lecturer() -> None:
    content = render(
        CANCELLED_WEBINAR,
        title="Rust",
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        by_lecturer=True,
        coins=1000,
    )

    assert 'Das Webinar "Rust" am' in content
    assert '<a href="https://example.com/time">24.12.2026 um 18:00 (UTC)</a> wurde abgesagt.' in content
    assert "Wir haben dir 1000 MorphCoins zurückerstattet." in content


def test__cancelled_webinar__cancelled_by_the_participant() -> None:
    content = render(
        CANCELLED_WEBINAR,
        title="Rust",
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        by_lecturer=False,
        coins=0,
    )

    assert 'Deine Anmeldung für das Webinar "Rust" am' in content
    assert "wurde storniert." in content
    assert "MorphCoins zurückerstattet" not in content


def test__cancelled_webinar_lecturer__whole_event() -> None:
    content = render(
        CANCELLED_WEBINAR_LECTURER,
        title="Rust",
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        whole_event=True,
        coins=0,
    )

    assert 'Dein Webinar "Rust" am' in content
    assert "Alle Anmeldungen wurden storniert und die MorphCoins zurückerstattet." in content
    assert "als Ausgleich gutgeschrieben" not in content


def test__cancelled_webinar_lecturer__single_registration() -> None:
    content = render(
        CANCELLED_WEBINAR_LECTURER,
        title="Rust",
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        whole_event=False,
        coins=350,
    )

    assert 'Eine Anmeldung für dein Webinar "Rust" am' in content
    assert "Dir wurden 350 MorphCoins als Ausgleich gutgeschrieben." in content


def test__cancelled_coaching__names_the_lecturer() -> None:
    content = render(
        CANCELLED_COACHING,
        instructor="Lecturer Person",
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        by_lecturer=True,
        coins=800,
    )

    assert "Dein Coaching mit Lecturer Person am" in content
    assert "wurde abgesagt." in content
    assert "Wir haben dir 800 MorphCoins zurückerstattet." in content


def test__cancelled_coaching__without_a_known_lecturer() -> None:
    content = render(
        CANCELLED_COACHING,
        instructor=None,
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        by_lecturer=False,
        coins=400,
    )

    assert "Deine Buchung des Coachings am" in content
    assert "None" not in content


def test__cancelled_coaching_lecturer__frees_the_slot() -> None:
    content = render(
        CANCELLED_COACHING_LECTURER,
        date="24.12.2026",
        time="18:00",
        datetime_link="https://example.com/time",
        by_student=True,
        coins=280,
    )

    assert "Die Buchung deines Coaching-Termins am" in content
    assert "Der Termin ist wieder buchbar." in content
    assert "Dir wurden 280 MorphCoins als Ausgleich gutgeschrieben." in content
