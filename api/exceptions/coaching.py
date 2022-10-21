from starlette import status

from api.exceptions.api_exception import APIException


class CoachingNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Coaching not found"
    description = "The requested coaching does not exist."


class NotEnoughCoinsError(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    detail = "Not enough coins"
    description = "The user does not have enough coins to perform this action."


class CannotBookOwnCoachingError(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "Cannot book own coaching"
    description = "The user cannot book their own coaching."
