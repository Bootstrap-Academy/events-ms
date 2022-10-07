from starlette import status

from api.exceptions.api_exception import APIException


class ExamNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Exam not found"
    description = "The requested exam does not exist."


class ExamAlreadyBookedError(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    detail = "Exam already booked"
    description = "The requested exam is already booked."


class ExamAlreadyPassedError(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    detail = "Exam already passed"
    description = "The requested exam is already passed."
