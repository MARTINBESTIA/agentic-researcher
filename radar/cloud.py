"""Cloudové modely (Azure OpenAI v1) vedľa lokálnych (audit 15. 10. 2026, balík D).

- Profil poskytovateľa (`models.providers`) má endpoint a **názov premennej prostredia** s kľúčom – tajomstvo sa
  neukladá do YAML, SQLite, logov ani promptov. Rola modelu (`RoleConfig.provider`, `deployment`) určí, či ide na
  lokálny server alebo do cloudu; v poli `model` sa do Azure posiela názov nasadenia.
- Parametre podľa schopností: reasoning model dostane `max_completion_tokens` bez `temperature`; lokálne
  `thinking_on/off`, Ollama probe ani uvoľňovanie modelu sa do Azure neposielajú.
- Chyby sa triedia (autentifikácia, nenájdené nasadenie, nepodporovaný parameter, formát, limit rýchlosti, kvóta,
  dočasná chyba); fallback JSON režimu iba pri potvrdenej nepodpore formátu; opakovanie obmedzené počtom aj
  rozpočtom; žiadny tichý prechod na iný model. Zlyhanie = položka čaká (LLMError), nikdy falošné schválenie.
- Rozpočet (`cloud`): pred volaním rezervácia horného odhadu ceny v jednej transakcii s limitmi (celkový, denný, beh),
  po odpovedi vyrovnanie podľa skutočných tokenov; pri timeout-e ostáva rezervácia (neistý výsledok). Bez sadzby sa
  nevolá. Zobrazená spotreba je vlastný odhad, nie overený zostatok Azure.
- Nasadenia sa načítajú iba čítaním cez Azure Resource Manager (GET); nič sa nevytvára ani nemení. Bez oprávnenia
  ostáva ručné pridanie existujúceho nasadenia.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from openai import (
    APIConnectionError, APITimeoutError, AuthenticationError, BadRequestError, InternalServerError, NotFoundError,
    OpenAI, PermissionDeniedError, RateLimitError,
)

from .config import CloudConfig, CloudRate, LLMConfig, ModelsConfig, ProviderProfile, RoleConfig
from .llm import LLM, CallInfo, LLMError

log = logging.getLogger(__name__)

CLOUD_SETTINGS_KEY = "cloud"  # GUI: {"rates": {...}, "budget_total_eur": …, "deployments": {profil: [...]}}
FORMAT_HINTS = ("response_format", "json_schema", "json_object")


class CloudError(LLMError):
    """Chyba cloudového volania – položka ostane na dokončenie (invariant 5)."""

    kind = "cloud"


class CloudAuthError(CloudError):
    kind = "autentifikácia"


class DeploymentNotFound(CloudError):
    kind = "nasadenie nenájdené"


class UnsupportedParameter(CloudError):
    kind = "nepodporovaný parameter"


class RateLimited(CloudError):
    kind = "limit rýchlosti"


class QuotaExceeded(CloudError):
    kind = "kvóta"


class TransientCloudError(CloudError):
    kind = "dočasná chyba služby"


class BudgetExceeded(CloudError):
    kind = "rozpočet"


class CloudNotAllowed(CloudError):
    kind = "súkromie"


def secret_present(env: str) -> bool:
    """Je kľúč v premennej prostredia (bez čítania hodnoty do výstupu)."""
    return bool(env) and bool(os.environ.get(env))


# ======================================================================= rozpočet
def rate_for(cloud: CloudConfig, deployment: str) -> CloudRate | None:
    return cloud.rates.get(deployment)


def estimate_eur(rate: CloudRate, prompt_tokens: float, completion_tokens: float) -> float:
    return (prompt_tokens * rate.input_per_1m + completion_tokens * rate.output_per_1m) / 1_000_000


class CloudBudget:
    """Rezervácia a vyrovnanie nákladov cloudových volaní (tabuľka llm_usage) s limitmi celkom, za deň a za beh."""

    def __init__(self, cloud: CloudConfig, db, run_started: str | None = None):
        self.cloud = cloud
        self.db = db
        self.run_started = run_started or datetime.now(timezone.utc).isoformat(timespec="seconds")

    def limits(self) -> dict[str, tuple[str, float]]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        return {"celkom": (self.cloud.budget_since or "0000", self.cloud.budget_total_eur),
                "deň": (today, self.cloud.budget_daily_eur), "beh": (self.run_started, self.cloud.budget_run_eur)}

    def reserve(self, provider: str, profile: str, deployment: str, task: str | None, rate: CloudRate | None,
                prompt_chars: int, max_tokens: int) -> int:
        if self.db is None:
            raise BudgetExceeded("cloudové volanie bez databázy nákladov nie je povolené")
        if self.cloud.budget_total_eur <= 0:
            raise BudgetExceeded("cloudový rozpočet nie je nastavený (cloud.budget_total_eur = 0)")
        if rate is None:
            raise BudgetExceeded(f"chýba sadzba pre nasadenie {deployment} – cena sa nenahrádza nulou (nastav ju v GUI)")
        tokens_in = math.ceil(prompt_chars / max(self.cloud.chars_per_token, 0.5))
        reserve = estimate_eur(rate, tokens_in, max_tokens)
        usage_id = self.db.reserve_cloud(provider, profile, deployment, task or "", reserve, self.limits(),
                                         rate.model_dump())
        if usage_id is None:
            raise BudgetExceeded(f"vyčerpaný limit cloudového rozpočtu (rezervácia {reserve:.4f} {rate.currency})")
        return usage_id

    def settle(self, usage_id: int, rate: CloudRate, usage: Any) -> float:
        prompt = getattr(usage, "prompt_tokens", None) or 0
        completion = getattr(usage, "completion_tokens", None) or 0  # obsahuje aj reasoning tokeny (bez dvojitého rátania)
        details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
        cost = estimate_eur(rate, prompt, completion)
        self.db.settle_cloud(usage_id, "settled", cost, prompt, completion, reasoning)
        return cost

    def fail(self, usage_id: int, note: str) -> None:
        self.db.settle_cloud(usage_id, "failed", 0.0, note=note[:200])

    def uncertain(self, usage_id: int, note: str) -> None:
        """Timeout: nevieme, či služba požiadavku spracovala – rezervácia ostáva započítaná."""
        self.db.settle_cloud(usage_id, "uncertain", None, note=note[:200])


# ======================================================================= klient
class CloudLLM(LLM):
    """Klient Azure OpenAI v1 (štandardný OpenAI klient s Azure base_url; `model` = názov nasadenia)."""

    def __init__(self, cfg: LLMConfig, *, provider: str, profile: ProviderProfile, role_cfg: RoleConfig,
                 budget: CloudBudget | None, cloud: CloudConfig, client: Any = None,
                 sleep: Callable[[float], None] | None = None):
        super().__init__(cfg)
        self.provider, self.profile, self.role_cfg = provider, profile, role_cfg
        self.deployment = role_cfg.deployment or role_cfg.model
        self.budget, self.cloud = budget, cloud
        self._mode = role_cfg.json_mode
        self._sleep = sleep or time.sleep
        self._has_key = secret_present(profile.api_key_env)
        self.client = client or OpenAI(base_url=profile.endpoint or "https://invalid.local/",
                                       api_key=os.environ.get(profile.api_key_env, "") or "missing",
                                       timeout=cfg.timeout_s, max_retries=0)
        self.task: str | None = None  # nastaví router pred volaním (rezervácia nákladov podľa úlohy)
        self.last = CallInfo(model=self.deployment)

    @property
    def model(self) -> str:
        return self.deployment

    # lokálne operácie Ollamy sa do cloudu neposielajú
    def is_ollama(self) -> bool:
        return False

    def unload(self) -> bool:
        return False

    def make_exclusive(self) -> list[str]:
        self.last_swap = None
        return []

    def gpu_share(self) -> float | None:
        return None

    def _params(self, messages: list[dict[str, Any]], max_tokens: int | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        limit = max_tokens or self.role_cfg.max_tokens
        params: dict[str, Any] = {"model": self.deployment, "messages": messages}
        if self.role_cfg.reasoning:  # tokeny uvažovania sa rátajú do limitu – rezerva, aby výstup nebol orezaný
            params["max_completion_tokens"] = limit + self.cloud.reasoning_extra_tokens
        else:
            params["temperature"] = self.cfg.temperature
            params["max_tokens"] = limit
        params.update(kwargs)
        return params

    def _create(self, messages: list[dict[str, Any]], max_tokens: int | None = None, **kwargs: Any):
        if not self._has_key:
            raise CloudAuthError(f"chýba kľúč v premennej prostredia {self.profile.api_key_env} (profil {self.provider})")
        params = self._params(messages, max_tokens, kwargs)
        rate = rate_for(self.cloud, self.deployment)
        chars = sum(len(str(m.get("content") or "")) for m in messages) + len(json.dumps(kwargs.get("response_format") or {}))
        limit = params.get("max_completion_tokens") or params.get("max_tokens") or 0
        attempts = 0
        while True:
            usage_id = self.budget.reserve(self.provider, self.provider, self.deployment, self.task, rate, chars, limit) \
                if self.budget is not None else None
            start = time.monotonic()
            try:
                resp = self.client.chat.completions.create(**params)
            except APITimeoutError:
                if usage_id is not None:
                    self.budget.uncertain(usage_id, "timeout")
                raise
            except (AuthenticationError, PermissionDeniedError) as exc:
                self._fail(usage_id, exc)
                raise CloudAuthError(f"autentifikácia alebo oprávnenie zlyhalo ({type(exc).__name__})") from exc
            except NotFoundError as exc:
                self._fail(usage_id, exc)
                raise DeploymentNotFound(f"nasadenie {self.deployment} sa nenašlo na {self.profile.endpoint}") from exc
            except BadRequestError as exc:
                self._fail(usage_id, exc)
                text = str(exc).lower()
                if any(h in text for h in FORMAT_HINTS) and "response_format" in params:
                    raise  # potvrdená nepodpora formátu → LLM._json_completion prejde na jednoduchší režim
                raise UnsupportedParameter(f"nasadenie {self.deployment} odmietlo požiadavku: {str(exc)[:200]}") from exc
            except RateLimitError as exc:
                self._fail(usage_id, exc)
                text = str(exc).lower()
                if "quota" in text or "insufficient" in text:
                    raise QuotaExceeded(f"vyčerpaná kvóta nasadenia {self.deployment}") from exc
                attempts += 1
                if attempts > self.cloud.max_retries:
                    raise RateLimited(f"limit rýchlosti nasadenia {self.deployment} (po {attempts - 1} opakovaniach)") from exc
                self._sleep(min(30.0, 2.0 ** attempts))
                continue
            except (APIConnectionError, InternalServerError) as exc:
                self._fail(usage_id, exc)
                attempts += 1
                if attempts > self.cloud.max_retries:
                    raise TransientCloudError(f"dočasná chyba služby ({type(exc).__name__})") from exc
                self._sleep(min(30.0, 2.0 ** attempts))
                continue
            latency = time.monotonic() - start
            usage = getattr(resp, "usage", None)
            if usage_id is not None and rate is not None:
                self.budget.settle(usage_id, rate, usage)
            choice = resp.choices[0] if resp.choices else None
            self._calls += 1
            self.total_calls += 1
            self.last = CallInfo(model=self.deployment, finish_reason=getattr(choice, "finish_reason", None),
                                 prompt_tokens=getattr(usage, "prompt_tokens", None),
                                 completion_tokens=getattr(usage, "completion_tokens", None), latency_s=latency)
            return resp

    def _fail(self, usage_id: int | None, exc: Exception) -> None:
        if usage_id is not None:
            self.budget.fail(usage_id, type(exc).__name__)


def make_cloud_client(models: ModelsConfig, cloud: CloudConfig, db, role_cfg: RoleConfig, temperature: float,
                      budget: CloudBudget | None, client: Any = None,
                      sleep: Callable[[float], None] | None = None) -> CloudLLM:
    profile = models.providers.get(role_cfg.provider)
    if profile is None:
        raise CloudError(f"neznámy profil poskytovateľa {role_cfg.provider!r}")
    cfg = LLMConfig(base_url=profile.endpoint or "https://invalid.local/", api_key="-", model=role_cfg.deployment or role_cfg.model,
                    temperature=temperature, max_tokens=role_cfg.max_tokens, timeout_s=role_cfg.timeout_s,
                    extra_body={}, unload_every=0)
    return CloudLLM(cfg, provider=role_cfg.provider, profile=profile, role_cfg=role_cfg, budget=budget, cloud=cloud,
                    client=client, sleep=sleep)


# ======================================================================= cache
def cache_key(role_cfg: RoleConfig, profile: ProviderProfile | None, version: str | None, prompt_version: str,
              params: dict[str, Any], messages: list[dict[str, Any]], schema_name: str) -> str:
    """Kľúč odpovede: provider, endpoint (bez tajomstva), nasadenie, známa verzia modelu, verzia promptu, parametre
    a hash vstupu. Zmena nasadenia alebo promptu = iný kľúč (staré hodnotenie sa nevydáva za nové)."""
    payload = [role_cfg.provider, profile.endpoint if profile else "", role_cfg.deployment or role_cfg.model, version or "",
               prompt_version, params, schema_name, messages]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


# ======================================================================= nasadenia a test
class DeploymentListError(RuntimeError):
    pass


def list_deployments(profile: ProviderProfile, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Existujúce nasadenia Azure OpenAI resource (iba čítanie cez Azure Resource Manager). Inference kľúč nestačí –
    treba Bearer token s oprávnením čítať resource; bez neho zrozumiteľná chyba a ostáva ručné pridanie."""
    if not (profile.subscription_id and profile.resource_group and profile.account_name):
        raise DeploymentListError("Na načítanie nasadení vyplň subscription, resource group a názov Azure OpenAI "
                                  "resource; inak pridaj existujúce nasadenie ručne.")
    token = os.environ.get(profile.mgmt_token_env or "", "")
    if not token:
        raise DeploymentListError(f"Chýba token na čítanie správy resource v premennej {profile.mgmt_token_env} "
                                  "(napr. az account get-access-token). Nasadenie môžeš pridať ručne.")
    url = (f"https://management.azure.com/subscriptions/{profile.subscription_id}/resourceGroups/{profile.resource_group}"
           f"/providers/Microsoft.CognitiveServices/accounts/{profile.account_name}/deployments")
    client = http or httpx.Client(timeout=30)
    try:
        resp = client.get(url, params={"api-version": profile.mgmt_api_version},
                          headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise DeploymentListError(f"Azure Resource Manager nedostupný: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise DeploymentListError("Bez oprávnenia čítať nasadenia tohto resource (401/403). Inference kľúč nestačí – "
                                  "pridaj existujúce nasadenie ručne alebo požiadaj o rolu Reader.")
    if resp.status_code == 404:
        raise DeploymentListError("Resource sa nenašiel – skontroluj subscription, resource group a názov.")
    if resp.status_code >= 400:
        raise DeploymentListError(f"Azure Resource Manager vrátil chybu {resp.status_code}.")
    out = []
    for item in (resp.json() or {}).get("value", []):
        props = item.get("properties") or {}
        model = props.get("model") or {}
        out.append({"deployment": item.get("name", ""), "model": model.get("name", ""), "version": model.get("version", ""),
                    "sku": (item.get("sku") or {}).get("name", ""), "stav": props.get("provisioningState", ""),
                    "zdroj": "Azure Resource Manager", "overené": {}})
    return out


def deployment_label(d: dict[str, Any]) -> str:
    """Model/rodina, verzia a názov nasadenia (dve nasadenia toho istého modelu sú rozlíšiteľné)."""
    model = d.get("model") or "model neznámy"
    version = f" {d['version']}" if d.get("version") else ""
    return f"{model}{version} · nasadenie {d['deployment']}"


def test_connection(client: CloudLLM, capability: str = "text") -> dict[str, Any]:
    """Jedno malé ohraničené inference volanie (spotrebuje tokeny – GUI to vopred povie). capability „text“ overí
    iba odpoveď; „json_schema“ samostatne štruktúrovaný výstup. Úspech textu nepotvrdzuje JSON schému."""
    from pydantic import BaseModel

    class Ping(BaseModel):
        ok: bool

    start = time.monotonic()
    client.task = f"test_{capability}"
    try:
        if capability == "json_schema":
            client._mode = "json_schema"
            value = client.chat_json([{"role": "user", "content": 'Vráť JSON {"ok": true}.'}], Ping, retries=0,
                                     max_tokens=20)
            ok = bool(value.ok) and client.schema_mode == "json_schema"
        else:
            text = client.chat([{"role": "user", "content": "Odpovedz jedným slovom: OK"}], max_tokens=5)
            ok = bool(text.strip())
    except (LLMError, APITimeoutError) as exc:
        return {"ok": False, "schopnosť": capability, "nasadenie": client.deployment,
                "chyba": getattr(exc, "kind", type(exc).__name__) + ": " + str(exc)[:200]}
    info = client.last
    return {"ok": ok, "schopnosť": capability, "nasadenie": client.deployment, "latencia_s": round(time.monotonic() - start, 2),
            "tokeny": {"prompt": info.prompt_tokens, "výstup": info.completion_tokens},
            "kedy": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ======================================================================= nastavenia z GUI
def apply_cloud_settings(cloud: CloudConfig, settings: dict[str, Any]) -> CloudConfig:
    """Rozpočet a sadzby z GUI (prepíšu config.yaml); tajomstvá v nastaveniach nie sú."""
    new = cloud.model_copy(deep=True)
    for key in ("budget_total_eur", "budget_daily_eur", "budget_run_eur", "budget_since"):
        if key in settings and settings[key] is not None:
            setattr(new, key, settings[key])
    for dep, rate in (settings.get("rates") or {}).items():
        new.rates[dep] = CloudRate.model_validate(rate)
    return new


# ======================================================================= úpravy nastavení (GUI, bez tajomstiev)
SECRET_KEYS = ("api_key", "key", "secret", "token", "password")


def _models_settings(db) -> dict[str, Any]:
    from .models import MODEL_SETTINGS_KEY

    return db.get_setting(MODEL_SETTINGS_KEY, {}) or {}


def _save_models_settings(db, settings: dict[str, Any]) -> None:
    from .models import MODEL_SETTINGS_KEY

    db.set_setting(MODEL_SETTINGS_KEY, settings)


def save_profile(db, name: str, data: dict[str, Any]) -> ProviderProfile:
    """Profil poskytovateľa do nastavení: iba endpoint, názvy premenných prostredia a údaje o resource. Hodnota
    kľúča sa neprijme (ani omylom vložená do poľa)."""
    name = (name or "").strip()
    if not name or name == "local":
        raise ValueError("Zadaj názov profilu (iný než „local“).")
    if any(k in data for k in SECRET_KEYS):
        raise ValueError("Kľúč sa neukladá – zadaj iba názov premennej prostredia, v ktorej je.")
    for env_field in ("api_key_env", "mgmt_token_env"):
        value = str(data.get(env_field) or "")
        if value and not value.replace("_", "").isalnum():
            raise ValueError(f"{env_field} musí byť názov premennej prostredia (napr. AZURE_OPENAI_API_KEY), nie hodnota.")
    profile = ProviderProfile.model_validate(data)
    settings = _models_settings(db)
    settings.setdefault("providers", {})[name] = profile.model_dump()
    _save_models_settings(db, settings)
    return profile


def cloud_settings(db) -> dict[str, Any]:
    return db.get_setting(CLOUD_SETTINGS_KEY, {}) or {}


def save_cloud_settings(db, **fields: Any) -> dict[str, Any]:
    settings = cloud_settings(db)
    settings.update({k: v for k, v in fields.items() if v is not None})
    db.set_setting(CLOUD_SETTINGS_KEY, settings)
    return settings


def deployments(db, profile: str) -> list[dict[str, Any]]:
    return list((cloud_settings(db).get("deployments") or {}).get(profile) or [])


def deployment_version(db, profile: str, deployment: str) -> str | None:
    """Známa verzia modelu nasadenia (z načítania alebo ručného zadania) – súčasť kľúča cache."""
    try:
        for d in deployments(db, profile):
            if d["deployment"] == deployment:
                return f"{d.get('model') or ''}:{d.get('version') or ''}"
    except Exception:  # noqa: BLE001 – databáza bez nastavení
        return None
    return None


def _store_deployments(db, profile: str, items: list[dict[str, Any]]) -> None:
    settings = cloud_settings(db)
    settings.setdefault("deployments", {})[profile] = items
    db.set_setting(CLOUD_SETTINGS_KEY, settings)


def refresh_deployments(db, name: str, profile: ProviderProfile, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Načíta existujúce nasadenia (Azure Resource Manager); ručne pridané a overené schopnosti ostanú."""
    found = list_deployments(profile, http)
    old = {d["deployment"]: d for d in deployments(db, name)}
    merged = []
    for d in found:
        prev = old.pop(d["deployment"], None)
        merged.append({**d, "overené": (prev or {}).get("overené") or {}, "reasoning": (prev or {}).get("reasoning", False)})
    merged += [d for d in old.values() if d.get("zdroj") == "ručne"]
    _store_deployments(db, name, merged)
    return merged


def add_manual_deployment(db, profile: str, deployment: str, model: str = "", version: str = "",
                          reasoning: bool = False) -> dict[str, Any]:
    """Existujúce nasadenie zadané ručne (bez oprávnenia na správu resource). Kompatibilita nie je overená."""
    deployment = (deployment or "").strip()
    if not deployment:
        raise ValueError("Zadaj názov nasadenia (deployment name) z Azure.")
    items = [d for d in deployments(db, profile) if d["deployment"] != deployment]
    item = {"deployment": deployment, "model": model.strip(), "version": version.strip(), "sku": "", "stav": "neoverené",
            "zdroj": "ručne", "overené": {}, "reasoning": bool(reasoning)}
    _store_deployments(db, profile, items + [item])
    return item


def record_capability(db, profile: str, deployment: str, result: dict[str, Any]) -> None:
    """Uloží iba úspešne overenú schopnosť (neoverená kompatibilita sa nezapisuje ako overená)."""
    if not result.get("ok"):
        return
    items = deployments(db, profile)
    for d in items:
        if d["deployment"] == deployment:
            d.setdefault("overené", {})[result["schopnosť"]] = result.get("kedy")
    _store_deployments(db, profile, items)


def set_route(db, role: str, provider: str, deployment: str = "", reasoning: bool = False,
              json_mode: str = "json_schema") -> None:
    """Rola → poskytovateľ a nasadenie (prežije reštart; local = späť na lokálny model)."""
    settings = _models_settings(db)
    routes = settings.setdefault("routes", {})
    if provider == "local":
        routes.pop(role, None)
    else:
        if not deployment:
            raise ValueError("Vyber nasadenie pre cloudovú rolu.")
        routes[role] = {"provider": provider, "deployment": deployment, "reasoning": bool(reasoning), "json_mode": json_mode}
    _save_models_settings(db, settings)


def spend_summary(db, cloud: CloudConfig) -> dict[str, Any]:
    """Vlastný odhad spotreby (nie overený zostatok Azure): celkom od budget_since a dnes."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    total = db.cloud_spend(cloud.budget_since or "0000")
    day = db.cloud_spend(today)
    return {"celkom": total, "dnes": day, "rozpočet": cloud.budget_total_eur,
            "zostáva_v_rozpočte": round(cloud.budget_total_eur - total["settled"] - total["open"], 4)}


def cloud_routes(router) -> list[dict[str, Any]]:
    """Úlohy, ktoré by išli do cloudového modelu: rola, nasadenie a či ich podklady smú opustiť počítač."""
    if router.legacy or router.models is None:
        return []
    rows = []
    for task in sorted(router.models.tasks):
        route = router.route(task)
        role_cfg = router.role_config(route.role)
        if role_cfg.provider == "local":
            continue
        allowed = router.cloud_allowed(task)
        rows.append({"úloha": task, "rola": route.role, "poskytovateľ": role_cfg.provider,
                     "nasadenie": role_cfg.deployment or role_cfg.model,
                     "do cloudu": "áno" if allowed else "nie – osobné údaje, úloha čaká"})
    return rows
