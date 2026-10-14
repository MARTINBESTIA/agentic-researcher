"""Roly modelov, routing úloh, profily, self-consistency a provenance (R-ACC, spec kap. 5).

Fázy nikdy nevolajú konkrétny model. Zavolajú `router.run_task("entail", …)` a router podľa
profilu vyberie rolu (fast / accurate), uvažovanie a počet hlasov k. Každé volanie zapíše
riadok do model_judgments (úloha, objekt, rola, model, profil, verzia promptu, výstup, tokeny,
čas, orezanie).

Bez sekcie `models` v configu idú všetky úlohy na `llm` (správanie v0.1).

Zlyhanie modelu nezhodí beh: orezaný alebo neplatný výstup je `undecided` a pri rozhodovacích
úlohách ide do kontroly (review_queue); timeout nechá položku na ďalší beh.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, TypeVar

from openai import APITimeoutError
from pydantic import BaseModel

from .config import Config, LLMConfig, ModelsConfig, RoleConfig, TaskRoute
from .db import DB
from .llm import LLM, LLMError, LLMTruncated
from .prompts import prompt_version
from .util import utcnow

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

LEGACY_ROLE = "llm"
PROFILES = ("fast", "balanced", "accurate")


@dataclass
class TaskResult(Generic[T]):
    """Výsledok úlohy. `value` je None, ak je výsledok neurčený (undecided)."""

    task: str
    value: Optional[T] = None
    verdict: str | None = None
    undecided: bool = False
    reason: str | None = None  # truncated | invalid | timeout | disagreement
    low_agreement: bool = False
    votes: list[str] = field(default_factory=list)
    role: str = ""
    model: str = ""

    @property
    def ok(self) -> bool:
        return not self.undecided and self.value is not None


@dataclass(frozen=True)
class Route:
    task: str
    role: str
    model: str
    thinking: bool
    k: int


def _value_dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value


MODEL_SETTINGS_KEY = "models"  # nastavenie z GUI: {"roles": {"agent": "qwen3.6:35b-a3b", …}, "profile": "balanced"}
SWAPS_KEY = "model_swaps"  # posledných 30 načítaní modelu pri výmene (čas, uvoľnené modely)


def gui_model_settings(db: DB | None) -> dict[str, Any]:
    """Výber modelov z GUI (Správca akcií → Modely); prepíše config.yaml pre ďalšie spustené akcie."""
    if db is None or not hasattr(db, "get_setting"):
        return {}
    try:
        return db.get_setting(MODEL_SETTINGS_KEY, {}) or {}
    except Exception:  # noqa: BLE001 – stará databáza bez tabuľky settings: platí config.yaml
        return {}


def apply_role_models(models: ModelsConfig, roles: dict[str, str]) -> ModelsConfig:
    """Kópia konfigurácie s inými modelmi rolí (časové limity a tokeny ostávajú z roly)."""
    new = models.model_copy(deep=True)
    for role, model in roles.items():
        if not model:
            continue
        base = new.roles.get(role) or new.roles.get("fast") or RoleConfig(model=model)
        new.roles[role] = base.model_copy(update={"model": model})
    return new


class ModelRouter:
    """Vyberá model podľa úlohy a zapisuje provenance.

    client_factory(role_name, role_cfg, thinking, temperature) -> klient s metódami chat_json/chat_tools.
    V testoch sa podstrčí falošný klient; v režime v0.1 sa všetko volá cez jeden `legacy_client`.
    """

    def __init__(
        self,
        cfg: Config,
        db: DB | None,
        legacy_client: Any = None,
        client_factory: Callable[[str, RoleConfig, bool, float], Any] | None = None,
        profile: str | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.models: ModelsConfig | None = cfg.models
        self.legacy_client = legacy_client
        self.client_factory = client_factory or self._default_factory
        gui = gui_model_settings(db) if self.models is not None else {}
        if gui.get("roles"):
            self.models = apply_role_models(self.models, gui["roles"])
        self.profile = profile or gui.get("profile") or (self.models.profile if self.models else "v0.1")
        self._clients: dict[tuple[str, str, bool, float], Any] = {}
        self.overrides: dict[str, TaskRoute] = {}  # dočasné nastavenie úlohy (eval)
        self._current_model: str | None = None  # naposledy použitý veľký model (na výmenu v Ollame)

    # ------------------------------------------------------------------ výber
    @property
    def legacy(self) -> bool:
        return self.models is None

    def route(self, task: str) -> Route:
        if self.legacy:
            return Route(task, LEGACY_ROLE, self.cfg.llm.model, False, 1)
        base = self.overrides.get(task) or self.models.tasks.get(task) or TaskRoute()
        role = base.role
        if task in self.overrides:
            pass
        elif self.profile == "fast" and role != "embed":
            role = "fast"
        elif self.profile == "accurate" and role != "embed":
            role = "accurate"
        role_cfg = self.role_config(role)
        return Route(task, role, role_cfg.model, base.thinking, max(1, base.k))

    def role_config(self, role: str) -> RoleConfig:
        if self.legacy:
            llm = self.cfg.llm
            return RoleConfig(model=llm.model, base_url=llm.base_url, timeout_s=llm.timeout_s, max_tokens=llm.max_tokens,
                              temperature=llm.temperature, extra_body=llm.extra_body)
        if role in self.models.roles:
            return self.models.roles[role]
        # Eval profil môže uviesť priamo názov modelu na serveri.
        fast = self.models.roles.get("fast")
        return RoleConfig(model=role, timeout_s=fast.timeout_s if fast else 600, max_tokens=fast.max_tokens if fast else 2048)

    def _default_factory(self, role: str, role_cfg: RoleConfig, thinking: bool, temperature: float) -> LLM:
        m = self.models
        assert m is not None
        extra = dict(role_cfg.extra_body)
        extra.update(m.thinking_on if thinking else m.thinking_off)
        llm_cfg = LLMConfig(
            base_url=role_cfg.base_url or m.base_url,
            api_key=m.api_key,
            model=role_cfg.model,
            temperature=temperature,
            max_tokens=role_cfg.max_tokens + (m.thinking_extra_tokens if thinking else 0),
            timeout_s=role_cfg.timeout_s,
            extra_body=extra,
            unload_every=m.unload_every,
        )
        return LLM(llm_cfg, thinking=thinking)

    def client(self, role: str, thinking: bool = False, temperature: float | None = None) -> Any:
        if self.legacy:
            if self.legacy_client is None:
                self.legacy_client = LLM(self.cfg.llm)
            return self.legacy_client
        role_cfg = self.role_config(role)
        temp = role_cfg.temperature if temperature is None else temperature
        key = (role, role_cfg.model, thinking, round(temp, 3))
        if key not in self._clients:
            self._clients[key] = self.client_factory(role, role_cfg, thinking, temp)
        return self._clients[key]

    def client_for(self, task: str) -> Any:
        r = self.route(task)
        return self.client(r.role, r.thinking)

    def max_tokens(self, route: Route, requested: int | None) -> int | None:
        """Pri uvažovaní pridá rozpočet na premýšľanie, inak by sa odpoveď orezala."""
        if requested is None:
            return None
        if route.thinking and self.models is not None:
            return requested + self.models.thinking_extra_tokens
        return requested

    # --------------------------------------------------------------- volanie
    def run_task(
        self,
        task: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        object_type: str | None = None,
        object_id: int | None = None,
        vote_key: Callable[[T], str] | None = None,
        max_tokens: int | None = None,
        review: bool = False,
        review_detail: dict[str, Any] | None = None,
    ) -> TaskResult[T]:
        """Spustí úlohu (k hlasov pri self-consistency). Nikdy nevyhodí LLMError ani timeout.

        vote_key: z výstupu vyberie verdikt (napr. lambda r: r.verdict); bez neho je verdikt None
        a pri k > 1 sa hlasuje podľa celého výstupu.
        review: pri neurčenom výsledku alebo nezhode zaradí položku do kontroly (iba ak je objekt).
        """
        route = self.route(task)
        k = route.k
        temperature = None
        if k > 1:
            temperature = self.models.self_consistency_temperature if self.models else None
        client = self.client(route.role, route.thinking, temperature)
        version = prompt_version(task)
        samples: list[tuple[T | None, str]] = []  # (výstup, verdikt alebo "undecided")
        failure: str | None = None
        for i in range(k):
            value, verdict, reason = self._one_call(task, route, client, version, messages, schema, max_tokens,
                                                    object_type, object_id, vote_key, sample=i)
            samples.append((value, verdict))
            if reason:
                failure = failure or reason
            if reason == "timeout":
                break  # server je preťažený; ďalšie vzorky by dopadli rovnako
        result = self._decide(task, route, samples, failure, k)
        if review and result.undecided and object_type and object_id is not None and result.reason != "timeout":
            detail = {"votes": result.votes, "model": route.model, **(review_detail or {})}
            if self.db is not None:
                self.db.enqueue_review(task, object_type, object_id, result.reason or "undecided", detail)
        elif review and result.low_agreement and object_type and object_id is not None and self.db is not None:
            self.db.enqueue_review(task, object_type, object_id, "low_agreement",
                                   {"votes": result.votes, "model": route.model, **(review_detail or {})})
        return result

    def _switch_to(self, route: Route, client: Any) -> None:
        """Pri výmene veľkého modelu uvoľní predošlý z GPU (Ollama), aby sa nový zmestil celý."""
        if self.legacy or route.model == self._current_model:
            return
        exclusive = getattr(client, "make_exclusive", None)
        if exclusive is not None:
            try:
                exclusive()
                swap = getattr(client, "last_swap", None)
                if isinstance(swap, dict) and self.db is not None:  # posledné výmeny modelov (GUI: Správca akcií)
                    swaps = (self.db.get_setting(SWAPS_KEY, []) or [])[-29:]
                    self.db.set_setting(SWAPS_KEY, swaps + [swap | {"kedy": utcnow()}])
            except Exception as exc:  # noqa: BLE001 – výmena modelu nesmie zhodiť beh
                log.warning("Uvoľnenie predošlého modelu zlyhalo: %s", exc)
        self._current_model = route.model

    def _one_call(self, task, route: Route, client, version, messages, schema, max_tokens, object_type, object_id,
                  vote_key, sample: int) -> tuple[Any, str, str | None]:
        self._switch_to(route, client)
        value = None
        reason = None
        truncated = False
        try:
            value = client.chat_json(messages, schema, max_tokens=self.max_tokens(route, max_tokens))
        except LLMTruncated as exc:
            reason, truncated = "truncated", True
            log.warning("Úloha %s (%s %s): orezaný výstup – %s", task, object_type or "-", object_id or "-", exc)
        except LLMError as exc:
            reason = "invalid"
            log.warning("Úloha %s (%s %s): neplatný výstup – %s", task, object_type or "-", object_id or "-", exc)
        except APITimeoutError as exc:
            reason = "timeout"
            log.warning("Úloha %s (%s %s): timeout – %s", task, object_type or "-", object_id or "-", exc)
        verdict = "undecided"
        if value is not None:
            verdict = vote_key(value) if vote_key else _verdict_of(value)
        info = getattr(client, "last", None)
        if self.db is not None:
            self.db.add_judgment(
                task=task,
                role=route.role,
                model=getattr(info, "model", None) or route.model,
                profile=self.profile,
                prompt_version=version,
                params={"thinking": route.thinking, "k": route.k, "sample": sample,
                        "temperature": getattr(getattr(client, "cfg", None), "temperature", None),
                        "max_tokens": max_tokens, "error": reason},
                output=_value_dump(value),
                verdict=verdict if value is not None else ("undecided" if reason != "timeout" else "timeout"),
                prompt_tokens=getattr(info, "prompt_tokens", None),
                completion_tokens=getattr(info, "completion_tokens", None),
                latency_s=getattr(info, "latency_s", None),
                truncated=truncated or getattr(info, "finish_reason", None) == "length",
                object_type=object_type,
                object_id=object_id,
            )
        return value, verdict, reason

    def _decide(self, task: str, route: Route, samples: list[tuple[Any, str]], failure: str | None, k: int) -> TaskResult:
        votes = [v for _, v in samples]
        base = dict(task=task, votes=votes, role=route.role, model=route.model)
        if k == 1 or len(samples) == 1:
            value, verdict = samples[0]
            if value is None:
                return TaskResult(undecided=True, reason=failure or "undecided", **base)
            return TaskResult(value=value, verdict=None if verdict == "undecided" else verdict, **base)
        counts = Counter(votes)
        top, n_top = counts.most_common(1)[0]
        majority = k // 2 + 1
        if top == "undecided" or n_top < majority:
            # Nezhoda (1:1:1) alebo väčšina neurčených výstupov: rozhodne človek.
            return TaskResult(undecided=True, reason="disagreement" if top != "undecided" else (failure or "undecided"), **base)
        value = next(v for v, verdict in samples if verdict == top)
        return TaskResult(value=value, verdict=top, low_agreement=n_top < k, **base)

    def run_samples(
        self,
        task: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        object_type: str | None = None,
        object_id: int | None = None,
        max_tokens: int | None = None,
    ) -> list[T]:
        """k vzoriek úlohy (pri k > 1 so zvýšenou teplotou) bez hlasovania – pre úlohy so štruktúrovaným
        výstupom (napr. mapa rolí), kde zhodu počíta volajúci. Neurčené vzorky sa vynechajú."""
        route = self.route(task)
        temperature = self.models.self_consistency_temperature if (self.models and route.k > 1) else None
        client = self.client(route.role, route.thinking, temperature)
        version = prompt_version(task)
        values = []
        for i in range(route.k):
            value, _, reason = self._one_call(task, route, client, version, messages, schema, max_tokens,
                                              object_type, object_id, None, sample=i)
            if value is not None:
                values.append(value)
            if reason == "timeout":
                break
        return values

    # ----------------------------------------------------------------- nástroje
    def chat_tools(self, task: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                   object_type: str | None = None, object_id: int | None = None):
        """Jeden krok agenta s nástrojmi; zapíše provenance, chyby nechá na volajúcom."""
        route = self.route(task)
        client = self.client(route.role, route.thinking)
        self._switch_to(route, client)
        msg = client.chat_tools(messages, tools)
        info = getattr(client, "last", None)
        if self.db is not None:
            calls = [c.function.name for c in (getattr(msg, "tool_calls", None) or [])]
            self.db.add_judgment(
                task=task, role=route.role, model=getattr(info, "model", None) or route.model, profile=self.profile,
                prompt_version=prompt_version(task), params={"thinking": route.thinking, "tools": [t["function"]["name"] for t in tools]},
                output={"tool_calls": calls, "content": (getattr(msg, "content", None) or "")[:2000]},
                verdict=",".join(calls) or "text", prompt_tokens=getattr(info, "prompt_tokens", None),
                completion_tokens=getattr(info, "completion_tokens", None), latency_s=getattr(info, "latency_s", None),
                truncated=getattr(info, "finish_reason", None) == "length", object_type=object_type, object_id=object_id,
            )
        return msg

    def roles_in_use(self) -> dict[str, str]:
        """Rola -> model (na výpis v doctor)."""
        if self.legacy:
            return {LEGACY_ROLE: self.cfg.llm.model}
        return {name: r.model for name, r in self.models.roles.items()}


def _verdict_of(value: Any) -> str:
    """Predvolený verdikt na hlasovanie: pole verdict/decision/same, inak celý výstup."""
    for attr in ("verdict", "decision", "same", "relevant"):
        if hasattr(value, attr):
            return str(getattr(value, attr))
    try:
        return json.dumps(_value_dump(value), ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def as_router(llm_or_router: Any, cfg: Config, db: DB | None) -> ModelRouter:
    """Fázy prijímajú router aj samotného klienta (testy v0.1 posielajú FakeLLM)."""
    if isinstance(llm_or_router, ModelRouter):
        return llm_or_router
    if llm_or_router is None:  # klient sa vytvorí až pri prvom volaní
        return ModelRouter(cfg, db)
    return ModelRouter(cfg, db, legacy_client=llm_or_router) if cfg.models is None else ModelRouter(
        cfg, db, client_factory=lambda *a: llm_or_router
    )
