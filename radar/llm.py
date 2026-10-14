"""Klient pre lokálny model cez OpenAI-kompatibilné API (Ollama, llama-server, llama-swap).

Najdôležitejšia funkcia je chat_json: model musí vrátiť JSON podľa Pydantic schémy.
Server vie výstup obmedziť gramatikou zo schémy (response_format), takže malý
model nevyrobí rozbitý JSON. Ak server daný režim nepodporuje, klient prejde na
jednoduchší a výsledok aj tak overí a pri chybe si vypýta opravu.

Orezaný výstup (finish_reason == "length") sa neopravuje ani nehádá: klient vyhodí
LLMTruncated a volajúci (models.run_task) položku pošle do kontroly (R-ACC-3).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from .config import LLMConfig
from .util import strip_thinking

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Model ani po opravách nevrátil platný výstup."""


class LLMTruncated(LLMError):
    """Výstup bol orezaný limitom tokenov (aj keď model minul tokeny na uvažovanie)."""


@dataclass
class CallInfo:
    """Údaje o poslednom volaní (na zápis do model_judgments)."""

    model: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_s: float | None = None
    reasoning_chars: int = 0
    timings: dict[str, Any] | None = None  # llama-server vracia časy spracovania


def parse_json_object(text: str) -> Any:
    text = strip_thinking(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("vo výstupe nie je JSON objekt")
    return json.loads(text[start : end + 1])


class LLM:
    def __init__(self, cfg: LLMConfig, thinking: bool = False):
        self.cfg = cfg
        self.thinking = thinking  # zapnuté uvažovanie: prázdna odpoveď = minuté tokeny = orezanie
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout_s, max_retries=1)
        self._mode = "json_schema"  # pri odmietnutí serverom: json_object -> none
        self._calls = 0
        self._is_ollama: bool | None = None
        self.last = CallInfo(model=cfg.model)
        self.total_calls = 0

    @property
    def model(self) -> str:
        return self.cfg.model

    @property
    def schema_mode(self) -> str:
        return self._mode

    def ping(self) -> str:
        models = self.client.models.list()
        return ", ".join(m.id for m in models.data) or "(bez názvu)"

    def _server_root(self) -> str:
        return re.sub(r"/v1/?$", "", self.cfg.base_url.rstrip("/"))

    def is_ollama(self) -> bool:
        if self._is_ollama is None:
            try:
                self._is_ollama = httpx.get(self._server_root() + "/api/version", timeout=5).status_code == 200
            except httpx.HTTPError:
                self._is_ollama = False
        return self._is_ollama

    def unload(self) -> bool:
        """Ollama: uvoľní model z pamäte (aj s cache promptov); ďalšie volanie ho načíta znova."""
        if not self.is_ollama():
            return False
        try:
            httpx.post(self._server_root() + "/api/generate", json={"model": self.cfg.model, "keep_alive": 0}, timeout=60)
            log.debug("Model %s uvoľnený z pamäte", self.cfg.model)
            return True
        except httpx.HTTPError as exc:
            log.warning("Uvoľnenie modelu zlyhalo: %s", exc)
            return False

    def _create(self, messages: list[dict[str, Any]], max_tokens: int | None = None, **kwargs: Any):
        start = time.monotonic()
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature,
            max_tokens=max_tokens or self.cfg.max_tokens,
            extra_body=self.cfg.extra_body or None,
            **kwargs,
        )
        latency = time.monotonic() - start
        self._calls += 1
        self.total_calls += 1
        usage = getattr(resp, "usage", None)
        choice = resp.choices[0] if resp.choices else None
        message = getattr(choice, "message", None)
        reasoning = (getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None) or "") if message else ""
        self.last = CallInfo(
            model=self.cfg.model,
            finish_reason=getattr(choice, "finish_reason", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_s=latency,
            reasoning_chars=len(reasoning or ""),
            timings=getattr(resp, "timings", None) or (getattr(resp, "model_extra", None) or {}).get("timings"),
        )
        if self.cfg.unload_every and self._calls % self.cfg.unload_every == 0:
            self.unload()
        return resp

    def make_exclusive(self) -> list[str]:
        """Ollama: uvoľní z GPU ostatné načítané modely (embedding na CPU ostane). Na 8 GB VRAM sa
        rýchly a presný model nezmestia naraz; bez toho Ollama časť presného modelu odloží do zdieľanej
        pamäte a generovanie spadne z ~4 na ~1 token/s (merané 11. 10. 2026)."""
        if not self.is_ollama():
            return []
        try:
            loaded = httpx.get(self._server_root() + "/api/ps", timeout=5).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []
        wanted = {self.cfg.model, f"{self.cfg.model}:latest"}
        unloaded = []
        for m in loaded:
            if m.get("name") in wanted or m.get("model") in wanted or not m.get("size_vram"):
                continue
            try:
                httpx.post(self._server_root() + "/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=60)
                unloaded.append(m["name"])
            except httpx.HTTPError as exc:
                log.warning("Uvoľnenie modelu %s zlyhalo: %s", m.get("name"), exc)
        if unloaded:
            log.info("Výmena modelu: uvoľnené %s, načítava sa %s", ", ".join(unloaded), self.cfg.model)
        self.last_swap = None
        if not any(m.get("name") in wanted or m.get("model") in wanted for m in loaded):
            # načítanie zmerané hneď (prázdny prompt = iba načítanie): koľko stojí výmena modelov na 8 GB VRAM
            t0 = time.monotonic()
            try:
                resp = httpx.post(self._server_root() + "/api/generate", json={"model": self.cfg.model, "prompt": ""},
                                  timeout=900).json()
                self.last_swap = {"model": self.cfg.model, "uvoľnené": unloaded,
                                  "načítanie_s": round((resp.get("load_duration") or 0) / 1e9, 1),
                                  "spolu_s": round(time.monotonic() - t0, 1)}
                log.info("Načítanie %s: %.1f s", self.cfg.model, self.last_swap["spolu_s"])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Meranie načítania %s zlyhalo: %s", self.cfg.model, exc)
        return unloaded

    def gpu_share(self) -> float | None:
        """Ollama: aká časť načítaného modelu je vo VRAM (1.0 = celý na GPU); None, ak sa nedá zistiť."""
        if not self.is_ollama():
            return None
        try:
            models = httpx.get(self._server_root() + "/api/ps", timeout=5).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return None
        wanted = {self.cfg.model, f"{self.cfg.model}:latest"}
        for m in models:
            if m.get("name") in wanted and m.get("size"):
                return (m.get("size_vram") or 0) / m["size"]
        return None

    def generation_speed(self) -> float:
        """Tokeny za sekundu pri krátkej odpovedi (na kontrolu, či model neodkladá pamäť na disk)."""
        self._create([{"role": "user", "content": "Napíš tri krátke vety o počasí."}], max_tokens=10)  # zahriatie
        start = time.monotonic()
        resp = self._create([{"role": "user", "content": "Napíš päť viet o obciach na Slovensku."}], max_tokens=120)
        tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        return tokens / max(time.monotonic() - start, 1e-6)

    def chat(self, messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
        resp = self._create(messages, max_tokens=max_tokens)
        return strip_thinking(resp.choices[0].message.content)

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        """Jeden krok agenta: model odpovie textom alebo požiada o volanie nástrojov."""
        resp = self._create(messages, tools=tools, tool_choice="auto")
        return resp.choices[0].message

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        model_cls: type[T],
        retries: int = 2,
        max_tokens: int | None = None,
    ) -> T:
        schema = model_cls.model_json_schema()
        attempt_messages = list(messages)
        last_error: Exception | None = None
        for _ in range(retries + 1):
            raw = self._json_completion(attempt_messages, schema, model_cls.__name__, max_tokens)
            try:
                return model_cls.model_validate(parse_json_object(raw))
            except (ValueError, ValidationError) as exc:
                last_error = exc
                log.debug("Neplatný JSON pre %s: %s", model_cls.__name__, exc)
                attempt_messages = list(messages) + [
                    {"role": "assistant", "content": raw[:4000]},
                    {
                        "role": "user",
                        "content": "Tvoj výstup nebol platný JSON podľa schémy "
                        f"({str(exc)[:500]}). Vráť iba opravený JSON objekt bez ďalšieho textu.",
                    },
                ]
        raise LLMError(f"Model nevrátil platný JSON pre {model_cls.__name__}: {last_error}")

    def _json_completion(
        self, messages: list[dict[str, Any]], schema: dict[str, Any], name: str, max_tokens: int | None
    ) -> str:
        while True:
            mode = self._mode
            kwargs: dict[str, Any] = {}
            msgs = messages
            if mode == "json_schema":
                kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
            elif mode == "json_object":
                kwargs["response_format"] = {"type": "json_object", "schema": schema}
            else:
                msgs = list(messages) + [
                    {
                        "role": "user",
                        "content": "Odpovedz iba JSON objektom podľa tejto schémy:\n"
                        + json.dumps(schema, ensure_ascii=False),
                    }
                ]
            try:
                resp = self._create(msgs, max_tokens=max_tokens, **kwargs)
            except BadRequestError as exc:
                if mode == "none":
                    raise
                self._mode = "json_object" if mode == "json_schema" else "none"
                log.warning("Server odmietol response_format=%s (%s); prepínam na %s", mode, exc, self._mode)
                continue
            msg = resp.choices[0].message
            content = msg.content or ""
            if self.last.finish_reason == "length":
                # Opakovanie by sa orezalo znova; o položke rozhodne človek (R-ACC-3).
                raise LLMTruncated(f"Výstup pre {name} bol orezaný limitom {max_tokens or self.cfg.max_tokens} tokenov.")
            if not content.strip() and (getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None)):
                if self.thinking:
                    raise LLMTruncated(f"Model pri {name} minul tokeny na uvažovanie a nevrátil odpoveď.")
                # Model minul tokeny na "premýšľanie" – opakovanie nepomôže, treba ho vypnúť.
                raise LLMError(
                    "Model vrátil len premýšľanie bez odpovede. Vypni ho v config.yaml: "
                    "llm.extra_body.reasoning_effort: none (Ollama) alebo chat_template_kwargs.enable_thinking: false (llama-server)."
                )
            return content
