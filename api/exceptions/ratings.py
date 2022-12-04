from starlette import status

from api.exceptions.api_exception import APIException


class RatingNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Rating not found"
    description = "The requested rating does not exist."
