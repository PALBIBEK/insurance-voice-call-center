"""Test doubles shared across suites.

FakeChatClient mimics the AsyncOpenAI chat.completions surface the runtime
uses: `client.chat.completions.create(...)` returning an object with
`.choices[0].message.content` / `.tool_calls`. Responses are scripted in
order, so every agent test is fully deterministic and burns zero tokens.
"""

import json
import types
import typing as t


def text_response(content: str):
    message = types.SimpleNamespace(content=content, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def tool_call_response(*calls: tuple[str, dict]):
    tool_calls = [
        types.SimpleNamespace(
            id=f"call_{i}",
            type="function",
            function=types.SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(calls)
    ]
    message = types.SimpleNamespace(content=None, tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class FakeChatClient:
    """Pops scripted responses in order; records every request it saw."""

    def __init__(self, script: list):
        self._script = list(script)
        self.requests: list[dict] = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    async def _create(self, **kwargs) -> t.Any:
        self.requests.append(kwargs)
        if not self._script:
            raise AssertionError("FakeChatClient script exhausted - test asked for more LLM calls than scripted")
        return self._script.pop(0)
