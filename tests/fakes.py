"""Falošné modely, vyhľadávanie a web pre testy v0.2 (bez siete, deterministické)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from radar.config import Config, ModelsConfig
from radar.fetch import FetchedPage, Fetcher
from radar.llm import CallInfo
from radar.models import ModelRouter
from radar.search import SearchResult


@dataclass
class Recorder:
    """Spoločný záznam volaní všetkých falošných klientov: (názov schémy, model, uvažovanie, teplota)."""

    calls: list[tuple[str, str, bool, float]] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)  # názov schémy -> hodnota | callable | výnimka | zoznam

    def respond(self, schema_name: str, messages: list[dict[str, Any]], model_cls):
        if schema_name not in self.responses:
            raise AssertionError(f"Neočakávaná schéma {schema_name}")
        r = self.responses[schema_name]
        if isinstance(r, list):  # postupné odpovede (napr. hlasovanie)
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r) and not isinstance(r, type):
            r = r(messages)
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, dict):
            return model_cls.model_validate(r)
        return r

    def models_for(self, schema_name: str) -> list[str]:
        return [m for s, m, _, _ in self.calls if s == schema_name]


class BoundFake:
    """Klient jedného modelu (ako LLM), ktorý odpovedá podľa Recorder.responses."""

    def __init__(self, recorder: Recorder, model: str, thinking: bool = False, temperature: float = 0.2):
        self.recorder, self.thinking = recorder, thinking
        self.cfg = SimpleNamespace(model=model, temperature=temperature, max_tokens=2048)
        self.last = CallInfo(model=model)

    def chat_json(self, messages, model_cls, retries: int = 2, max_tokens: int | None = None):
        self.recorder.calls.append((model_cls.__name__, self.cfg.model, self.thinking, self.cfg.temperature))
        self.last = CallInfo(model=self.cfg.model, finish_reason="stop", prompt_tokens=100, completion_tokens=20, latency_s=0.01)
        return self.recorder.respond(model_cls.__name__, messages, model_cls)

    def chat_tools(self, messages, tools):
        self.recorder.calls.append(("tools", self.cfg.model, self.thinking, self.cfg.temperature))
        return self.recorder.respond("tools", messages, None)

    def ping(self) -> str:
        return self.cfg.model


def make_router(cfg: Config, db, recorder: Recorder, profile: str | None = None) -> ModelRouter:
    """Router s rolami (sekcia models) a falošnými klientmi, ktoré zaznamenávajú pole model."""
    if cfg.models is None:
        cfg.models = ModelsConfig()

    def factory(role, role_cfg, thinking, temperature):
        return BoundFake(recorder, role_cfg.model, thinking, temperature)

    return ModelRouter(cfg, db, client_factory=factory, profile=profile)


class FakeSearx:
    def __init__(self, results: dict[str, list[SearchResult]] | Callable[[str], list[SearchResult]] | None = None):
        self.results = results or {}
        self.queries: list[str] = []

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        self.queries.append(query)
        if callable(self.results):
            return self.results(query)[: limit or 8]
        for key, value in self.results.items():
            if key in query:
                return value[: limit or 8]
        return []


class FakeFetcher:
    """Text stránky podľa URL (fetch) a surové HTML (fetch_html)."""

    def __init__(self, texts: dict[str, str] | None = None, htmls: dict[str, str] | None = None):
        self.texts = texts or {}
        self.htmls = htmls or {}
        self.fetched: list[str] = []

    def fetch(self, url: str):
        self.fetched.append(url)
        text = self.texts.get(url)
        if text is None:
            return None
        return FetchedPage(url=url, final_url=url, title=url, text=text, content_hash=str(abs(hash(text))))

    def fetch_html(self, url: str):
        from radar.fetch import HtmlPage

        self.fetched.append(url)
        html = self.htmls.get(url)
        if html is None:
            return HtmlPage(url=url, final_url=url, html="", content_hash="", status="error")
        return HtmlPage(url=url, final_url=url, html=html, content_hash=str(abs(hash(html))), status="ok")


def mock_fetcher(routes: dict[str, httpx.Response], **kwargs) -> Fetcher:
    from radar.config import FetchConfig

    fetcher = Fetcher(FetchConfig(per_domain_delay_s=0, respect_robots=False), **kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    fetcher.http = httpx.Client(transport=httpx.MockTransport(handler))
    return fetcher
