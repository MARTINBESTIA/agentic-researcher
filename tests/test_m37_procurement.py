"""M37 – obstarávanie z TED (26. 10. 2026): štruktúrované oznámenia namiesto vyhľadávania stránok, podobné zákazky
k hypotéze, signály dopytu (kód) a stav obstarávania organizácie z konkrétneho záznamu. Offline (falošné API a
embeddingy podľa kľúčových slov)."""

from datetime import date, timedelta

import httpx
import numpy as np

from radar.config import Config, EmbeddingsConfig, ModelsConfig, PrivacyConfig
from radar.db import DB
from radar.embed import Embedder
from radar.procurement import (
    FIELDS, SYNC_KEY, TedClient, match_hypotheses, parse_notice, signals, signals_lines, stage_of, sync_notices,
)
from radar.util import Deadline, utcnow
from tests.test_m2_gui import gui  # noqa: F401 – fixture GUI

TODAY = date(2026, 10, 26)
KEYS = ("rozpo", "faktúr", "dochádz")


class KeywordEmbedder(Embedder):
    """Smer podľa kľúčového slova (rozpočty, faktúry, dochádzka); cache vektorov v DB ako skutočný Embedder."""

    def __init__(self, db):
        self.db, self.model, self.cfg, self.calls = db, "test-emb", None, 0

    def embed_texts(self, texts):
        self.calls += len(texts)
        rows = [np.array([1.0 if k in t.lower() else 0.0 for k in KEYS] + [0.2], dtype=np.float32) for t in texts]
        return np.vstack([r / np.linalg.norm(r) for r in rows])


def raw(pub, kind, title, buyer, ico, published, cpv="72000000", deadline=None, estimated=None, total=None,
        winners=None, winner_ids=None, desc="Predmetom zákazky je dodávka a podpora systému."):
    out = {"publication-number": pub, "notice-type": kind, "procedure-type": "open", "contract-nature": ["services"],
           "title-proc": {"slk": title}, "description-proc": {"slk": desc}, "buyer-name": {"slk": [buyer]},
           "buyer-identifier": [ico, "2020000000"], "buyer-legal-type": ["la"], "classification-cpv": [cpv, cpv],
           "main-classification-proc": [cpv], "publication-date": f"{published}+02:00",
           "links": {"html": {"SLK": f"https://ted.europa.eu/sk/notice/-/detail/{pub}"}}}
    if deadline:
        out["deadline-receipt-tender-date-lot"] = [f"{deadline}+01:00"]
    if estimated is not None:
        out["estimated-value-proc"], out["estimated-value-cur-proc"] = f"{estimated:.2f}", "EUR"
    if total is not None:
        out["total-value"], out["total-value-cur"] = total, ["EUR"]
    if winners:
        out["winner-name"], out["winner-identifier"], out["winner-size"] = {"slk": winners}, winner_ids or [], ["small"]
    return out


NOTICES = [
    raw("100001-2026", "cn-standard", "Rozpočtový informačný systém mesta", "Mesto Testovo", "00312345", "2026-10-01",
        deadline="2026-11-20", estimated=120000, total=120000),
    raw("200002-2024", "can-standard", "Rozpočtový modul – podpora a rozvoj", "Mesto Testovo", "312345", "2024-03-10",
        total=98000, winners=["Alfa Systémy s.r.o."], winner_ids=["45678901"]),
    raw("300003-2025", "can-standard", "Softvér na rozpočet obce", "Obec Malá", "00398765", "2025-05-05", cpv="48000000",
        estimated=15000),
    raw("400004-2026", "pin-only", "Rozpočtové plánovanie kraja", "Kraj Stredný", "37861234", "2026-09-01"),
    raw("500005-2025", "can-standard", "Dochádzkový systém nemocnice", "Nemocnica Iná", "00611111", "2025-01-15",
        total=50000),
]


def _cfg(tmp_path, **kw):
    return Config(db_path=str(tmp_path / "data" / "r.sqlite"), models=ModelsConfig(),
                  embeddings=EmbeddingsConfig(enabled=False), privacy=PrivacyConfig(lia_completed=True), **kw)


def _org(db, hid, name, ico):
    org, _ = db.upsert_organization(name, name.lower(), "evidence", ico=ico, sector_kind="public")
    db.link_org_hypothesis(org, hid, "segment_match")
    return org


def test_parse_notice_keeps_organisations_only_and_masks_contacts():
    pc = Config().procurement
    call = parse_notice(raw("100001-2026", "cn-standard", "Rozpočtový systém", "Mesto Testovo", "312345", "2026-10-01",
                            deadline="2026-11-10", estimated=120000, total=120000,
                            desc="Kontakt: jana.nova@testovo.sk, tel. +421 905 123 456. Dodávka systému."), pc)
    call_lots = parse_notice({**raw("1-2026", "cn-standard", "X", "B", "00312345", "2026-10-01"),
                              "deadline-receipt-tender-date-lot": ["2026-11-20+01:00", "2026-11-10+01:00"]}, pc)
    assert call["stage"] == "call" and call["buyer_ico"] == "00312345" and call["deadline"] == "2026-11-10"
    assert call_lots["deadline"] == "2026-11-20"  # najneskoršia lehota časti zákazky
    assert call["estimated_value"] == 120000.0 and call["awarded_value"] is None  # vo výzve nie je zadaná hodnota
    assert call["cpv_json"] == ["72000000"] and call["main_cpv"] == "72000000" and call["publication_date"] == "2026-10-01"
    assert "jana.nova" not in call["description"] and "905" not in call["description"]  # maskované ako v logoch
    assert "Dodávka systému" in call["description"]
    assert call["url"] == "https://ted.europa.eu/sk/notice/-/detail/100001-2026" and call["raw_hash"]
    award = parse_notice(raw("2-2025", "can-standard", "Y", "B", "00312345", "2025-01-01", total=98000,
                             winners=["Alfa Systémy s.r.o.", "Ján Testovací"], winner_ids=["45678901", "2023456789", "51234567"]),
                         Config().procurement)
    assert award["stage"] == "awarded" and award["awarded_value"] == 98000.0 and award["awarded_currency"] == "EUR"
    assert award["winners_json"] == {"names": ["Alfa Systémy s.r.o."], "ico": ["45678901", "51234567"], "size": ["small"]}
    assert [stage_of(t) for t in ("pin-only", "pin-cfc-standard", "can-modif", "veat", "corr", "cn-social")] == [
        "planned", "call", "modified", "awarded", "other", "call"]
    assert not {f for f in FIELDS if any(x in f for x in ("email", "person", "tel", "street", "post-code"))}


def test_sync_iterates_pages_is_idempotent_and_moves_cursor(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    bodies = []
    pages = [{"notices": NOTICES[:2], "iterationNextToken": "T1"}, {"notices": NOTICES[2:3], "iterationNextToken": None}]

    def post(url, body):
        bodies.append(body)
        return pages[(len(bodies) - 1) % 2]

    client = TedClient(cfg.procurement, post=post, sleep=lambda s: None)
    out = sync_notices(cfg, db, client, Deadline(5), today=TODAY)
    assert out["nové"] == 3 and out["strany"] == 2 and out["celkom"] == 3
    q = bodies[0]["query"]
    assert "buyer-country=SVK" in q and "classification-cpv IN (72000000 48000000 79000000)" in q
    assert f"publication-date>={TODAY - timedelta(days=365 * 3):%Y%m%d}" in q
    assert bodies[0]["paginationMode"] == "ITERATION" and bodies[1]["iterationNextToken"] == "T1"
    assert db.get_setting(SYNC_KEY)["do"] == "2026-10-01"
    again = sync_notices(cfg, db, client, Deadline(5), today=TODAY)
    assert again.get("nové", 0) == 0 and again.get("zmenené", 0) == 0 and again["celkom"] == 3
    assert "publication-date>=20260917" in bodies[2]["query"]  # od posledného dátumu mínus prekryv 14 dní

    def down(url, body):
        raise httpx.ConnectError("reset")

    before = db.get_setting(SYNC_KEY)
    failed = sync_notices(cfg, db, TedClient(cfg.procurement, post=down, sleep=lambda s: None), Deadline(5), today=TODAY)
    assert "chyba" in failed and db.get_setting(SYNC_KEY) == before  # posun sa nezapíše


def _world(tmp_path):
    from radar.recipients import set_procurement

    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Obce nevidia plnenie rozpočtov", "Obce ručne zostavujú prehľady rozpočtu.", "obce")
    db.set_people_selection(hid, True)
    db.upsert_procurement_notices([parse_notice(n, cfg.procurement) for n in NOTICES])
    city = _org(db, hid, "Mesto Testovo", "00312345")
    village = _org(db, hid, "Obec Malá", "00398765")
    db.set_org_hypothesis(village, hid, procurement_status="candidate", procurement_url="https://www.uvo.gov.sk/x",
                          procurement_note="Možné prebiehajúce obstarávanie z vyhľadávania")
    region = _org(db, hid, "Kraj Stredný", "37861234")
    set_procurement(db, region, hid, "none", "overené na webe kraja")
    return cfg, db, hid, city, village, region


def test_matches_signals_and_org_status_come_from_records(tmp_path):
    cfg, db, hid, city, village, region = _world(tmp_path)
    emb = KeywordEmbedder(db)
    out = match_hypotheses(cfg, db, emb, Deadline(5), today=TODAY)
    assert out["hypotézy"] == 1 and out["zhody"] == 4 and out["nové_vektory"] == 5  # dochádzka nie je podobná
    oh = db.org_hypothesis(city, hid)
    assert oh["procurement_status"] == "confirmed" and oh["procurement_active"] == 1
    assert oh["procurement_url"].endswith("100001-2026") and oh["procurement_date"] == "2026-11-20"
    assert "[TED 2026-10-26: otvorená výzva TED 100001-2026" in oh["procurement_note"]
    assert db.org_hypothesis(village, hid)["procurement_status"] == "candidate"  # odhad z vyhľadávania ostal
    assert db.org_hypothesis(region, hid)["procurement_status"] == "none"  # rozhodnutie človeka sa neprepíše
    assert out["obstarávanie_ponechaný_odhad"] == 1 and out["obstarávanie_overil_človek"] == 1

    sig = signals(cfg, db, hid, today=TODAY)
    assert sig["zákazky"] == 4 and sig["kupujúci"] == 3
    assert sig["opakovaní_kupujúci"] == [{"kupujúci": "Mesto Testovo", "ico": "00312345", "zákazky": 2,
                                          "roky": ["2024", "2026"]}]
    assert [o["oznámenie"] for o in sig["otvorené_teraz"]] == ["100001-2026"]
    assert [o["oznámenie"] for o in sig["plánované"]] == ["400004-2026"]
    assert sig["podľa_cpv"] == {"IT služby": 3, "softvér": 1}
    assert sig["hodnoty_eur"]["odhad_spolu"] == 135000 and sig["hodnoty_eur"]["zadané_spolu"] == 98000
    assert sig["menší_kupujúci"] == [{"kupujúci": "Obec Malá", "ico": "00398765", "hodnota_eur": 15000}]
    text = "\n".join(signals_lines(sig))
    assert "Mesto Testovo (2×, 2024/2026)" in text and "⚠ Otvorené teraz" in text and "skôr služby" in text

    calls = emb.calls
    again = match_hypotheses(cfg, db, emb, Deadline(5), today=TODAY)
    assert again["bez_zmeny"] == 1 and emb.calls == calls  # nič nové → bez nových vektorov
    # výzva po lehote: stav z TED sa aktualizuje na ukončené (záznam TED, nie rozhodnutie človeka)
    from radar.procurement import update_org_status

    update_org_status(cfg, db, hid, today=date(2026, 12, 1))
    assert db.org_hypothesis(city, hid)["procurement_status"] == "closed"
    assert db.org_hypothesis(city, hid)["procurement_note"].count("[TED") == 1


def test_no_embedder_or_no_notices_is_a_reported_skip(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    hid = db.create_hypothesis("Obce nevidia plnenie rozpočtov", "", "obce")
    db.set_people_selection(hid, True)
    assert "embeddingy vypnuté" in match_hypotheses(cfg, db, None, Deadline(1))["preskočené"]
    assert "procurement-sync" in match_hypotheses(cfg, db, KeywordEmbedder(db), Deadline(1))["preskočené"]
    assert signals(cfg, db, hid) == {"zákazky": 0, "porovnané": False, "prah": cfg.procurement.match_threshold}
    assert "zatiaľ neporovnané" in signals_lines(signals(cfg, db, hid))[0]


def test_autopilot_plans_procurement_weekly_and_for_new_hypotheses(tmp_path):
    from radar.autopilot import DISCOVERY, pending_work

    cfg, db, hid, *_ = _world(tmp_path)
    assert "procurement" in dict(DISCOVERY)
    assert "stiahnuté nikdy" in pending_work(cfg, db)["procurement"]
    db.set_setting(SYNC_KEY, {"do": "2026-10-01", "kedy": utcnow()})
    cfg.embeddings.enabled = True
    assert "1 hypotéz bez porovnania" in pending_work(cfg, db)["procurement"]
    match_hypotheses(cfg, db, KeywordEmbedder(db), Deadline(5), today=TODAY)
    assert "procurement" not in pending_work(cfg, db)


def test_problems_page_shows_procurement_tab_and_brief_row(gui):
    from streamlit.testing.v1 import AppTest

    from radar.config import load_config
    from radar.problem_brief import problem_brief
    from tests.test_m2_gui import APP

    cfg = load_config(gui / "config.yaml")
    db = DB(cfg.db_path)
    db.upsert_procurement_notices([parse_notice(raw("700007-2026", "cn-standard", "Systém na obstarávanie pre obce",
                                                    "Obec Fiktívna", "00700007", "2026-10-01", deadline="2099-01-01"),
                                                cfg.procurement)])
    nid = db.procurement_notices()[0]["id"]
    db.set_procurement_matches(1, [(nid, 0.71)], "h")
    assert "| obstarávanie (TED) | 1 podobných zákaziek u 1 kupujúcich" in problem_brief(cfg, db, 1)
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/problems.py").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Obstarávanie (TED): 1 podobných zákaziek u 1 kupujúcich" in text and "⚠ Otvorené teraz" in text
    d = DB(cfg.db_path)
    assert d.conn.execute("SELECT COUNT(*) FROM model_judgments").fetchone()[0] == 0
    d.close()
