import os
from typing import List, TypedDict

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


class SizeItem(BaseModel):
    size: str
    quantity: float


class Item(BaseModel):
    name: str
    part: str
    color: str
    store: str
    sizes: List[SizeItem]
    total_quantity: float


class SalesOrder(BaseModel):
    url: str
    id: int
    items: List[Item]
    total: float
    customer: str
