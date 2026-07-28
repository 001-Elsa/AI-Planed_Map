from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=20, pattern=r"^[\w\u4e00-\u9fff]+$")
    password: str = Field(min_length=6, max_length=64)
    nickname: str | None = Field(default=None, max_length=20)
    adminInitToken: str | None = None

    @field_validator("nickname")
    @classmethod
    def normalize_nickname(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


class LoginRequest(BaseModel):
    username: str
    password: str
