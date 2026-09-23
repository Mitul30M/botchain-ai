from pydantic import BaseModel, Field


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class Paginated[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int