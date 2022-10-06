from starlette import status

from api.exceptions.api_exception import APIException


class SkillNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Skill not found"
    description = "The requested skill does not exist."


class SkillRequirementsNotMetError(APIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "Skill requirements not met"
    description = "The requested skill requirements are not met."
