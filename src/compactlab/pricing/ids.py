"""Model IDs as harnesses and platforms write them, reduced to the vendor's own ID plus the platform they point at.

    us.anthropic.claude-fable-5-2-v1:0   -> claude-fable-5-2, Amazon Bedrock, region us
    claude-opus-5-5@20260922             -> claude-opus-5-5, Vertex AI
    anthropic/claude-fable-5.2           -> claude-fable-5-2, OpenRouter
    claude-opus-5-5-20261001             -> claude-opus-5-5 (dated snapshot)
    claude-opus-4-6[1m]                  -> claude-opus-4-6, 1M-context variant

Platform names are models.dev provider IDs, so refreshed rows and lookups use the same keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# the vendor's own API (models.dev provider ID) by ID prefix; earlier entries win
FIRST_PARTY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anthropic", ("claude-",)),
    ("openai", ("gpt-", "o1", "o3", "o4", "codex-", "chatgpt-")),
    ("deepseek", ("deepseek-",)),
    ("moonshotai", ("kimi-", "moonshot-")),
    ("zai", ("glm-",)),
    ("alibaba", ("qwen", "qwq-", "qvq-")),
    ("google", ("gemini-", "gemma-")),
    ("xai", ("grok-",)),
    ("mistral", ("mistral-", "devstral-", "codestral-", "ministral-", "magistral-")),
    ("minimax", ("minimax-",)),
)
# platforms that resell other vendors' models at their own prices
PLATFORMS = ("amazon-bedrock", "google-vertex-anthropic", "google-vertex", "azure", "openrouter")
# what harness configs call a platform -> models.dev provider ID (Codex `model_provider`, LiteLLM `litellm_provider`)
PLATFORM_ALIASES = {
    "bedrock": "amazon-bedrock",
    "bedrock_converse": "amazon-bedrock",
    "amazon-bedrock": "amazon-bedrock",
    "vertex": "google-vertex-anthropic",
    "vertex_ai": "google-vertex-anthropic",
    "vertex_ai-anthropic_models": "google-vertex-anthropic",
    "azure": "azure",
    "azure_ai": "azure",
    "openrouter": "openrouter",
}
BEDROCK = re.compile(
    r"^(?:(us-gov|global|us|eu|apac|au|jp|ca|in)\.)?"
    r"(anthropic|openai|meta|amazon|mistral|deepseek|qwen|moonshot|moonshotai|minimax|google|zai|cohere|writer)\.(.+)$"
)
DATE = re.compile(r"-(?:20\d{6}|20\d{2}-\d{2}-\d{2})$")
# Bedrock model versions: -v1:0, -v1:0:200k (a context variant), -v1, -1:0
BEDROCK_VERSION = re.compile(r"-v?\d+:\d+(?::\d+[km])?$|-v\d+$")
# OpenRouter names models `<vendor>/<model>`; these vendor prefixes mean OpenRouter (or a router using its names)
ROUTER_VENDORS = {"anthropic", "openai", "google", "deepseek", "x-ai", "mistralai", "moonshotai", "qwen", "z-ai",
                  "meta-llama", "minimax"}  # fmt: skip


@dataclass(frozen=True)
class ModelId:
    id: str  # the vendor's own ID: lower case, no platform prefix, date, version or [1m] suffix
    platform: str | None  # the platform the ID's shape points at, or None
    region: str | None  # Bedrock cross-region prefix (us, eu, global, ...)
    one_m: bool  # the [1m] (1M-context) variant


def first_party(mid: str) -> str | None:
    """The vendor's own platform for a vendor ID, or None when the ID matches no known vendor."""
    return next((p for p, prefixes in FIRST_PARTY if mid.startswith(prefixes)), None)


def platform_name(name: str | None) -> str | None:
    """A harness's or dataset's platform name as a models.dev provider ID (None for first-party or unknown)."""
    if not name:
        return None
    n = name.strip().lower()
    return PLATFORM_ALIASES.get(n, n if n in PLATFORMS else None)


def parse(raw: str) -> ModelId:
    s = (raw or "").strip().lower()
    one_m = s.endswith("[1m]")
    s = s.removesuffix("[1m]")
    platform: str | None = None
    region: str | None = None
    if "/" in s:  # OpenRouter `anthropic/claude-...`, LiteLLM `bedrock/...`, ARNs, Vertex resource paths
        head = s.split("/", 1)[0]
        if s.startswith("arn:aws:bedrock"):
            platform = "amazon-bedrock"
        elif head in ("publishers", "projects", "vertex_ai"):
            platform = "vertex"
        elif head in ROUTER_VENDORS or head == "openrouter":
            platform = "openrouter"
        else:  # azure/..., bedrock/...; anything else (ollama/..., a proxy alias) names no platform we price
            platform = PLATFORM_ALIASES.get(head)
        s = s.rsplit("/", 1)[-1]
    m = BEDROCK.match(s)
    if m:
        platform, region, s = "amazon-bedrock", m.group(1), m.group(3)
    if platform == "amazon-bedrock" or re.search(r"-v?\d+:\d+", s):
        s = BEDROCK_VERSION.sub("", s)
    if "@" in s:  # Vertex: claude-opus-5-5@20260922 or @default
        s = s.split("@", 1)[0]
        platform = platform or "vertex"
    s = s.split(":", 1)[0]  # OpenRouter variants (:batch, :free, :thinking)
    s = DATE.sub("", s).removesuffix("-latest")
    if s.startswith("claude-"):  # Anthropic writes versions with hyphens; OpenRouter and others with dots
        s = re.sub(r"(?<=\d)\.(?=\d)", "-", s)
    if platform == "vertex":  # Anthropic's models on Vertex are their own models.dev provider
        platform = "google-vertex-anthropic" if s.startswith("claude-") else "google-vertex"
    return ModelId(s, platform, region, one_m)


def claude_settings_key(model: str) -> str:
    """Claude Code's modelSettings key for a logged model ID: the canonical Claude name, which also matches its alias,
    date-suffixed, [1m] and Bedrock / Vertex IDs; any other ID as logged."""
    m = parse(model)
    if not m.id.startswith("claude-"):
        return model
    return m.id + "[1m]" if m.one_m else m.id  # the 1M variant has a setting of its own


def platform_key(platform: str, mid: ModelId) -> str:
    """Key of a platform's own price row: `amazon-bedrock:us.claude-fable-5-2`, `openrouter:claude-fable-5-2`."""
    return f"{platform}:{mid.region}.{mid.id}" if mid.region else f"{platform}:{mid.id}"


def family(mid: str) -> tuple[str, tuple[int, ...]]:
    """(family, version): `claude-fable-5-2` -> (`claude-fable`, (5, 2)); `gpt-6.1-sol` -> (`gpt-sol`, (6, 1))."""
    version = tuple(int(x) for x in re.findall(r"\d+", mid))
    return re.sub(r"[-._]+", "-", re.sub(r"\d+", "", mid)).strip("-"), version


def nearest(target: tuple[int, ...], versions: list[tuple[int, ...]]) -> tuple[int, ...] | None:
    """The closest known version: the longest shared leading part, then the newest not newer than `target`, else the
    oldest newer one. `fable-5-3` borrows from 5-2, `haiku-5` from 5-5, `opus-4-1` from 4-5."""

    def shared(v: tuple[int, ...]) -> int:
        n = 0
        for a, b in zip(v, target, strict=False):
            if a != b:
                break
            n += 1
        return n

    if not versions:
        return None
    best = max(shared(v) for v in versions)
    pool = [v for v in versions if shared(v) == best]
    older = [v for v in pool if v <= target]
    return max(older) if older else min(pool)
