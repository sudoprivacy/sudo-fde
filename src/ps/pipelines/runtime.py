"""流水线共用的运行时：设置、LLM、会话、用量累计、版本记录。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ps import PIPELINE_VERSION
from ps.config import Settings
from ps.core import prompts
from ps.core.auth import Identity
from ps.core.errors import ServiceError
from ps.core.llm import LLM, LLMBadOutput, LLMCall, LLMUnavailable, Usage
from ps.db import CallLog


@dataclass
class Run:
    settings: Settings
    llm: LLM
    session: Session
    ident: Identity
    capability: str
    call_id: str
    workspace_id: str
    input_hash: str
    usage: Usage = field(default_factory=Usage)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)    # v1.1：FDE 已应用的更正
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def llm_json(self, task: str, prompt_name: str, data: dict[str, Any], render: dict[str, Any],
                 schema: dict[str, Any], alias: str = "reasoner", temperature: float | None = None) -> dict[str, Any]:
        p = prompts.load(prompt_name)
        with self._lock:
            self.prompt_versions[task] = p.version
        system, user = p.render(**render)
        if self.corrections:
            # v1.1：已应用的更正对所有生成类能力生效，优先级高于更早的证据（服务规格 §5.11）
            user += "\n\n<corrections>\n" + "\n".join(f"- [{c['evidence_id']}] {c['understood']}" for c in self.corrections) + \
                "\n</corrections>\n【更正规则】<corrections> 是 FDE 已确认的更正，优先级高于更早的证据；与之冲突的旧证据不得作为依据，输出必须与这些更正一致。"
            data = {**data, "corrections": self.corrections}
        call = LLMCall(task=task, alias=alias, system=system, user=user, schema=schema,
                       temperature=self.settings.t("llm.temperature") if temperature is None else temperature,
                       data=data)
        try:
            out, u = self.llm.complete_json(call)
        except LLMUnavailable as e:
            raise ServiceError("MODEL_UNAVAILABLE", "模型不可用", reason=str(e)[:200]) from e
        except LLMBadOutput as e:
            raise ServiceError("MODEL_UNAVAILABLE", "模型输出无法解析", reason=str(e)[:200]) from e
        with self._lock:
            self.usage.add(u)
        return out

    def versions(self) -> dict[str, Any]:
        return {"pipeline": PIPELINE_VERSION, "prompts": dict(self.prompt_versions),
                "model_alias": getattr(self.llm, "name", None), "calibration": None, "snapshot": None}

    def log(self, status: str, dropped: dict[str, int] | None = None) -> CallLog:
        return CallLog(tenant_id=self.ident.tenant_id, workspace_id=self.workspace_id, call_id=self.call_id,
                       capability=self.capability, pipeline_version=PIPELINE_VERSION,
                       prompt_versions=dict(self.prompt_versions), model_alias=getattr(self.llm, "name", None),
                       input_hash=self.input_hash, llm_calls=self.usage.calls, tokens_in=self.usage.tokens_in,
                       tokens_out=self.usage.tokens_out, latency_ms=self.usage.latency_ms,
                       dropped=dropped or {}, status=status)
