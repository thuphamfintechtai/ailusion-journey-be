from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field

# Emails are stored lower-cased so lookups are case-insensitive.
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]

Password = Annotated[str, Field(min_length=8, max_length=128)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
