import pytest

from api.utils.email import (
    BOOKED_COACHING,
    BOOKED_WEBINAR,
    CANCELLED_COACHING,
    CANCELLED_COACHING_LECTURER,
    CANCELLED_WEBINAR,
    CANCELLED_WEBINAR_LECTURER,
    COMMERCIAL_CANCELLATION,
    Message,
    env,
)


MESSAGES = [
    COMMERCIAL_CANCELLATION,
    BOOKED_WEBINAR,
    BOOKED_COACHING,
    CANCELLED_WEBINAR,
    CANCELLED_WEBINAR_LECTURER,
    CANCELLED_COACHING,
    CANCELLED_COACHING_LECTURER,
]


@pytest.mark.parametrize("scope", ["session", "booking"])
def test__commercial_cancellation__names_the_affected_event_without_claiming_payment(scope: str) -> None:
    content = render(
        COMMERCIAL_CANCELLATION,
        reference="batch-reference",
        notice={
            "title": "Rust <Basics>",
            "start": "2026-09-12T20:00:00+02:00",
            "scope": scope,
            "state": "applied",
            "command_id": "declaration-reference",
            "role": "administrator",
            "affected_orders": 3,
        },
    )
    assert "Rust &lt;Basics&gt;" in content
    assert "12.09.2026 um 18:00 Uhr (UTC)" in content
    assert "wurde abgesagt" in content if scope == "session" else "wurde storniert" in content
    assert content.index("declaration-reference") > content.index("Rust &lt;Basics&gt;")
    assert "https://bootstrap.academy/ansprueche" in content
    assert "https://bootstrap.academy/docs/privacy" in content
    for absent in [
        "zurückerstattet",
        "gutgeschrieben",
        "Verzichtserklärung",
        "Pflichtinformationen",
        "Wir freuen uns auf dich",
        "Lehrperson",
    ]:
        assert absent not in content


def test__commercial_cancellation__unavailable_event_and_missing_context_do_not_invent_an_outcome() -> None:
    content = render(
        COMMERCIAL_CANCELLATION,
        reference="batch-reference",
        notice={
            "title": "Python",
            "start": "morgen",
            "scope": "session",
            "state": "unavailable",
            "command_id": "declaration-reference",
        },
    )
    assert "nicht mehr verfügbar" in content
    assert "wurde abgesagt" not in content
    assert "wurde storniert" not in content
    missing = render(COMMERCIAL_CANCELLATION, reference="batch-reference")
    assert "batch-reference" in missing and "None" not in missing
    assert "wurde abgesagt" not in missing and "wurde storniert" not in missing


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
