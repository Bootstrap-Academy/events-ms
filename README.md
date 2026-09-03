[![check](https://github.com/Bootstrap-Academy/events-ms/actions/workflows/check.yml/badge.svg)](https://github.com/Bootstrap-Academy/events-ms/actions/workflows/check.yml)
[![test](https://github.com/Bootstrap-Academy/events-ms/actions/workflows/test.yml/badge.svg)](https://github.com/Bootstrap-Academy/events-ms/actions/workflows/test.yml)
[![build](https://github.com/Bootstrap-Academy/events-ms/actions/workflows/build.yml/badge.svg)](https://github.com/Bootstrap-Academy/events-ms/actions/workflows/build.yml) <!--
https://app.codecov.io/gh/Bootstrap-Academy/events-ms/settings/badge
[![codecov](https://codecov.io/gh/Bootstrap-Academy/events-ms/branch/develop/graph/badge.svg?token=changeme)](https://codecov.io/gh/Bootstrap-Academy/events-ms) -->
![Version](https://img.shields.io/github/v/tag/Bootstrap-Academy/events-ms?include_prereleases&label=version)

# Bootstrap Academy Events Microservice
The official events microservice of [Bootstrap Academy](https://bootstrap.academy/).

If you would like to submit a bug report or feature request, or are looking for general information about the project or the publicly available instances, please refer to the [Bootstrap-Academy repository](https://github.com/Bootstrap-Academy/Bootstrap-Academy).

## Development Setup
1. Install [Python 3.11](https://python.org/), [Poetry](https://python-poetry.org/) and [poethepoet](https://pypi.org/project/poethepoet/).
2. Clone this repository and `cd` into it.
3. Run `poe setup` to install the dependencies.
4. Start a [PostgreSQL](https://www.postgresql.org/) database, for example using [Docker](https://www.docker.com/) or [Podman](https://podman.io/):
    ```bash
    podman run -d --rm \
        --name postgres \
        -p 127.0.0.1:5432:5432 \
        -e POSTGRES_HOST_AUTH_METHOD=trust \
        postgres:alpine
    ```
5. Create the `academy-events` database:
    ```bash
    podman exec postgres \
        psql -U postgres \
        -c 'create database "academy-events"'
    ```
6. Start a [Redis](https://redis.io/) instance, for example using [Docker](https://www.docker.com/) or [Podman](https://podman.io/):
    ```bash
    podman run -d --rm \
        --name redis \
        -p 127.0.0.1:6379:6379 \
        redis:alpine
    ```
7. Run `poe migrate` to run the database migrations.
8. Run `poe api` to start the microservice. You can find the automatically generated swagger documentation on http://localhost:8004/docs.

## Poetry Scripts
```bash
poe setup           # setup dependencies, .env file and pre-commit hook
poe api             # start api locally
poe test            # run unit tests
poe pre-commit      # run pre-commit checks
  poe lint          # run linter
    poe format      # run auto formatter
      poe isort     # sort imports
      poe black     # reformat code
    poe ruff        # check code style
    poe mypy        # check typing
    poe flake8      # check code style
  poe coverage      # run unit tests with coverage
poe alembic         # use alembic to manage database migrations
poe migrate         # run database migrations
poe env             # show settings from .env file
poe jwt             # generate a jwt with the given payload and ttl in seconds
```

## Account Deletion
When an account is deleted, the auth service calls `DELETE /_internal/users/{user_id}` on this microservice.
The endpoint requires an internal token with the `events` audience and answers `204`, also for a user that has no data here, so it can be retried safely.

Everything the user owns is deleted: the webinars they created together with the participants of those webinars, the slots and weekly slots they offer as a lecturer, their coachings, exams, emergency cancellations, lecturer ratings and the token of their ics calendar feed.
Bookings of events that belong to somebody else are treated differently: a booked webinar loses its participant entry and a booked slot is freed instead of being deleted, so the other lecturer keeps their slot.
The cache entries keyed on a user id are dropped as well.

Because the auth service logs and swallows a failing call, a periodic sweep catches the deletions that were lost.
It has no poe task and is installed as the `sweep-deleted-users` entry point:

```bash
poetry run sweep-deleted-users
```

It walks the distinct user ids found in every user id column in batches, asks the auth service for each one and deletes the data of every user it answers `404` for.
The relevant settings are:

| Variable | Default | Description |
| --- | --- | --- |
| `AUTH_URL` | `""` | Base url of the auth service the sweep asks whether a user still exists. |
| `INTERNAL_JWT_TTL` | `10` | Lifetime in seconds of the token used for those requests. |
| `DELETED_USER_SWEEP_BATCH_SIZE` | `500` | Number of user ids loaded from the database per batch. |
| `DELETED_USER_SWEEP_RATE_LIMIT` | `10` | Auth service requests per second. |

In the NixOS module the sweep is a oneshot service with a timer, enabled through `academy.backend.events.sweepDeletedUsers.enable` (`interval`, default `daily`, and `randomizedDelay`, default `5m`).

## Calendar Subscriptions
`GET /calendar` returns an `ics_token`, which the frontend turns into the subscription url `…/events/calendar/{token}/academy.ics`.
That url is a bearer credential: whoever knows it can read the events of that user without logging in.

The token is a random value stored per user in `events_calendar_tokens` and created the first time the calendar is loaded.
`POST /calendar/token/rotate` replaces it, which invalidates every calendar client that still uses the old url.
It is deleted with the rest of the user's data on account deletion, so a deleted account's feed stops working immediately.

## Internal Service Tokens
Tokens for the `/_internal/…` endpoints are signed per audience.
An outgoing token is signed with the secret of the service it is sent to, and an incoming one is verified with the secret of the `events` audience:

| Variable | Default | Description |
| --- | --- | --- |
| `INTERNAL_JWT_SECRET_AUTH` | `""` | Tokens this service sends to the auth service. |
| `INTERNAL_JWT_SECRET_SHOP` | `""` | Tokens this service sends to the shop service. |
| `INTERNAL_JWT_SECRET_SKILLS` | `""` | Tokens this service sends to the skills service. |
| `INTERNAL_JWT_SECRET_EVENTS` | `""` | Tokens this service accepts on `/_internal/…`. |

An empty value falls back to `JWT_SECRET`, so the services keep working until a dedicated secret is deployed to every sender and to the receiver of an audience.
`JWT_SECRET` itself stays in use for the user access tokens the auth service issues.

## PyCharm configuration
Configure the Python interpreter:

- Open PyCharm and go to `Settings` ➔ `Project` ➔ `Python Interpreter`
- Open the menu `Python Interpreter` and click on `Show All...`
- Click on the plus symbol
- Click on `Poetry Environment`
- Select `Existing environment` (setup the environment first by running `poe setup`)
- Confirm with `OK`

Setup the run configuration:

- Click on `Add Configuration...` ➔ `Add new...` ➔ `Python`
- Change target from `Script path` to `Module name` and choose the `api` module
- Change the working directory to root path  ➔ `Edit Configurations`  ➔ `Working directory`
- In the `EnvFile` tab add your `.env` file
- Confirm with `OK`
