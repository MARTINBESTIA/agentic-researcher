# Akceptačné kritériá v0.2 → testy

Všetky testy bežia offline (falošný model, vyhľadávanie a web): `.venv\Scripts\python -m pytest -q`.
Stav 14. 10. 2026: 247 testov prechádza (30 pôvodných z v0.1 bez úprav; test migrácie overuje schému v9).

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
| M12 | 4 reprodukcia s mierkou, pripravenosť; skóre ≥ 0,5 → Brána 1 prešla automaticky s dôvodom | `test_reproduction_of_known_number_scale_and_readiness` |
| M12 | 4a skóre < 0,5 → „čaká na tvoje dáta“, ručné rozhodnutie sa neprepíše | `test_low_readiness_waits_for_user_and_manual_decision_wins` |
| M12 | 4b tvoje dáta: súbor do DuckDB, sudca po poliach, vhodnosť počíta kód, ≥ 0,5 → prešla | `test_user_data_is_imported_judged_per_field_and_scored_by_code` |
| M12 | 4c tvoje dáta s osobnými údajmi sa neuložia ani nepošlú modelu; zlyhanie modelu → vstup čaká; nízka vhodnosť → čaká | `test_user_input_with_personal_data_low_score_and_model_failure` |
| M12 | 4d autopilot: posúdenie vstupov pred Bránou 1, „čaká na dáta“ znova až po 3 dňoch | `test_autopilot_evaluates_user_inputs_and_retries_waiting_hypotheses_later` |
| M12 | 5 návrh žiadosti 211/2000, príznak 6 týždňov, bez vyradenia | `test_missing_key_field_gets_info_request_draft_and_six_week_flag` |
| M13 | 1 spätný test (rolling origin, bootstrap) | `test_backtest_series_math` |
| M13 | 2 predregistrácia → uzamknutie → spätný test → demo bez čísel mimo faktov | `test_gate2_prereg_lock_backtest_and_demo`, `test_invalid_prereg_columns_cannot_be_locked` |
| M13 | 3 GUI Brány 1 (pripravenosť, príznaky, žiadosť, „čaká na tvoje dáta“ a formulár vstupu) | `test_m2_gui.py::test_gate1_page_shows_readiness_fields_and_info_request` |
| M11 | 3 zdroje z prečítaných stránok, keď agent nič neuloží; pole „názov: popis“, citát s „…“ | `test_sources_from_read_pages_when_agent_only_reads`, `test_field_name_with_description_and_shortened_quote` |
| M12 | 6 API s popisom OpenAPI → JSON endpointy, vnorené IČO → pokrytie | `test_openapi_endpoints_become_json_datasets_with_nested_fields` |
| M13 | 4 dátumový stĺpec zoskupený po obdobiach (štvrťrok) | `test_backtest_groups_date_column_by_period` |
| M13 | 4a iba úplné obdobia v okne dát (CRZ), budúce a bežiace vyradené, prázdne obdobie = 0 pri súčte | `test_backtest_uses_only_complete_periods_inside_data_window` |
| M13 | 4e zoskupenie podľa okna dát (kód opraví rok → mesiac), model dostane okno, krátky rad sa nedá uzamknúť | `test_prereg_period_fits_data_window_and_lock_needs_enough_periods` |
| M13 | 4f postup experimentu krok po kroku (výber, návrh, SQL, kolá, výber auto, pravidlo s prahom) | `test_experiment_trace_explains_selection_prereg_data_folds_and_rule` |
| M13 | 4b organizácia podľa vlastnej strany zmluvy (objednávateľ), IČO s medzerami a predponou | `test_coverage_prefers_own_side_of_contract_and_dirty_ico` |
| M13 | 4c pokrytie sa obnoví pre organizácie pridané po stiahnutí dát | `test_finish_refreshes_coverage_for_organizations_added_later` |
| M13 | 4d GUI: náhľad časového radu pred uzamknutím predregistrácie | `test_m2_gui.py::test_gate2_page_previews_series_before_lock` |
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
| M9 | prerušená akcia: poradie stojí, pokračovať v poradí, spustiť znova s rovnakými parametrami | `test_m9_agentic.py::test_interrupted_job_stalls_queue_until_resumed_or_restarted` |
| M9 | GUI: Správca akcií ponúkne pokračovanie po prerušení | `test_m2_gui.py::test_action_manager_offers_resume_after_interruption` |
| M9 | `radar job --mode queue --wait`: úloha ako z GUI, čaká aj v poradí | `test_m9_agentic.py::test_job_command_queues_like_gui_and_waits_for_end` |
| M4 | istič registra: po 3 zlyhaniach sa register vynechá, po prestávke sa skúsi znova | `test_m4_orgs.py::test_registry_circuit_breaker_skips_unresponsive_register` |
| M10 | národný katalóg: kmene bez diakritiky, uvoľnenie dopytu, text metadát s licenciou, výpadok | `test_m10_gates.py::test_nkod_search_terms_metadata_text_and_failure` |
| M10 | kandidát z katalógu → zdroj (licencia a periodicita z metadát) → súbory z distribúcií → import podľa formátu z katalógu | `test_m10_gates.py::test_catalog_candidates_become_sources_with_files_and_typed_download` |
| M10 | licencia NC/ND nie je otvorená | `test_m10_gates.py::test_noncommercial_license_is_not_counted_as_open` |
| M9 | dlhé behy načítajú všetky moduly pri štarte (úpravy kódu počas behu nemiešajú verzie) | `test_m9_agentic.py::test_long_runs_preload_whole_package_at_start` |
| M4 | orgs v behu bez presného modelu; prepare-next najprv filtre segmentu, potom mapy rolí | `test_m4_orgs.py::test_run_without_model_filters_and_prepare_next_does_filters_first` |
| M4 | pokazený odkaz v HTML (napr. „http://[…“) nezahodí celú stránku kandidáta | `test_m4_orgs.py::test_malformed_link_on_page_does_not_drop_the_page` |
| M9 | zber ľudí: orgs najviac polovicu rýchlej fázy, people dostane zvyšok | `test_m9_agentic.py::test_people_workflow_leaves_time_for_people_after_orgs` |
| M4 | IČO kandidáta zo segmentu nájdené aj na podstránke O nás | `test_m4_orgs.py::test_segment_candidate_ico_found_on_about_page` |
| M4 | názov z textu sa porovnáva s aktuálnym názvom v RPO, nie s historickým | `test_m4_orgs.py::test_name_resolution_uses_current_rpo_name_not_last_listed` |
| M5 | chyba poradia jednej hypotézy nezastaví ostatné | `test_m5_people.py::test_people_rank_error_in_one_hypothesis_does_not_stop_others` |
| M5 | GUI Ľudia ukazuje aj zozbieraných, ešte nezoradených ľudí | `test_m2_gui.py::test_people_overview_shows_collected_but_unranked_people` |
| M1 | config.local.yaml (mimo gitu) prepíše config.yaml po kľúčoch | `test_m1_foundation.py::test_local_config_overrides_without_git` |
| M6 | rodovo určená 1. osoba bez nastaveného rodu odosielateľa → na úpravu | `test_m6_outreach.py::test_gendered_first_person_needs_edit_unless_sender_gender_set` |
| M10 | CRZ: denné dávky, zlúčenie podľa ID, bez mien dodávateľov a adries, pokrytie podľa IČO objednávateľa | `test_m10_gates.py::test_crz_daily_exports_deduplicated_without_supplier_names` |
| M10 | CLI kroky brán s --hypotheses | `test_m10_gates.py::test_gate_stage_cli_accepts_hypotheses` |
| M10 | CRZ: riadiaci znak neplatný v XML nezahodí dávku | `test_m10_gates.py::test_crz_batch_with_control_character_is_not_dropped` |
| M10 | CRZ: doplnenie chýbajúcich dní pridá iba nové zmluvy | `test_m10_gates.py::test_crz_append_missing_days_adds_only_new_contracts` |
| M8 | rotácia zdrojov s vyhľadávaním v kategórii, RSS vždy | `test_m8_discovery.py::test_sources_rotate_within_category_and_rss_always_used` |
| M8 | TLS cez úložisko OS s overovaním | `test_m8_discovery.py::test_tls_uses_os_trust_store_with_verification_on` |
| M8 | GUI Zdroje: katalóg zdrojov objavovania s výťažnosťou podľa kategórie a domény | `test_m2_gui.py::test_every_page_loads[sources]` |
| M9 | autopilot: kolo iba z akcií s prácou, úseky do zvyšného času, zber ľudí iba pre zapnuté hypotézy, ďalšie kolo | `test_m9_agentic.py::test_autopilot_plans_round_by_work_and_time_and_continues` |
| M9 | autopilot číta svoj koniec a naplánuje kolo | `test_m9_agentic.py::test_autopilot_job_reads_its_end_time_and_plans` |
| M9 | ostré spustenie úlohy s konfiguráciou inej databázy sa odmietne | `test_m9_agentic.py::test_real_spawn_refuses_config_of_another_database` |
| M10 | vopred povolené „prešla“ iba pri stave pripravená a iba pre zadanú hypotézu | `test_m10_gates.py::test_reproduction_of_known_number_scale_and_readiness` |
| M1 | rola sudca: posúdenie dôkazov ostáva na sudcovi, keď sa presný model zmení | `test_m1_foundation.py::test_judge_role_keeps_evidence_tasks_on_its_model_when_accurate_changes` |
| M10 | XLSX bez prípony sa načíta, poškodený súbor je chyba datasetu | `test_m10_gates.py::test_xlsx_without_extension_imports_and_bad_file_is_dataset_error` |
| M2 | GUI Dokumenty: zoznam stiahnutých dokumentov s počtom citátov, detail s textom a citátmi | `test_m2_gui.py::test_documents_page_lists_documents_with_claims` |
| M9 | kontrola citátov kódom beží aj po vyčerpaní rýchlej fázy | `test_m9_agentic.py::test_code_only_checks_run_even_when_fast_phase_ran_out` |
| M14 | 1 karta: iba citáty hypotézy, sudca na kľúčových poliach, rozdelenie (presun citátov, dedenie Brány 1, zdieľaná DuckDB) | `test_m14_experiments.py::test_card_cites_own_claims_checks_key_fields_and_splits_hypothesis` |
| M14 | 2 operácie: prepojenie záznamov, história bez toho istého dňa, budúcnosť ako štítok, únik budúcich dát odmietnutý, zakázané výrazy | `test_workspace_links_records_and_keeps_history_and_future_apart` |
| M14 | 3 návrh modelom: skúšobný beh, oprava po chybách, chybný návrh sa nedá uzamknúť | `test_plan_dry_run_repair_and_lock` |
| M14 | 4 poradie: modely proti náhodnému výberu, najväčším, miere entity; všetky metriky; dôležitosť príznakov | `test_ranking_beats_random_and_value_with_full_scorecard` |
| M14 | 5 predpoveď (spoločný model, ETS, 4 naivné odhady × 5 metrík) a anomália nájde vložený skok | `test_forecast_pooled_and_anomaly_flags_spike` |
| M14 | 6 prah: hromadenie pod 50 000 (prah z citátu), poradie entít; porovnanie v kategórii s liftom | `test_threshold_bunching_and_comparison_lift` |
| M14 | 7 záver iba z faktov (vymyslené číslo → záver z kódu), správa HTML | `test_conclusion_from_facts_rejects_invented_numbers_and_writes_report` |
| M14 | 8 CRZ dozadu po blokoch, chýbajúce dni | `test_m10_gates.py::test_crz_window_extends_backwards_in_chunks_and_records_missing_days` |
| M14 | 9 GUI: karta, čomu experiment slúži, výsledková tabuľka, záver | `test_m2_gui.py::test_gate2_page_shows_card_scorecard_items_and_conclusion` |
| M14 | 10 `lookup`: iná tabuľka podľa IČO a časového okna, budúcnosť iba štítok | `test_m14_experiments.py::test_lookup_joins_other_table_by_ico_within_time_window` |
| M14 | 11 NKÚ: zistenia s IČO bez adries, nový dataset zdieľaný s podhypotézou | `test_nku_findings_get_ico_without_addresses_and_dataset_is_shared_with_subhypotheses` |
| M14 | 12 kvalita návrhov a voliteľné automatické uzamknutie | `test_plan_quality_is_measured_and_optional_auto_lock` |
| M14 | 13 CRZ: výpadok siete neposunie okno, `--retry` | `test_m10_gates.py::test_crz_window_does_not_move_over_network_outage_and_retries_missing_days` |
| M14 | 14 reprodukcia bez cieľa a pripravenosť bez tejto zložky | `test_m10_gates.py::test_repro_target_skips_years_and_dates_and_readiness_ignores_missing_target` |
| M14 | 15 kandidáti z katalógu podľa významu poľa | `test_m10_gates.py::test_catalog_candidates_ranked_by_meaning_of_field` |
| M14 | 16 odhady časov z histórie a poradia | `test_m9_agentic.py::test_dashboard_eta_from_history_and_queue` |
| M15 | 1 dosiér pseudonymizovaný: všetky časti, ľudia rodiča, dáta Parquet, SHA-256, žiadne meno ani osobný kontakt | `test_m15_dossier.py::test_pseudonymized_dossier_has_everything_but_no_personal_data` |
| M15 | 2 dosiér s menami a kontaktmi, záznam do poznámok | `test_personal_dossier_includes_contacts_and_is_recorded` |
| M15 | 3 dáta celé / vzorka / bez dát, bez súvisiacich hypotéz | `test_data_modes_and_related_switch` |
| M15 | 4 GUI: Problémy → Export všetkého vytvorí ZIP | `test_m2_gui.py::test_problems_page_creates_pseudonymized_dossier` |
| M16 | 1 Brána 1: beh vezme s modelmi toľko hypotéz, koľko stihne (rozpracované, nové, opakované, prešlé) | `test_m10_gates.py::test_gate1_plan_prefers_new_hypotheses_and_fits_the_limit` |
| M16 | 2 kontrola a sťahovanie dostanú čas, aj keď agent minie celý svoj úsek | `test_m10_gates.py::test_gate1_check_and_downloads_keep_their_time_when_agent_uses_all` |
| M16 | 3 Brána 1 iba nezačaté a rozpracované; „Prehľadať znova“ | `test_m10_gates.py::test_autopilot_evaluates_user_inputs_and_researches_waiting_hypotheses_only_after_reopen` |
| M17 | 1 blokujúci vyhľadávač sa vynechá, prázdne hľadania sa počítajú | `test_m10_gates.py::test_search_skips_blocking_engines_for_a_while_and_counts_empty_searches` |
| M17 | 2 po prerušení pokračuje poradie a prerušené úlohy | `test_m9_agentic.py::test_scheduled_start_resumes_stalled_queue_and_interrupted_jobs` |
| M17 | 3 lokálny index katalógu: inkrementálne, séria = jeden vektor, hľadanie podľa významu | `test_m10_gates.py::test_local_catalog_index_syncs_incrementally_and_finds_by_meaning` |
| M17 | 4 Témy: Brána 0 za tému, výber celej témy | `test_m2_gui.py::test_theme_view_shows_gate0_summary_and_selects_whole_theme` |
| M17 | 5 kandidátske citáty pri read_page | `test_m9_agentic.py::test_read_page_offers_candidate_quotes_for_reading_only_models` |
| M17 | 6 GDPR: uloženie bez osobných stĺpcov po posúdení | `test_m10_gates.py::test_personal_dataset_kept_without_personal_columns_after_human_gdpr_decision` |
| M17 | 7 časové okno akcií autopilota | `test_m9_agentic.py::test_autopilot_action_hours_window` |
| M17 | 8 eval iba pri dosť labeloch | `test_m3_accuracy.py::test_eval_all_runs_only_tasks_with_enough_gold_labels` |
| M17 | 9 čas výmeny modelu | `test_features.py::test_model_swap_load_time_is_measured_and_kept` |
| M17 | 10 lokálny index webu organizácie | `test_m10_gates.py::test_domain_index_follows_nested_sitemaps_and_is_cached` |
| M18 | 1 plán kontaktov (rýchly model): ciele zákazník / držiteľ dát / expert, dopyty bez e-mailov a telefónov; web overený cez IČO; katalóg firiem preskočený; expertná doména (NKÚ) pri cieli „expert“ | `test_m18_contacts.py::test_plan_queries_are_cleaned_and_orgs_verified_by_ico` |
| M18 | 2 zákazník s nezhodou segmentu sa nezaradí | `test_customer_with_segment_mismatch_is_not_linked` |
| M18 | 3 ďalšie kolo dopytov až po kroku people, so spätnou väzbou (čo sa už hľadalo a našlo) | `test_next_round_only_after_people_step_and_with_feedback` |
| M18 | 4 držiteľ dát z Brány 1 a kontaktný bod datasetu z katalógu (meno a e-mail doslova v úryvku, dátum metadát, cache) | `test_data_holder_from_gate1_source_and_catalog_contact_point` |
| M18 | 5 poradie: zákazníci, držitelia dát a experti zvlášť; ručne pridaný ostáva; nevybraný sa vymaže | `test_rank_keeps_groups_separate_and_manual_person` |
| M18 | 6 ručné pridanie zo stránky: meno a kontakt v tom istom bloku, cudzí e-mail sa neuloží, LinkedIn a bez LIA odmietnuté | `test_add_contact_from_url_verifies_name_and_contact_on_page` |
| M18 | 7 Koho osloviť: odporúčanie pravidlom s dôvodmi, obstarávanie blokuje, riadok organizácie s generickým e-mailom | `test_shortlist_tiers_reasons_and_org_rows` |
| M18 | 8 test „podložená“ zapne Bránu 0; ručné vypnutie má prednosť | `test_validation_pass_selects_hypothesis_for_gate0_unless_manually_off` |
| M18 | 9 krok people navštívi aj stránku výsledku hľadania | `test_website_people_visits_result_page_first` |
| M18 | 10 bez LIA sa nehľadá; funkcia „práca s dátami“ podľa celých slov; držitelia dát bez návrhu rozhovoru | `test_contact_search_needs_lia`, `test_data_role_fit_uses_whole_words`, `test_drafts_skip_data_holders` |
| M18 | 11 GUI Brána 0 → Koho osloviť: e-mail, odporúčanie, dôvody, čo žiadať | `test_m2_gui.py::test_gate0_shows_whom_to_contact_with_email_reason_and_ask` |
| M19 | 1 lievik: ďalší krok podľa stavu – test → kontakty → Brána 1 → Brána 2; čaká na tvoje dáta a na uzamknutie; hotovo | `test_m19_funnel.py::test_next_step_follows_hypothesis_from_test_to_gate2` |
| M19 | 2 neúspešný test končí; kontakty bez LIA preskočené; krok mimo autopilot_hours čaká | `test_failed_test_stops_and_contacts_skip_without_lia_or_outside_hours` |
| M19 | 3 výber: fokus → rozpracované → neotestované → hľadanie nových | `test_pick_prefers_focus_then_started_then_untested_then_find` |
| M19 | 4 hľadanie nových po N testoch; zaseknutý krok (rovnaký stav) sa odloží | `test_find_every_n_tests_and_stuck_step_is_skipped` |
| M19 | 5 jedna úloha = jeden krok so záznamom; ďalšia úloha do poradia (0 min = donekonečna) pokračuje tou istou hypotézou | `test_funnel_job_runs_one_step_logs_it_and_queues_next` |
| M19 | 6 reťaz končí po čase alebo po opakovaných zlyhaniach | `test_funnel_chain_stops_after_time_or_repeated_failures` |
| M19 | 7 reštart prerušenej úlohy zachová argumenty reťaze | `test_restart_keeps_chain_arguments` |
| M19 | 8 GUI: posledné kroky lievika a „Čaká na teba“ | `test_actions_page_shows_funnel_log_and_waiting_hypotheses` |
| M20 | 1 nefunkčný odkaz ani stránka prihlásenia nie je overený zdroj; pole ostane „iba odkaz“ | `test_m20_data_trust.py::test_broken_link_or_login_page_is_not_verified_and_field_stays_partial` |
| M20 | 2 overenie súboru: presmerovanie, blokovaná doména aj po presmerovaní, HTML namiesto dát | `test_fetcher_probe_follows_redirects_blocks_social_and_detects_html` |
| M20 | 3 HTML uložené ako CSV sa neimportuje ako tabuľka | `test_html_saved_as_csv_is_rejected_not_imported_as_table` |
| M20 | 4 SpreadsheetML s 3 riadkami → 3 záznamy, bez stĺpcov formátovania, jednotka z hlavičky | `test_spreadsheetml_rows_become_records_without_formatting_columns` |
| M20 | 5 RDF kocka: pozorovania s dimenziami (aj cez rdf:nodeID) a jednotkou | `test_rdf_cube_observations_keep_dimensions_and_unit` |
| M20 | 6 RDF s odpojenými dimenziami alebo bez pozorovaní nie je použiteľné | `test_rdf_with_disconnected_dimensions_or_without_observations_is_not_usable` |
| M20 | 7 neznáme XML a CSV s odmietnutými riadkami → na kontrolu, nerátajú sa; schválenie/zamietnutie človekom (label) | `test_unknown_xml_and_rejected_csv_rows_go_to_review_and_do_not_count` |
| M20 | 8 XLSX: nadpis nad tabuľkou a dvojriadková hlavička | `test_xlsx_title_rows_and_two_row_header_are_detected` |
| M20 | 9 duplikát distribúcie sa ráta raz; pôvodný súbor podľa SHA-256 | `test_duplicate_distribution_counts_once_and_raw_file_is_kept_by_hash` |
| M20 | 10 register liekov nepokrýva patent ani cenu (exspirácia ≠ patent); vysoké skóre nenahradí kľúčové pole | `test_drug_register_does_not_cover_patent_or_price`, `test_concept_guard_needs_concept_in_column_not_in_model_text` |
| M20 | 11 stĺpec overený kódom + tvoje potvrdenie → prechod pravidlom; zamietnutie väzby → „treba znova overiť“ | `test_binding_confirmed_by_human_passes_gate_and_rejection_revokes_it` |
| M20 | 12 ručný prechod ostáva s autorom a príznakom; staré rozhodnutie pravidlom sa preverí | `test_manual_pass_is_kept_with_author_but_flagged` |
| M20 | 13 podhypotéza dedí dáta a návrhy väzieb, nie prechod; lievik čaká na potvrdenie stĺpcov | `test_split_child_inherits_data_and_bindings_not_the_pass` |
| M20 | 14 interné pole → partner, web sa nehľadá | `test_internal_field_goes_to_partner_not_web_search` |
| M20 | 15 GUI: potvrdenie stĺpca a schválenie importu | `test_gate1_page_confirms_column_and_approves_import` |
| M21 | 1 zmena validačných odpovedí nezmení príznaky učenia (sonda z auditu: 200 záznamov, 112/28/60); zmena testovacích nezmení príznaky | `test_m21_experiments.py::test_entity_rate_feature_ignores_validation_and_test_answers` |
| M21 | 2 zmena testovacích odpovedí nezmení predikcie ani výber modelu; štítky dozreté pred každou hranicou | `test_test_labels_do_not_change_predictions_and_labels_mature_per_boundary` |
| M21 | 3 silné porovnanie vybrané na validácii; osi, verdikt, verzie, interval po entitách | `test_strong_baseline_is_chosen_on_validation_and_result_has_axes_and_versions`, `test_cluster_bootstrap_and_entity_unit` |
| M21 | 4 E6: splnená predregistrácia proti slabému porovnaniu nie je prínos | `test_verdict_does_not_claim_benefit_against_weak_baseline_e6` |
| M21 | 5 štítok z inej tabuľky: neznáma dostupnosť a neohraničený čas blokujú; horizont = okno + oneskorenie zverejnenia | `test_lookup_label_needs_known_availability_and_horizon_includes_publication_delay` |
| M21 | 6 jednotka štítka, nesprávna entita, nekontrolované subjekty NKÚ blokujú uzamknutie | `test_label_unit_entity_and_uninspected_nku_subjects_block_the_experiment` |
| M21 | 7 staršie uzamknutý experiment s metodickou chybou → „neplatné – metodika“ | `test_old_locked_experiment_with_methodology_error_is_marked_invalid` |
| M21 | 8 záver uvádza verdikt a silné porovnanie | `test_conclusion_states_verdict_and_strong_baseline` |
| M21 | 9 neúspešný návrh sa opakuje iba pri zmene odtlačku vstupov | `test_failed_plan_is_retried_only_when_inputs_change` |
| M21 | 10 GUI: verdikt, osi (a)–(d), zápis výsledku u používateľa | `test_gate2_page_shows_verdict_axes_and_saves_user_result` |
| M22 | 1 krátky beh: tvoje dáta a vetva vybranej hypotézy pred zberom; zber má vyhradený podiel; starnutie | `test_m22_priorities.py::test_short_run_plans_user_inputs_and_branches_before_discovery`, `test_m9_agentic.py::test_autopilot_plans_round_by_work_and_time_and_continues` |
| M22 | 2 najviac 3 prioritné hypotézy, najďalej v lieviku prvé | `test_priority_hypotheses_are_limited_and_furthest_first` |
| M22 | 3 účel kontaktu s dôkazom; politik, člen orgánu a osoba z článku nie sú vlastníci procesu | `test_contact_purposes_need_evidence_and_politicians_are_not_owners`, `test_rank_gives_role_owner_only_with_responsibility_evidence` |
| M22 | 4 osi stavu samostatne; krok obchodného stavu iba s dôkazom | `test_axes_are_separate_and_business_steps_need_proof` |
| M22 | 5 postup k zákazníkovi; počet hypotéz iba prevádzková metrika | `test_customer_progress_counts_steps_toward_customer` |
| M22 | 6 referenčná vzorka s kontrolnou časťou (stabilný hash) a zhodou systému | `test_reference_sample_has_stable_holdout_and_system_agreement` |
| M22 | 7 dokument dodávateľa nie je úspech objavovania | `test_vendor_documents_do_not_reward_discovery` |
| M22 | 8 kvalita IČO s pôvodnou hodnotou; profil zo snapshotu tabuľky | `test_identifier_quality_keeps_original_and_reprofile_from_snapshot` |
| M22 | 9 GUI: osi na stránke Problémy, referenčná vzorka v Evale | `test_problems_page_shows_axes_and_eval_page_reference_sample` |
| M23 | 1 sonda auditu: oslovený, odpovedajúci a odmietnutý kontakt nie je vo fronte prvého oslovenia; odmietnutý sa nepočíta do cieľa | `test_m23_contact_queue.py::test_states_from_audit_probe_and_first_contact_queue` |
| M23 | 2 oslovenie v inej hypotéze a odstup organizácie | `test_contact_from_other_hypothesis_and_org_cooldown` |
| M23 | 3 spoločný e-mail a jedna osoba z organizácie vo fronte; súhrn podľa unikátnych organizácií | `test_shared_mailbox_and_one_contact_per_organization_in_queue` |
| M23 | 4 migrácia v13 zachová oslovenia a umožní oslovenie organizácie bez osoby | `test_migration_v13_keeps_outreach_and_allows_organization_contact` |
| M24 | 1 prehľad: citáty, tvrdenie bez citátu medzi neznámymi, tri vyvracajúce otázky, hodnotiteľ v roli reviewer, Markdown | `test_m24_value.py::test_brief_cites_evidence_moves_gaps_to_unknowns_and_has_three_questions` |
| M24 | 2 jeden prehľad pre doménu; nová verzia iba pri zmene podkladov, história ostáva | `test_brief_is_shared_in_domain_and_new_version_only_when_inputs_change` |
| M24 | 3 zlyhanie hodnotiteľa = čaká na hodnotenie, nie schválenie | `test_reviewer_failure_leaves_brief_pending_not_approved` |
| M24 | 4 návrh prínosu: hypotéza potreby, bez sľubov a čísel bez podkladu, malá požiadavka, záväzok iba po potvrdení človekom | `test_value_proposals_are_hypotheses_without_promises_and_commitment_needs_human` |
| M24 | 5 držiteľ dát nie je nákupca | `test_data_holder_is_not_treated_as_buyer` |
| M24 | 6 krok value a GUI Brána 0 → Doména a prínos | `test_run_value_stage_and_gui_tab`, `test_gate0_page_shows_domain_brief_and_value_proposals` |
| M25 | 1 zákazník, držiteľ dát, expert aj funkčný kontakt organizácie dostanú návrh podľa svojej roly; reťazec prečo → potreba → výstup → vstup | `test_m25_outreach_modes.py::test_customer_data_holder_expert_and_mailbox_get_role_specific_drafts`, `test_m18_contacts.py::test_data_holder_gets_data_request_draft` |
| M25 | 2 potreba ako hypotéza; číselný sľub bez merania a „nejde o ponuku“ pri ponuke sú chyby; demo pri ponuke povolené | `test_need_as_fact_promises_and_not_offer_claims_are_flagged` |
| M25 | 3 pilot až po potvrdení problému, s obchodným zámerom a podmienkami | `test_pilot_only_after_confirmed_problem_and_says_business_intent` |
| M25 | 4 zástupné polia = rozpracovaný návrh; export s reťazcom dôvodov a pseudonymizáciou; chýbajúci zdroj dôvodu na doplnenie | `test_placeholders_mark_draft_unfinished_and_export_has_reason_chain`, `test_missing_source_of_reason_marks_draft_for_completion` |
| M26 | 6 dve nasadenia toho istého modelu sú v GUI rozlíšené, výber prežije reštart, beh posiela správny názov nasadenia | `test_m26_cloud.py::test_two_deployments_of_same_model_are_distinct_selection_survives_restart_and_runtime_sends_deployment`, `test_gui_lists_deployments_distinctly_and_keeps_role_selection` |
| M26 | 7 načítanie nasadení bez oprávnenia je zrozumiteľné, ručné pridanie funguje, výber nič nevytvára; neoverená schopnosť sa nezapíše | `test_listing_without_permission_is_understandable_manual_add_works_and_nothing_is_created`, `test_connection_test_checks_text_and_json_schema_separately` |
| M26 | 8 lokálny generátor a cloudový hodnotiteľ majú oddelené poverenia a parametre; tajomstvo nie je v DB ani logoch; stará lokálna konfigurácia funguje | `test_local_generator_and_cloud_evaluator_use_separate_credentials_and_params_without_leaking_secret`, `test_old_local_config_works_unchanged` |
| M26 | 9 nepodporovaný parameter bez JSON fallbacku, formát s fallbackom, obmedzené opakovanie pri limite, kvóta a rozpočet bez drahšieho náhradného modelu a bez falošného schválenia | `test_unsupported_parameter_does_not_downgrade_json_and_item_waits`, `test_confirmed_format_unsupport_falls_back_to_simpler_json_mode`, `test_rate_limit_retries_are_bounded_and_quota_stops_without_fallback`, `test_exhausted_budget_or_missing_rate_never_calls_and_cloud_reviewer_failure_leaves_brief_pending` |
| M26 | 10 rezervácia pokrýva súbežné volania a opakovania; po limite žiadne volanie; spotreba je vlastný odhad | `test_reservations_cover_concurrent_calls`, `test_timeout_keeps_reservation_and_limit_blocks_next_call_spend_is_own_estimate` |
| M26 | súkromie a cache: úlohy s osobnými údajmi neodídu bez povolenia; cache iba pre rovnaké nasadenie a vstup | `test_personal_data_tasks_do_not_leave_the_computer_unless_allowed`, `test_cache_reuses_answer_only_for_same_deployment_and_prompt` |
| M27 | výsledok zmení stav oslovenia a potreby (iba pre danú organizáciu), aktualizuje prehľad domény s históriou a bez mena v prompte | `test_m27_outcomes.py::test_outcome_updates_outreach_status_need_and_triggers_brief_update_with_history` |
| M27 | metriky po unikátnych organizáciách, čas prípravy a dodania, rozpad podľa režimu | `test_metrics_count_unique_organizations_not_messages` |
| M27 | stav potreby potvrdená / vyvrátená / zmiešaná podľa typu adresáta; neplatný vstup sa odmietne | `test_refuted_and_mixed_need_status_and_validation` |
| M27 | forget a námietka zmažú výsledky; GUI zapíše výsledok a ukáže metriky | `test_forget_and_objection_remove_outcomes`, `test_outreach_page_records_outcome_and_shows_org_metrics` |
