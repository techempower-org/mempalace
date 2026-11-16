"""
llm_client.py — Minimal provider abstraction for LLM-assisted entity refinement.

Three providers cover the useful space:

- ``ollama`` (default): local models via http://localhost:11434. Works fully
  offline. Honors MemPalace's "zero-API required" principle.
- ``openai-compat``: any OpenAI-compatible ``/v1/chat/completions`` endpoint.
  Covers OpenRouter, LM Studio, llama.cpp server, vLLM, Groq, Fireworks,
  Together, and most self-hosted setups.
- ``anthropic``: the official Messages API. Opt-in for users who want Haiku
  quality without setting up a local model.

All providers expose the same ``classify(system, user, json_mode)`` method and
the same ``check_available()`` probe. No external SDK dependencies — stdlib
``urllib`` only.

JSON mode matters here: we always ask for structured output. Providers
differ on how to request it (Ollama: ``format: json``; OpenAI-compat:
``response_format``; Anthropic: prompt-level instruction) and this module
normalizes that away from the caller.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("mempalace_llm")


# ── External-service heuristic (issue #24 — privacy warning support) ─────
# Used by ``LLMProvider.is_external_service`` to decide whether the
# provider's configured endpoint will send user content off the local
# machine/network. Single source of truth so all three providers share
# identical "local vs external" semantics.

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


# Tailscale and other CGNAT overlays hand out 100.64.0.0/10, which Python's
# ``is_private`` does not cover; a local LLM reached over one is still the
# user's own network.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_private_address(addr) -> bool:
    """Loopback, private (RFC 1918, IPv6 unique-local), link-local or CGNAT."""
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    return addr.version == 4 and addr in _CGNAT


def _resolves_private(host: str) -> bool:
    """True when every address ``host`` resolves to stays on the user's network."""
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos if info and info[4]}
    if not addresses:
        return False
    for raw in addresses:
        try:
            addr = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if not _is_private_address(addr):
            return False
    return True


def _endpoint_is_local(url: Optional[str]) -> bool:
    """Return True if ``url``'s hostname is on the user's machine or
    private network.

    Local includes:
      - localhost, 127.0.0.1, ::1
      - IP literals that are loopback, RFC 1918 (10/8, 172.16/12,
        192.168/16), link-local, IPv6 unique-local (fc00::/7), or CGNAT
        100.64.0.0/10 (Tailscale and similar overlays)
      - a single-label or ``.local`` hostname whose every resolved address
        is one of the above

    None / empty / unparseable URLs are treated as local (defensive default —
    no endpoint means no external request can happen yet).

    Anything else (including public IPs and FQDNs) is external.
    """
    if not url:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return True
    if host in _LOCALHOST_HOSTS:
        return True
    # An IP literal is judged by its address, never by how the text starts:
    # ``10.example.com`` or ``fd.example.com`` are public names that merely
    # begin like a private range, and a prefix test waved them through the
    # consent gate that guards sending palace content.
    try:
        return _is_private_address(ipaddress.ip_address(host))
    except ValueError:
        pass
    # A single-label hostname (``gpu-box``) or an mDNS name (``gpu-box.local``)
    # usually names a LAN machine, but a resolver search domain, or an office
    # network that uses ``.local`` for unicast DNS, can point it anywhere, so
    # the name shape proves nothing: resolve it and require every address to
    # be private, loopback, link-local or CGNAT. Unresolvable or public means
    # external, and the user can still opt in with the explicit consent flag.
    if "." not in host or host.endswith(".local"):
        return _resolves_private(host)
    # Any other domain name is external: DNS can point it anywhere.
    return False


def endpoint_is_local(url: Optional[str]) -> bool:
    """Public name for :func:`_endpoint_is_local`."""
    return _endpoint_is_local(url)


def resolve_openai_url(endpoint: str, leaf: str) -> str:
    """Join an OpenAI-compatible base URL with ``leaf`` (``chat/completions`` or ``embeddings``)."""
    url = endpoint.rstrip("/")
    suffix = "/" + leaf.strip("/")
    if url.endswith(suffix):
        return url
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return f"{url}/{leaf.strip('/')}"


# Masks for keys an endpoint URL, a header or an echoed reply can carry into an error message (ported from
# upstream 857fbec4, "mask endpoint credentials in embedding and LLM errors"; 2026-11-15 sync).
_SECRET_NAME = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|pwd|auth(?:orization)?)"
_SECRETS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+"), r"\1 ***"),
    # user:password@ in a URL's authority
    (re.compile(r"(//)[^/@\s]*@"), r"\1***@"),
    # ?key=… query parameters: the whole value
    (re.compile(r"(?i)([?&](?:[a-z_-]*" + _SECRET_NAME + r")=)[^&\s#]+"), r"\1***"),
    # "token": "…", password='…', secret: … (JSON, YAML, headers): the whole value, quoted or bare
    (
        re.compile(
            r"(?i)([\"']?[a-z_-]*"
            + _SECRET_NAME
            + r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
        ),
        r"\1***",
    ),
)


def redact_secrets(text: str) -> str:
    """Mask credentials a URL or one of our own messages may carry: a second layer, not a filter for untrusted
    text (errors never repeat an endpoint's reply; see describe_failure). Patterns cannot find every secret: an
    escaped quote or a multi-line YAML value defeats the assignment mask, which is why replies are not echoed."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class LLMError(RuntimeError):
    """Raised for any provider failure — transport, parse, auth, missing model.

    ``status`` is the HTTP status when the endpoint answered with an error.
    The message is masked: an endpoint URL or reply may carry a key.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(redact_secrets(str(message)))
        self.status = status


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    raw: dict


# ==================== BASE ====================


class LLMProvider:
    name: str = "base"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
        api_key_source: Optional[str] = None,
    ):
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        # Provenance of api_key (issue #26): "flag" when the constructor
        # received an explicit api_key arg, "env" when it fell back to an
        # environment variable, None when no key is in play. cmd_init
        # uses this to gate the consent prompt — stray env-resolved keys
        # require explicit user confirmation.
        self.api_key_source = api_key_source
        # Set by a caller whose consent gate has passed for an external
        # endpoint (`rooms propose` / `kg normalize` with
        # --accept-external-llm). Until then an env-resolved key is withheld
        # from an external endpoint's model listing; see served_models().
        self.external_use_accepted = False

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        """Classify a (system, user) pair into a structured response.

        ``think`` controls reasoning emission for thinking-capable models
        (currently honored by ``OllamaProvider`` for Qwen 3 / DeepSeek-R1
        style toggles). Other providers ignore it. Pass ``False`` to
        disable reasoning when the caller wants a fast classification
        without ``<think>`` overhead.
        """
        raise NotImplementedError

    def check_available(self) -> tuple[bool, str]:
        """Return ``(ok, message)``. Fast probe that the provider is reachable."""
        raise NotImplementedError

    @property
    def is_external_service(self) -> bool:
        """Return True if this provider's endpoint will send user content
        off the local machine/network.

        Used by ``mempalace init`` to decide whether to print a privacy
        warning before first use (issue #24). URL-based heuristic only —
        the endpoint determines, regardless of which provider class.
        Subclasses that resolve their endpoint dynamically should override
        if needed; the default works for the three in-tree providers
        (Ollama / OpenAI-compat / Anthropic).
        """
        return not _endpoint_is_local(self.endpoint)

    def sends_api_key(self) -> bool:
        """Whether the stored key may be attached to a request.

        An environment key is withheld from an external endpoint until the
        caller has accepted that use. An explicit key is sent.
        """
        if not self.api_key:
            return False
        if self.api_key_source != "env":
            return True
        if not self.is_external_service:
            return True
        return bool(self.external_use_accepted)

    def block_silent_external_key(self) -> Optional[str]:
        """Error when classify would send an env key off-machine, else None."""
        if self.sends_api_key() or self.api_key_source != "env":
            return None
        if not self.is_external_service or self.external_use_accepted:
            return None
        return (
            "Refusing to contact a non-local endpoint with an environment API key. "
            "Pass the key explicitly or accept external use."
        )


def require_external_consent(
    provider: LLMProvider,
    *,
    accept: bool = False,
    interactive: bool = False,
    prompt: Optional[str] = None,
) -> bool:
    """Allow an external endpoint only with an explicit yes.

    Local endpoints are allowed. ``accept`` records consent without a prompt.
    ``interactive`` asks on stdin. A yes sets ``external_use_accepted`` so an
    environment key may be sent.
    """
    if not provider.is_external_service:
        return True
    if accept:
        provider.external_use_accepted = True
        return True
    if interactive:
        text = prompt or "  This endpoint is external. Continue? [y/N] "
        try:
            answer = input(text).strip().lower()
        except EOFError:
            answer = ""
        if answer == "y":
            provider.external_use_accepted = True
            return True
    return False


_PLAIN_PATH = re.compile(r"[A-Za-z0-9/_.\-]{0,64}")


def endpoint_label(url: str) -> str:
    """The endpoint as an error may name it: scheme, host, port and a plain path. Never userinfo, a query or a
    fragment, which can carry a key under any spelling (xreview 2026-11-15); a URL that does not parse cleanly
    is "the configured endpoint"."""
    vague = "the configured endpoint"
    if not isinstance(url, str) or any(c.isspace() for c in url) or url.count("@") > 1:
        return vague
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return vague
    if not u.scheme or not u.hostname:
        return vague
    path = u.path if _PLAIN_PATH.fullmatch(u.path or "") else ""
    return f"{u.scheme}://{u.hostname}{f':{port}' if port else ''}{path}"


def describe_failure(e: BaseException) -> str:
    """What went wrong, in words that never come from the endpoint: the exception's type, and an OS error's own
    description. Never the exception's text, a reply, or an HTTP reason phrase, any of which can carry a key
    (xreview 2026-11-15: no pattern reliably finds an arbitrary credential in untrusted text)."""
    if isinstance(e, HTTPError):
        return f"HTTP {e.code}" + (" (authentication failed)" if e.code in (401, 403) else "")
    if isinstance(e, json.JSONDecodeError):
        return f"malformed JSON at line {e.lineno} column {e.colno}"
    reason = getattr(e, "reason", None)
    inner = reason if isinstance(reason, BaseException) else e
    # the OS's own words for an errno (os.strerror), never the exception's stored strerror text
    errno_ = getattr(inner, "errno", None)
    desc = os.strerror(errno_) if isinstance(errno_, int) and errno_ > 0 else ""
    return type(inner).__name__ + (f" ({desc})" if desc else "")


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    """POST JSON and return the parsed response. Raises LLMError on any failure.

    The error names the (masked) endpoint and describes the failure in fixed words; it never repeats the reply.
    Every wrap is ``from None``: the original exception, whose text can hold the URL's credentials or the reply,
    is neither the cause nor the context a traceback prints.
    """
    try:
        # inside the guard: Request itself raises ValueError on a bad URL, with the URL in its text
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise LLMError(f"{describe_failure(e)} from {endpoint_label(url)}", status=e.code) from None
    except json.JSONDecodeError as e:
        raise LLMError(
            f"Malformed response from {endpoint_label(url)}: {describe_failure(e)}"
        ) from None
    except ValueError:  # an invalid or missing URL scheme
        raise LLMError(f"Cannot reach {endpoint_label(url)}: invalid URL") from None
    except (URLError, OSError, http.client.HTTPException) as e:
        raise LLMError(f"Cannot reach {endpoint_label(url)}: {describe_failure(e)}") from None


# ==================== OLLAMA ====================


class OllamaProvider(LLMProvider):
    name = "ollama"
    DEFAULT_ENDPOINT = "http://localhost:11434"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        timeout: int = 180,
        num_ctx: Optional[int] = None,
        **_: object,
    ):
        super().__init__(
            model=model,
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            timeout=timeout,
        )
        self.num_ctx = num_ctx

    def check_available(self) -> tuple[bool, str]:
        try:
            with urlopen(f"{self.endpoint}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read())
        except (URLError, HTTPError, OSError, json.JSONDecodeError) as e:
            return False, f"Cannot reach Ollama at {self.endpoint}: {e}"
        names = {m.get("name", "") for m in data.get("models", []) or []}
        # Ollama tags may or may not include ':latest' — accept either form
        wanted = {self.model, f"{self.model}:latest"}
        if not names & wanted:
            return (
                False,
                f"Model '{self.model}' not loaded in Ollama. Run: ollama pull {self.model}",
            )
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        options: dict = {"temperature": 0.1}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options,
        }
        if json_mode:
            body["format"] = "json"
        if think is not None:
            # Ollama 0.7+ supports `think` for thinking-capable models (Qwen 3
            # family, DeepSeek-R1). Pure-instruct models ignore it. We forward
            # only when the caller explicitly opts in/out so the wire format
            # stays minimal for the common case.
            body["think"] = think
        data = _http_post_json(f"{self.endpoint}/api/chat", body, headers={}, timeout=self.timeout)
        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise LLMError(f"Empty response from Ollama (model={self.model})")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=data)


# ==================== OPENAI-COMPAT ====================


class OpenAICompatProvider(LLMProvider):
    """Any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Supply ``--llm-endpoint http://host:port`` (with or without ``/v1``).
    API key via ``--llm-api-key`` or the ``OPENAI_API_KEY`` env var.
    """

    name = "openai-compat"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
        **_: object,
    ):
        if api_key:
            resolved_key = api_key
            source: Optional[str] = "flag"
        else:
            env_key = os.environ.get("OPENAI_API_KEY")
            resolved_key = env_key or None
            source = "env" if env_key else None
        super().__init__(
            model=model,
            endpoint=endpoint,
            api_key=resolved_key,
            timeout=timeout,
            api_key_source=source,
        )

    def _resolve_url(self) -> str:
        if not self.endpoint:
            raise LLMError("openai-compat provider requires --llm-endpoint")
        return resolve_openai_url(self.endpoint, "chat/completions")

    def _models_url(self) -> str:
        base = self.endpoint.rstrip("/")
        base = base.removesuffix("/chat/completions").removesuffix("/v1")
        return f"{base}/v1/models"

    def served_models(self) -> list[str]:
        """Model ids the endpoint lists; ``[]`` when it cannot be read."""
        req = Request(self._models_url())
        if self.sends_api_key():
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        items = data.get("data") if isinstance(data, dict) else data
        return [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]

    def check_available(self) -> tuple[bool, str]:
        """Reachability probe; ``model="auto"`` is resolved here to the served model.

        A local server (vLLM, NInfer, LM Studio) serves one model at a time
        and its id changes whenever the operator swaps it; ``auto`` follows
        the swap instead of failing with ``model_not_found``.
        """
        if not self.endpoint:
            return False, "no --llm-endpoint configured"
        try:
            models = self.served_models()
        except (URLError, HTTPError, OSError, ValueError) as e:
            return False, f"Cannot reach {self.endpoint}: {e}"
        if self.model == "auto":
            if not models:
                return False, f"{self.endpoint} lists no models to pick from"
            self.model = models[0]
        elif models and self.model not in models:
            # A gateway may list only part of what it routes, or spell a model
            # differently (``llama3.1`` vs ``llama3.1:8b``); the request itself
            # is the authority, so this is advice, not a refusal.
            logger.warning(
                "model %r is not in the list served at %s (%s); continuing. "
                "Use --llm-model auto to follow the server.",
                self.model,
                self.endpoint,
                ", ".join(models[:8]),
            )
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        """``think=False`` sends ``reasoning_effort: "none"``.

        Reasoning models served through an OpenAI-compatible endpoint (Qwen 3.x
        on vLLM, NInfer, llama.cpp) think before answering, and their default
        effort can spend the whole completion budget on the reasoning channel
        and return an empty ``content`` — a 60-excerpt room proposal did
        exactly that at 8192 reasoning tokens. ``reasoning_effort`` is the
        OpenAI field for this and the servers above honor it; a server that
        rejects unknown fields with HTTP 400 gets one retry without it.
        """
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if think is False:
            body["reasoning_effort"] = "none"
        blocked = self.block_silent_external_key()
        if blocked:
            raise LLMError(blocked)
        headers = {}
        if self.sends_api_key():
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self._resolve_url()
        try:
            data = _http_post_json(url, body, headers=headers, timeout=self.timeout)
        except LLMError as e:
            if "reasoning_effort" not in body or "HTTP 400" not in str(e):
                raise
            body.pop("reasoning_effort")
            data = _http_post_json(url, body, headers=headers, timeout=self.timeout)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {e}") from e
        if not text:
            reason = ""
            if isinstance(choice, dict) and choice.get("finish_reason") == "length":
                reason = (
                    " — the completion budget ran out (finish_reason=length); a reasoning "
                    "model likely spent it thinking. Pass think=False or lower the "
                    "model's reasoning effort."
                )
            raise LLMError(f"Empty response from {self.name} (model={self.model}){reason}")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=data)


# ==================== ANTHROPIC ====================


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    DEFAULT_ENDPOINT = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: int = 120,
        **_: object,
    ):
        if api_key:
            resolved_key = api_key
            source: Optional[str] = "flag"
        else:
            env_key = os.environ.get("ANTHROPIC_API_KEY")
            resolved_key = env_key or None
            source = "env" if env_key else None
        super().__init__(
            model=model,
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            api_key=resolved_key,
            timeout=timeout,
            api_key_source=source,
        )

    def check_available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY not set (use --llm-api-key or env)"
        # Don't probe — a live request would cost money. First real call will
        # surface auth errors if the key is invalid.
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,  # noqa: ARG002 — accepted for interface compat; Anthropic extended thinking is configured separately
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("Anthropic provider requires ANTHROPIC_API_KEY env or --llm-api-key")
        blocked = self.block_silent_external_key()
        if blocked:
            raise LLMError(blocked)
        sys_prompt = system
        if json_mode:
            sys_prompt += "\n\nRespond with valid JSON only, no prose."
        body = {
            "model": self.model,
            "max_tokens": 2048,
            "temperature": 0.1,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "X-API-Key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }
        if not self.sends_api_key():
            headers.pop("X-API-Key", None)
        data = _http_post_json(
            f"{self.endpoint}/v1/messages", body, headers=headers, timeout=self.timeout
        )
        try:
            text = "".join(
                b.get("text", "") for b in data.get("content", []) or [] if b.get("type") == "text"
            )
        except (AttributeError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {e}") from e
        if not text:
            raise LLMError(f"Empty response from Anthropic (model={self.model})")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=data)


# ==================== FACTORY ====================


PROVIDERS: dict[str, type[LLMProvider]] = {
    "ollama": OllamaProvider,
    "openai-compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}


def get_provider(
    name: str,
    model: str,
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 120,
    **provider_kwargs: object,
) -> LLMProvider:
    """Build a provider by name. Raises LLMError on unknown provider.

    Extra kwargs (e.g. num_ctx for Ollama) are forwarded to the provider's
    constructor; providers that don't recognize them ignore via **_.
    """
    cls = PROVIDERS.get(name)
    if cls is None:
        raise LLMError(f"Unknown provider '{name}'. Choices: {sorted(PROVIDERS.keys())}")
    return cls(model=model, endpoint=endpoint, api_key=api_key, timeout=timeout, **provider_kwargs)
