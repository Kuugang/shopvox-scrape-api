from typing import List

from pydantic import BaseModel


class SalesOrderItem(BaseModel):
    name: str
    color: str
    size: str
    quantity: str
    price: str
    total: str


class SalesOrder(BaseModel):
    id: str
    store_name: str
    order_name: str
    items: List[SalesOrderItem]
