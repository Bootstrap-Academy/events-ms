from starlette import status

from api.exceptions.api_exception import APIException


class WebinarNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Webinar not found"
    description = "The requested webinar does not exist."


class AlreadyRegisteredError(APIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "Already registered"
    description = "The user is already registered for this webinar."


class AlreadyFullError(APIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "Already full"
    description = "The webinar is already full."


class CannotStartInPastError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Cannot start in the past"
    description = "The webinar cannot start in the past."
