from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.http import HttpClient
from core.models import JobRecord

class SourceAdapter(ABC):
    name: str

    def __init__(self, http: HttpClient):
        self.http = http

    @abstractmethod
    def discover(self) -> list[JobRecord]: ...

    @abstractmethod
    def enrich(self, job: JobRecord) -> JobRecord: ...
