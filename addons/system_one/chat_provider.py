"""Nonstreaming OpenAI-compatible chat translation over the existing bounded transport."""

from __future__ import annotations

import json

from .http_provider import HttpProvider, ParsedChoice, _reject_constant
from .provider import InvalidResponse


class ChatCompletionsProvider(HttpProvider):
    def _payload(self, state, question, name):
        return {
            "model": self.config.request_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": (
                    "Answer the choice question. Return exactly one criterion ID from criteria, "
                    "with no explanation, quotes or formatting. " + question["instructions"]
                )},
                {"role": "user", "content": json.dumps({
                    "state": state, "criteria": question["criteria"],
                }, ensure_ascii=False, allow_nan=False)},
            ],
        }

    def _parse(self, body, name, criteria):
        try:
            data = json.loads(body, parse_constant=_reject_constant)
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            message = choices[0]["message"]
            content = message["content"]
            if (message.get("role") != "assistant" or not isinstance(content, str)
                    or message.get("tool_calls") or message.get("function_call")
                    or choices[0].get("finish_reason") != "stop"):
                raise ValueError
            choice = content.strip()
            if choice not in criteria:
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
            raise InvalidResponse(
                "expected one completed assistant message with an exact criterion ID"
            ) from None
        model = data.get("model")
        return ParsedChoice(
            choice=choice, probabilities={}, confidence=None,
            response_model=model if isinstance(model, str) and model else None,
            usage_in=None, usage_out=None, truncation=None, argmax_mismatch=False,
        )

    def manifest(self):
        result = super().manifest()
        result.pop("upstream_sha", None)  # the historical custom server is not this adapter
        return {**result, "protocol": "openai-chat"}
