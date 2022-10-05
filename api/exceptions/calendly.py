from fastapi import status

from .api_exception import APIException


class EventTypeNotConfiguredError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Event type not configured"
    description = "The event type has not been configured yet."


class APITokenMissingError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "API token missing"
    description = "The API token is missing."


class InvalidAPITokenError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Invalid API token"
    description = "The API token is invalid."


class EventTypeNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Event type not found"
    description = "The event type could not be found."


class EventTypeAmbiguousError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Event type ambiguous"
    description = "The event type is ambiguous because the user has created multiple event types."
