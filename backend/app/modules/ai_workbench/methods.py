"""接收插件方法清单并归档，不在业务后端维护或选择 Skill 正文。"""

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

METHOD_ARCHIVE_BYTES = 96000


class MethodResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal[
        "ai-delivery-review", "builds", "rules", "materials", "business", "scale"
    ]
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=40)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    body: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def check_digest(self):
        if sha256(self.body.encode()).hexdigest() != self.sha256:
            raise ValueError("method digest mismatch")
        return self


class MethodBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    main: MethodResource
    references: list[MethodResource] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def check_catalog(self):
        if self.main.key != "ai-delivery-review" or {
            r.key for r in self.references
        } != {"builds", "rules", "materials", "business", "scale"}:
            raise ValueError("invalid review method catalog")
        if sum(len(r.body.encode()) for r in [self.main, *self.references]) > 64000:
            raise ValueError("method catalog exceeds budget")
        if (
            len(
                json.dumps(
                    self.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            )
            > METHOD_ARCHIVE_BYTES
        ):
            raise ValueError("encoded method archive exceeds budget")
        return self
