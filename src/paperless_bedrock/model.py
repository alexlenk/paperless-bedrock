"""Model call. The analyst has no tools: one structured-output request per attempt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from paperless_bedrock.schema import LetterContent


@dataclass
class ModelRequest:
    system_prompt: str
    text: str
    images: dict[int, bytes]  # page number -> JPEG
    feedback: str | None = None  # issues from a previous attempt


@dataclass
class ModelResult:
    content: LetterContent
    usage: dict[str, int] = field(default_factory=dict)


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: int | None = Field(description="Id of the matching existing entry, or null.")
    confident: bool
    reason: str


@dataclass
class JudgeRequest:
    kind: str  # correspondent | document_type
    new: dict[str, object]
    candidates: list[dict[str, object]]


class Judge(Protocol):
    def judge(self, request: JudgeRequest) -> JudgeResult: ...


class Analyzer(Protocol):
    model_id: str

    def analyze(self, request: ModelRequest) -> ModelResult: ...


class StrandsBedrockAnalyzer:
    """Claude (or any Bedrock model) through the Strands Agents SDK, without tools."""

    def __init__(self, model_id: str, region: str, max_tokens: int) -> None:
        from strands.models.bedrock import BedrockModel

        self.model_id = model_id
        from strands.models.model import CacheConfig

        # No temperature: Claude Sonnet/Opus 5.5 reject non-default sampling parameters.
        # Prompt caching: the system prompt (rules, context, correspondent list) is the same for
        # every letter.
        self._model = BedrockModel(
            model_id=model_id,
            region_name=region,
            max_tokens=max_tokens,
            cache_config=CacheConfig(strategy="auto"),
        )

    def analyze(self, request: ModelRequest) -> ModelResult:
        from strands import Agent

        content: list[Any] = [{"text": request.text}]
        for number, image in sorted(request.images.items()):
            content.append({"text": f"Image of page {number}:"})
            content.append({"image": {"format": "jpeg", "source": {"bytes": image}}})
        if request.feedback:
            content.append({"text": request.feedback})

        # A fresh agent per call: no conversation state leaks between letters.
        agent = Agent(
            model=self._model,
            system_prompt=request.system_prompt,
            tools=[],
            callback_handler=None,
        )
        result = agent(content, structured_output_model=LetterContent)
        if not isinstance(result.structured_output, LetterContent):
            raise ValueError(f"model returned no structured output (stop: {result.stop_reason})")
        usage = {k: v for k, v in result.metrics.accumulated_usage.items() if isinstance(v, int)}
        return ModelResult(content=result.structured_output, usage=usage)


class StrandsBedrockJudge:
    """Same-entity check: small structured call, no letter text, no tools."""

    def __init__(self, model_id: str, region: str) -> None:
        from strands.models.bedrock import BedrockModel

        self._model = BedrockModel(model_id=model_id, region_name=region, max_tokens=1000)

    def judge(self, request: JudgeRequest) -> JudgeResult:
        import json

        from strands import Agent

        from paperless_bedrock.prompt import JUDGE_PROMPT

        agent = Agent(
            model=self._model, system_prompt=JUDGE_PROMPT, tools=[], callback_handler=None
        )
        text = (
            f"Kind: {request.kind}\n"
            f"New name and what we know about it:\n{json.dumps(request.new, ensure_ascii=False)}\n"
            "Existing entries:\n"
            + "\n".join(json.dumps(c, ensure_ascii=False) for c in request.candidates)
        )
        result = agent(text, structured_output_model=JudgeResult)
        if not isinstance(result.structured_output, JudgeResult):
            raise ValueError("judge returned no structured output")
        return result.structured_output
