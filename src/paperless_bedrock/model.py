"""Model call. The analyst has no tools: one structured-output request per attempt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

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


class Analyzer(Protocol):
    model_id: str

    def analyze(self, request: ModelRequest) -> ModelResult: ...


class StrandsBedrockAnalyzer:
    """Claude (or any Bedrock model) through the Strands Agents SDK, without tools."""

    def __init__(self, model_id: str, region: str, max_tokens: int) -> None:
        from strands.models.bedrock import BedrockModel

        self.model_id = model_id
        # No temperature: Claude Sonnet/Opus 5.5 reject non-default sampling parameters.
        self._model = BedrockModel(model_id=model_id, region_name=region, max_tokens=max_tokens)

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
