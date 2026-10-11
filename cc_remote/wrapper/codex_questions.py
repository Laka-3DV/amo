"""Small, explicit user-input tool for Remote-created Codex threads.

Codex's built-in request_user_input may be unavailable outside Plan mode.
Dynamic tools are local app-server RPCs, not relay-visible plug-in payloads.
"""
from __future__ import annotations

TOOL_NAME = "amo_ask_user"
USER_INPUT_TOOL = {
    "type": "function",
    "name": TOOL_NAME,
    "description": "Ask the user for missing information or a decision. Opens an interactive question in Amo and waits for their answer. Available in ordinary execution mode. Use when clarification is needed; never assume an unanswered question was approved.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array", "minItems": 1, "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "maxLength": 256},
                        "header": {"type": "string", "maxLength": 512},
                        "question": {"type": "string", "maxLength": 65536},
                        "options": {
                            "type": "array", "maxItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "maxLength": 512},
                                    "description": {"type": "string", "maxLength": 2048},
                                },
                                "required": ["label"], "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["id", "header", "question"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["questions"], "additionalProperties": False,
    },
}


def validated_questions(value):
    """Reject malformed requests before creating any pending question."""
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError("invalid question count")
    identities = set()
    for question in value:
        if not isinstance(question, dict):
            raise ValueError("invalid question")
        for key, limit in (("id", 256), ("question", 65536), ("header", 512)):
            text = question.get(key, "" if key == "header" else None)
            if not isinstance(text, str) or len(text) > limit or (key != "header" and not text.strip()):
                raise ValueError("invalid question text")
        if question["id"] in identities:
            raise ValueError("duplicate question identity")
        identities.add(question["id"])
        options = question.get("options") or []
        if not isinstance(options, list) or len(options) > 5:
            raise ValueError("invalid question options")
        labels = set()
        for option in options:
            if not isinstance(option, dict):
                raise ValueError("invalid option")
            label, description = option.get("label"), option.get("description", "")
            if (not isinstance(label, str) or not label.strip() or len(label) > 512
                    or label in labels or not isinstance(description, str) or len(description) > 2048):
                raise ValueError("invalid option text")
            labels.add(label)
    return value
