import os
from typing import TypedDict

from pydantic import BaseModel

TIMEOUT_MS_DEFAULT = int(os.getenv("SHOPVOX_TIMEOUT_MS", "15000"))


class JobFilters(TypedDict, total=False):
    sales_rep: str


class JobFiltersModel(BaseModel):
    sales_rep: str | None = None


class MfaBodyModel(BaseModel):
    code: str
    trust_device: bool = True
    timeout_ms: int = TIMEOUT_MS_DEFAULT
