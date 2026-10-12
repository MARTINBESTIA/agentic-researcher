# Akceptačné kritériá v0.2 → testy

Všetky testy bežia offline (falošný model, vyhľadávanie a web): `.venv\Scripts\python -m pytest -q`.
Stav 12. 10. 2026: 167 testov prechádza (30 pôvodných z v0.1 bez úprav; test migrácie overuje schému v6).

| Míľnik | AK | Test |
|---|---|---|
| M1 | 1 migrácia v0.1 → v2, záloha, druhé spustenie nič nezmení | `test_m1_foundation.py::test_v01_database_is_migrated_with_backup` |
| M1 | 2 bez `models` všetko na `llm`, testy v0.1 bez úprav | `test_without_models_section_everything_goes_to_llm` + `test_pipeline.py`, `test_features.py`, `test_verify.py` |
| M1 | 3 balanced: extract na fast, entail na accurate | `test_balanced_profile_routes_extract_to_fast_and_entail_to_accurate` |
| M1 | 4 orezaný výstup → undecided + kontrola, beh pokračuje | `test_truncated_output_goes_to_review_and_run_continues` |
| M1 | 5 každé volanie má riadok v model_judgments s verziou promptu | `test_every_model_call_is_recorded_with_prompt_version` |
| M1 | 6 log bez e-mailu a telefónu | `test_log_does_not_contain_email_or_phone` |
| M2 | 1 GUI iba 127.0.0.1, bez telemetrie (príkaz aj config.toml) | `test_m2_gui.py::test_gui_listens_only_on_localhost_without_telemetry` |
| M2 | 2 každá stránka sa načíta (AppTest) | `test_every_page_loads[...]` (11 stránok) |
| M2 | 3 označenie v Kontrole → label, položka uzavretá | `test_review_label_creates_gold_label_and_closes_item` |
| M2 | 4 `<script>` zo zdroja sa zobrazí escapovaný | `test_source_text_with_script_is_escaped` |
| M3 | 1 hlasovanie 3:0, 2:1 (príznak), 1:1:1 / undecided → kontrola | `test_m3_accuracy.py::test_self_consistency_votes` |
| M3 | 2 eval na 20 labeloch = ručný výpočet P/R/F1 a intervalu | `test_eval_with_20_labels_computes_metrics_like_manual`, `test_wilson_matches_manual_formula` |
| M3 | 3 < 50 labelov → „nedostatok dát“ | `test_recommendation_needs_50_labels_then_picks_cheapest_overlapping` |
| M3 | 4 testy v0.1 prechádzajú | celá sada |
| M4 | 1 RPO fixture: názov, IČO, právna forma, činnosť; bez adresy štatutára | `test_m4_orgs.py::test_rpo_fixture_maps_fields_without_statutory_address`, `test_sanitize_keeps_only_name_and_function` |
| M4 | 2 organizácia z dôkazu iba pri názve v citáte/okne | `test_org_from_evidence_only_when_name_in_quote_or_window` |
| M4 | 3 web overený iba s IČO alebo presným názvom | `test_website_verified_only_with_ico_or_exact_name` |
| M4 | 4 `exclude_orgs` sa neobjaví | `test_excluded_org_never_appears` |
| M5 | 1 e-mail z mailto: aj z textu s doslovným úryvkom | `test_m5_people.py::test_email_from_mailto_and_text_with_literal_snippet` |
| M5 | 2 vzor meno.priezvisko@ → štvrtej osobe sa e-mail nevytvorí | `test_pattern_does_not_create_email_for_fourth_person` |
| M5 | 3 (at) a Cloudflare → hidden_contact, nikdy dekódované | `test_obfuscated_and_cloudflare_become_hidden_contact_never_decoded` |
| M5 | 4 telefóny v E.164 | `test_phone_formats_are_stored_as_e164` |
| M5 | 5 dve osoby v tabuľke podľa riadku; kontakt mimo bloku generický | `test_two_people_in_table_get_contacts_by_row_and_outside_contact_is_generic` |
| M5 | 6 fetcher odmietne linkedin.com aj presmerovanie | `test_fetcher_refuses_linkedin_and_redirect_to_it` |
| M5 | 7 `people` bez LIA → zrozumiteľná chyba | `test_people_without_lia_fails_with_clear_error` |
| M5 | 8 `forget --email` + nový beh osobu neuloží | `test_forget_email_deletes_and_suppresses_on_next_run` |
| M5 | 9 purge s posunutým časom (90 dní, declined, exporty) | `test_purge_with_shifted_time` |
| M5 | 10 ≤ 15 na hypotézu, ≤ 2 na organizáciu, zložky priority | `test_limits_15_per_hypothesis_2_per_org_and_priority_components` |
| M5 | 11 v `radar/` nie je smtplib ani klient sociálnych sietí | `test_no_smtp_or_social_clients_in_code` |
| M5 | 12 adresa z RPO sa nikde neuloží | `test_rpo_address_never_stored` |
| M6 | 1 dĺžky e-mailu, predmetu, LinkedIn ≤ 200 | `test_m6_outreach.py::test_lengths_email_and_linkedin` |
| M6 | 2 zakázaná fráza, číslo mimo faktov, chýbajúca pätička → needs_edit | `test_forbidden_phrase_foreign_number_or_missing_footer_needs_edit` |
| M6 | 3 bez gender_hint „Dobrý deň,“ | `test_greeting_only_from_verified_gender` |
| M6 | 4 placeholder v profile odosielateľa blokuje schválenie | `test_approval_blocked_with_placeholder_sender` |
| M6 | 5 11. schválenie v jeden deň zablokované | `test_eleventh_approval_same_day_blocked` |
| M6 | 6 follow-up na 5. pracovný deň (cez víkend) | `test_follow_up_due_on_fifth_business_day_over_weekend` |
| M6 | 7 `objected` spustí forget | `test_objected_runs_forget` |
| M6 | 8 prebiehajúce obstarávanie → iba upozornenie | `test_active_procurement_gives_warning_not_draft` |
| M6 | 9 mailto URL-encoded | `test_mailto_is_url_encoded` |
| M7 | 1 citát z rozhovoru mimo poznámok zamietnutý | `test_m7_gate0.py::test_interview_quote_not_in_notes_is_rejected` |
| M7 | 2 pass / chýba platiteľ / nedostatok dát | `test_gate0_verdicts` |
| M7 | 3 experti sa nepočítajú | `test_fail_when_too_few_unprompted_and_experts_not_counted` |
| M7 | 4 správa a export bez mien a kontaktov | `test_report_and_export_without_personal_data` |
| M8 | 1 rozpočet 10, podiel 0,3 → ≥ 3 z objavovania | `test_m8_discovery.py::test_budget_gives_discovery_at_least_30_percent`, `test_run_all_order.py` |
| M8 | 2 bandit opakovateľný, každá kategória ≥ 5 % | `test_bandit_is_repeatable_and_respects_minimum_share` |
| M8 | 3 podobné → existujúca, odlišné → candidate (discovery), povýšenie po 2 tvrdeniach z 2 domén | `test_novelty_candidate_and_promotion_after_two_domains` |
| M8 | 4 screen = nie → bez extrakcie | `test_screen_negative_document_is_not_extracted` |
| M8 | 5 rotácia matice pokračuje | `test_matrix_rotation_continues_where_last_run_ended` |
| – | run-all: poradie fáz podľa modelov, LIA brána, pokračovanie po chybe | `test_run_all_order.py` |
| M9 | 1 fronta testu pokryje všetky hypotézy (aj zamietnuté, tie posledné) | `test_m9_agentic.py::test_validation_queue_covers_all_hypotheses_in_priority_order` |
| M9 | 2 agent testu ukladá tvrdenia s hypothesis_hint, zaradenie rozhodne presný model, protidôkaz nezaloží hypotézu, verdikt kódu | `test_validate_search_records_hinted_claims_and_cluster_decides`, `test_validate_finish_waits_for_pending_claims_and_verdicts` |
| M9 | 3 výpadok modelu nechá hypotézu vo fronte | `test_model_down_leaves_hypothesis_in_queue` |
| M9 | 4 scout: tvrdenia ako objavovanie → candidate, rotácia buniek | `test_scout_claims_become_discovery_candidates_and_cells_rotate` |
| M9 | 5 workflowy v poradí podľa modelov, stav úlohy, zastavenie pri chybe | `test_workflow_runs_stages_by_model_and_records_job`, `test_people_workflow_selects_only_given_hypotheses` |
| M9 | 6 úloha z GUI: jedna naraz, príkaz CLI, prerušenie, zastavenie, maskovaný log | `test_jobs_start_once_run_on_localhost_cli_and_stop` |
| M9 | 7 LinkedIn z webu organizácie s doslovným úryvkom, firma /company/, číslice v URL nie sú telefón | `test_linkedin_from_org_page_is_assigned_with_literal_snippet_and_never_a_phone`, `test_normalize_profile_url_and_url_digits` |
| M9 | 8 LinkedIn z vyhľadávania pre všetkých vybraných ľudí (meno + organizácia), ručné zadanie iba /in/ | `test_search_profiles_for_all_selected_people_with_name_and_org_check` |
| M9 | 9 GUI: tlačidlá v prehľade, Problémy ukazujú všetky hypotézy, prehľad ľudí so značkami a LinkedIn | `test_m2_gui.py::test_dashboard_has_agentic_buttons_and_problems_show_all`, `test_people_overview_shows_linkedin_and_markers` |
| M9 | 10 dávka testu posúdi a zaradí iba svoje tvrdenia (zásobník čaká) | `test_validation_batch_verifies_and_clusters_only_its_own_claims` |
| M9 | 11 čísla v prehľade sa počas úlohy obnovujú, vrátane čakajúcich na overenie | `test_m2_gui.py::test_overview_numbers_refresh_while_job_runs` |
| M9 | 12 Správca akcií: všetky akcie, model v GPU, počas behu „do poradia“ a „zastaviť a spustiť“, zrušenie z poradia | `test_m2_gui.py::test_action_manager_lists_all_actions_queue_and_switch` |
| M9 | 13 každá akcia spúšťa svoje kroky v poradí podľa modelov; po skončení sa spustí ďalšia z poradia | `test_m9_agentic.py::test_every_action_runs_its_stages_in_model_order` |
| M9 | 14 poradie, prepnutie (replace), zrušenie, zastaviť všetko | `test_queue_replace_and_cancel` |
| M9 | 15 stav GPU a uvoľnenie cez Ollama API (falošný server), nedostupný server → „nedostupné“ | `test_gpu_status_and_unload_with_fake_ollama` |
| M10 | 1 kontakt ku konkrétnej osobe iba z jej bloku (nie susedov, nie info@) | `test_m10_gates.py::test_contacts_of_name_takes_only_the_persons_own_contacts` |
| M10 | 2 kontakty iba z webu organizácie a úradných domén; potlačenie platí | `test_person_contacts_from_org_site_only_and_respect_suppression` |
| M10 | 3 výber modelov z GUI prepíše config.yaml pre nové routery (rola agent, profil) | `test_gui_model_selection_overrides_config_for_new_routers`, `test_m2_gui.py::test_model_selection_in_action_manager_saves_override` |
| M11 | 1 polia presným modelom, ručné úpravy sa neprepíšu, štart lehoty | `test_fields_are_defined_and_human_edits_kept` |
| M11 | 2 licencia a frekvencia iba zo stránky; kontrola nájde súbory, kvalita, stav poľa | `test_source_agent_keeps_license_only_if_on_page_and_check_finds_files` |
| M12 | 1 DuckDB import (cp1250), profil, pokrytie podľa IČO, pôvodný súbor zmazaný | `test_datasets_go_to_duckdb_with_profile_coverage_and_limits` |
| M12 | 2 osobné údaje → neuložené (GDPR), limit veľkosti | `test_personal_data_dataset_is_not_kept_and_size_limit` |
| M12 | 3 SQL od modelu iba na čítanie, jeden SELECT | `test_sql_from_model_is_read_only_single_select` |
| M12 | 4 reprodukcia s mierkou, pripravenosť, stav „ready“ iba ako návrh | `test_reproduction_of_known_number_scale_and_readiness` |
| M12 | 5 návrh žiadosti 211/2000, príznak 6 týždňov, bez vyradenia | `test_missing_key_field_gets_info_request_draft_and_six_week_flag` |
| M13 | 1 spätný test (rolling origin, bootstrap) | `test_backtest_series_math` |
| M13 | 2 predregistrácia → uzamknutie → spätný test → demo bez čísel mimo faktov | `test_gate2_prereg_lock_backtest_and_demo`, `test_invalid_prereg_columns_cannot_be_locked` |
| M13 | 3 GUI Brány 1 (pripravenosť, príznaky, žiadosť) | `test_m2_gui.py::test_gate1_page_shows_readiness_fields_and_info_request` |
| M11 | 3 zdroje z prečítaných stránok, keď agent nič neuloží; pole „názov: popis“, citát s „…“ | `test_sources_from_read_pages_when_agent_only_reads`, `test_field_name_with_description_and_shortened_quote` |
| M12 | 6 API s popisom OpenAPI → JSON endpointy, vnorené IČO → pokrytie | `test_openapi_endpoints_become_json_datasets_with_nested_fields` |
| M13 | 4 dátumový stĺpec zoskupený po obdobiach (štvrťrok) | `test_backtest_groups_date_column_by_period` |
| M10 | 4 stránka osoby zo sitemap.xml bez vyhľadávača | `test_sitemap_finds_person_page_without_search_engine` |
| M9 | 16 test hypotézy: tvrdenia z prečítaných stránok, keď agent nič neuloží | `test_m9_agentic.py::test_validation_agent_that_only_reads_gets_claims_from_extraction` |
| M12 | 7 stránkovanie API (kurzor, offset, ignorované stránkovanie), náhrada jednostránkového datasetu | `test_api_pagination_cursor_offset_and_ignored_paging`, `test_paginated_api_replaces_single_page_dataset` |
| M12 | 8 ZIP (cesta `../`, zip bomba) a XML (záznamy → stĺpce, bez XXE) | `test_zip_dataset_is_unpacked_safely`, `test_xml_records_become_columns_without_external_entities` |
| M10 | 5 vyhľadávanie: zopakovanie pri blokovaní, bez úvodzoviek, pamäť dopytov | `test_search_retries_blocked_engines_relaxes_quotes_and_caches` |
| M9 | 17 typ vydavateľa: pravidlá, model raz na doménu, dodávatelia sa nepočítajú do sily | `test_m10_gates.py::test_publisher_types_rules_model_once_per_domain_and_vendor_not_counted` |
| M10 | kontakty osoby z PDF na webe organizácie (okná okolo mena, zdroj + úryvok) | `test_m10_gates.py::test_person_contact_from_pdf_phone_list` |
| M11 | široká tabuľka (obdobia ako stĺpce) → dlhý tvar, chýbajúce hodnoty NULL, časový rad Brány 2 | `test_m10_gates.py::test_wide_table_with_periods_as_columns_gets_long_form` |
| M11 | sezóna podľa obdobia, modely ses / holt_damped / seasonal_trend / auto (výber iba z histórie) | `test_m10_gates.py::test_backtest_season_length_and_new_models` |
| M11 | filtre predregistrácie (ukazovateľ, kraj) nad dlhou tabuľkou; neznámy model a stĺpec filtra sú chyby návrhu | `test_m10_gates.py::test_prereg_filters_select_one_indicator_of_long_table` |
| M9 | extrakcia z prečítaných stránok až po všetkých agentoch dávky (menej výmen modelu) | `test_m9_agentic.py::test_validation_extracts_after_all_agents_of_batch` |
| M11 | témy: average linkage (prah, najväčšia veľkosť) | `test_m11_themes.py::test_average_linkage_threshold_and_max_size` |
| M11 | témy: názov z modelu, zamietnuté a samostatné bez témy, stabilné ID a ručný názov medzi behmi | `test_m11_themes.py::test_themes_named_by_model_stable_ids_and_manual_name_kept` |
| M11 | témy: pri chybe modelu názov najtypickejšej hypotézy; bez embeddingov sa krok preskočí | `test_m11_themes.py::test_theme_name_falls_back_to_most_typical_hypothesis` |
| M11 | GUI: Problémy → Témy (súhrn dôkazov), filter podľa témy, premenovanie | `test_m2_gui.py::test_problems_theme_view_and_rename` |
