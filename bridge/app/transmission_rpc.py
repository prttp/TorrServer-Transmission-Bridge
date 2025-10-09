from typing import Any, Dict, Optional

from pydantic import BaseModel


class TransmissionRequest(BaseModel):
    method: str
    arguments: Optional[Dict[str, Any]] = None
    tag: Optional[int] = None


class TransmissionResponse(BaseModel):
    result: str
    arguments: Dict[str, Any]
    tag: Optional[int] = None


