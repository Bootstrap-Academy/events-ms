from starlette import status

from api.exceptions.api_exception import APIException


class SlotNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Slot not found"
    description = "The requested slot does not exist."


class SlotBookedException(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Slot already booked"
    description = "The requested slot is already booked."
