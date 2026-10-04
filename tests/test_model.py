from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import strands
from conftest import content_dict

from paperless_bedrock.model import ModelRequest, StrandsBedrockAnalyzer
from paperless_bedrock.schema import LetterContent


class FakeAgent:
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def __call__(self, prompt: Any, *, structured_output_model: type) -> Any:
        FakeAgent.calls.append(
            {"init": self.kwargs, "prompt": prompt, "model": structured_output_model}
        )
        return SimpleNamespace(
            structured_output=LetterContent.model_validate(content_dict()),
            stop_reason="end_turn",
            metrics=SimpleNamespace(
                accumulated_usage={"inputTokens": 5, "outputTokens": 2, "x": "y"}
            ),
        )


def test_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    analyzer = StrandsBedrockAnalyzer("eu.anthropic.claude-sonnet-5-5", "eu-central-1", 8000)
    result = analyzer.analyze(
        ModelRequest(
            system_prompt="sys", text="<letter/>", images={2: b"jpg2", 1: b"jpg1"}, feedback="fix"
        )
    )
    call = FakeAgent.calls[-1]
    assert call["model"] is LetterContent
    assert call["init"]["tools"] == [] and call["init"]["system_prompt"] == "sys"
    prompt = call["prompt"]
    assert prompt[0] == {"text": "<letter/>"}
    assert prompt[1] == {"text": "Image of page 1:"}
    assert prompt[2]["image"]["source"]["bytes"] == b"jpg1"
    assert prompt[4]["image"]["source"]["bytes"] == b"jpg2"
    assert prompt[-1] == {"text": "fix"}
    assert result.usage == {"inputTokens": 5, "outputTokens": 2}
    assert "temperature" not in analyzer._model.get_config()
