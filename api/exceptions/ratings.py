from starlette import status

from api.exceptions.api_exception import APIException


class RatingNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Rating not found"
    description = "The requested rating does not exist."


class CouldNotSendMessageError(APIException):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    detail = "Could not send message"
    description = "The message could not be sent."
