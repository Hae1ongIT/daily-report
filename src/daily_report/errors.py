"""규칙 위반. field는 모달에서 오류를 표시할 입력 칸의 이름이다."""


class RuleError(ValueError):
    def __init__(self, message: str, field: str = "date"):
        super().__init__(message)
        self.message = message
        self.field = field
