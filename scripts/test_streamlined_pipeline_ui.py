"""Real-loopback browser checks for reviewed one-action execution and task control.

Uses constructed entities and a controlled acquisition boundary; no external HTTP
or actual cache performance is measured. Server locks, SQLite, analysis and UI
are real. Requires Playwright and Chromium; reports go to ignored runtime/.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_workflow_ui import ControlledOperations, api, wait_until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser-path",
        default=shutil.which("chromium") or shutil.which("chromium-browser"),
    )
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    from peps.app import create_server
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.workflow import GuidedWorkflow
    from peps.news_projection import projection_signature

    out = ROOT / "runtime/streamlined-pipeline-ui-validation"
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "kind": "synthetic_real_loopback_streamlined_ui",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "external_network": False,
        "checks": {},
        "screenshots": [],
    }
    errors = []
    external = []
    starts = []
    hold_retry = False
    held_retry_routes = []
    control = ControlledOperations()

    def checked(name, condition=True):
        assert condition, name
        report["checks"][name] = True

    try:
        with tempfile.TemporaryDirectory(prefix="webscope-run-ui-") as tmp:
            engine = Engine(tmp)
            engine.store.import_entities(
                [
                    {
                        "id": f"run-ui-{i}",
                        "name": f"Entidad Ficticia del Proceso {i}",
                        "kind": "company",
                    }
                    for i in range(3)
                ]
            )
            config = {
                "search": {
                    "strategy": "feeds",
                    "execution_mode": "quick",
                    "session_seconds": 120,
                    "start": "2020-01-01",
                    "end": "2025-12-31",
                },
                "sources": [
                    {
                        "id": "synthetic_rss",
                        "source_name": "Fuente ficticia del proceso",
                        "kind": "rss",
                        "url": "https://fixture.invalid/feed.xml",
                        "base_url": "https://fixture.invalid",
                        "allowed_hosts": ["fixture.invalid"],
                        "enabled": True,
                        "terms_approved": True,
                        "download_articles": True,
                        "article_terms_approved": True,
                        "min_interval_seconds": 5,
                        "review_note": "Fixture de interfaz sin solicitudes externas.",
                    }
                ],
            }
            (engine.data_dir / "news_sources.json").write_text(json.dumps(config))
            with (
                patch.object(
                    NewsAcquirer,
                    "acquire",
                    lambda acquirer, *a, **kw: control.acquire(acquirer, *a, **kw),
                ),
                patch(
                    "peps.collector.SafeFetcher",
                    side_effect=AssertionError("External HTTP forbidden"),
                ),
            ):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = f"http://127.0.0.1:{server.server_port}"
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(
                            executable_path=args.browser_path,
                            headless=True,
                            args=["--no-sandbox"],
                        )
                        context = browser.new_context(
                            viewport={"width": 1280, "height": 900}
                        )

                        def request(route):
                            r = route.request
                            if not r.url.startswith(address + "/"):
                                external.append(r.url)
                                route.abort()
                                return
                            if (
                                r.url.endswith("/api/workflow-run")
                                and r.method == "POST"
                            ):
                                starts.append(r.post_data_json)
                            if (
                                r.url.endswith("/api/news-source")
                                and r.method == "POST"
                            ):
                                checked(
                                    "run_disabled_during_source_permission_save",
                                    page.locator(
                                        "#workflow-sources-next"
                                    ).is_disabled(),
                                )
                            if r.url.endswith("/api/news-retry") and hold_retry:
                                held_retry_routes.append(route)
                                return
                            route.continue_()

                        context.route("**/*", request)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(address, wait_until="networkidle")
                        expect(page.locator("[data-panel=sources]")).to_be_visible()
                        expect(page.locator("#workflow-sources-next")).to_be_disabled()
                        checked("network_consent_is_explicit")
                        checked(
                            "two_visible_permissions_per_ordinary_source",
                            page.locator(
                                "[data-source-id=synthetic_rss] input[type=checkbox]"
                            ).count()
                            == 2,
                        )
                        for width, height in [(1280, 900), (390, 720)]:
                            page.set_viewport_size({"width": width, "height": height})
                            rect = page.locator("#workflow-sources-next").bounding_box()
                            checked(
                                f"run_cta_inside_{width}x{height}",
                                rect["y"] >= 0 and rect["y"] + rect["height"] <= height,
                            )
                            checked(
                                f"no_overflow_{width}",
                                page.evaluate(
                                    "document.documentElement.scrollWidth<=innerWidth"
                                ),
                            )
                        page.set_viewport_size({"width": 1280, "height": 900})
                        page.locator("#news-online").check()
                        expect(page.locator("#workflow-sources-next")).to_be_enabled()
                        page.locator("#workflow-manual-discovery > summary").click()
                        expect(
                            page.locator("#manual-entity-select option")
                        ).to_have_count(4)
                        page.locator("#manual-entity-select").select_option("run-ui-0")
                        expect(page.locator("#manual-search-results")).to_contain_text(
                            "Google"
                        )
                        checked(
                            "manual_search_available_before_scoring",
                            engine.store.stats()["scores"] == 0,
                        )
                        page.locator("#manual-entity-query").fill("Proceso 1")
                        expect(page.locator("#manual-search-results")).to_be_empty()
                        expect(
                            page.locator("#manual-entity-select option")
                        ).to_have_count(2)
                        checked("manual_filter_clears_previous_entity_links")
                        page.locator("#workflow-manual-discovery > summary").click()
                        permission = page.locator(
                            "[data-source-id=synthetic_rss] input[type=checkbox]"
                        ).nth(1)
                        permission.uncheck()
                        expect(
                            page.locator(
                                "[data-source-id=synthetic_rss] .source-permission-badge"
                            )
                        ).to_contain_text("Sólo referencias")
                        permission.check()
                        expect(
                            page.locator(
                                "[data-source-id=synthetic_rss] .source-permission-badge"
                            )
                        ).to_contain_text("Con texto autorizado")
                        expect(page.locator("#workflow-sources-next")).to_be_enabled()
                        page.locator("#workflow-sources-next").click()
                        wait_until(
                            page,
                            lambda: control.entered.is_set(),
                            "Acquisition did not start",
                        )
                        checked(
                            "one_run_action_starts_pipeline",
                            len(starts) == 1
                            and starts[0]["analyze_after_download"] is True
                            and starts[0]["cache_mode"] == "reuse",
                        )
                        expect(page.locator("[data-panel=news]")).to_be_visible()
                        expect(page.locator("#stop")).to_be_visible()
                        checked("pause_available_during_pipeline")
                        blocked = api(
                            page,
                            "workflow-run",
                            {
                                "online": True,
                                "resume": False,
                                "analyze_after_download": True,
                            },
                        )
                        checked(
                            "second_pipeline_blocked_by_backend",
                            blocked["status"] == 409,
                        )
                        checked(
                            "unknown_session_metrics_show_dash",
                            page.locator("#workflow-session-metrics dd").count() == 7
                            and all(
                                x == "—"
                                for x in page.locator(
                                    "#workflow-session-metrics dd"
                                ).all_text_contents()
                            ),
                        )
                        page.locator("#workflow-inspection").evaluate("e=>e.open=true")
                        page.locator("#workflow-queue-details").evaluate(
                            "e=>e.open=true"
                        )
                        expect(page.locator("#workflow-task-list")).to_contain_text(
                            "En cola"
                        )
                        checked("task_details_loaded_on_demand")
                        page.locator("#stop").click()
                        checked(
                            "pause_request_keeps_active_lock",
                            api(page, "stats")["body"]["operation"]["busy"],
                        )
                        control.release.set()
                        wait_until(
                            page,
                            lambda: not api(page, "stats")["body"]["operation"]["busy"],
                            "Pause did not finish",
                        )
                        page.reload(wait_until="networkidle")
                        checked(
                            "paused_pipeline_does_not_score",
                            engine.store.stats()["scores"] == 0,
                        )
                        control.prepare_resume()
                        page.locator("#news-online").check()
                        expect(page.locator("#news-acquire")).to_be_enabled()
                        page.locator("#news-acquire").click()
                        wait_until(
                            page,
                            lambda: control.entered.is_set(),
                            "Resume did not start",
                        )
                        control.release.set()
                        wait_until(
                            page,
                            lambda: api(page, "stats")["body"]["workflow"][
                                "analysis_current"
                            ],
                            "Automatic analysis did not finish",
                        )
                        wait_until(
                            page,
                            lambda: page.locator("[data-panel=bank]").is_visible(),
                            "Results did not open",
                            timeout=15,
                        )
                        checked(
                            "complete_pipeline_opens_results_without_second_analysis_click"
                        )
                        checked(
                            "resume_uses_existing_queue",
                            control.queue_modes == [True, False],
                        )
                        checked(
                            "exactly_two_successful_pipeline_starts",
                            control.calls == 2 and len(starts) == 3,
                        )
                        page.locator(
                            "details.public-search-links"
                        ).count()  # Detail remains lazy until entity is selected.
                        page.locator("#entities tr").first.click()
                        expect(page.locator("#detail")).to_contain_text(
                            "Ampliar búsqueda en la web y redes públicas"
                        )
                        page.locator(".public-search-links summary").click()
                        expect(page.locator(".public-search-links")).to_contain_text(
                            "Google"
                        )
                        checked("public_search_links_load_lazily")
                        checked(
                            "manual_links_are_safe",
                            all(
                                url.startswith("https://") or url.startswith("http://")
                                for url in page.locator(
                                    ".public-search-links a"
                                ).evaluate_all("xs=>xs.map(x=>x.href)")
                            ),
                        )
                        # A failed task is selected explicitly; retry does not start another pipeline.
                        with engine.store.connection() as con:
                            current_revision = projection_signature(con)["bank_revision"]
                        jid = engine.store.enqueue(
                            "news_article",
                            {
                                "source_id": "synthetic_rss",
                                "url": "https://fixture.invalid/retry",
                                "bank_revision": current_revision,
                            },
                            key="retry-ui",
                        )
                        job = engine.store.claim(kind_prefix="news_")
                        engine.store.finish(job, "blocked", "Synthetic source denial")
                        page.locator("[data-tab=news]").click()
                        page.locator("#workflow-inspection").evaluate("e=>e.open=true")
                        page.locator("#workflow-queue-details").evaluate(
                            "e=>e.open=true"
                        )
                        page.locator("#workflow-task-filter").select_option("blocked")
                        expect(page.locator("[data-task-retry]")).to_be_visible()
                        hold_retry = True
                        page.locator("[data-task-retry]").click()
                        wait_until(
                            page,
                            lambda: bool(held_retry_routes),
                            "Retry was not requested",
                        )
                        page.evaluate(
                            "stats=>window.pepsWorkflow.update(stats)",
                            api(page, "stats")["body"],
                        )
                        checked(
                            "retry_stays_disabled_while_request_is_pending_across_poll",
                            page.locator("[data-task-retry]").is_disabled(),
                        )
                        checked(
                            "pending_retry_blocks_other_mutations",
                            page.locator("#news-acquire").is_disabled(),
                        )
                        hold_retry = False
                        held_retry_routes.pop().continue_()
                        expect(page.locator("#message")).to_contain_text("Reanuda")
                        checked(
                            "retry_is_scoped_and_never_autostarts",
                            len(starts) == 3 and control.calls == 2,
                        )
                        page.locator("[data-tab=sources]").click()
                        page.locator("#workflow-cache-mode").select_option("local")
                        expect(page.locator("#workflow-network-consent")).to_be_hidden()
                        expect(
                            page.locator("#workflow-cache-explanation")
                        ).to_contain_text("Sin solicitudes de red")
                        checked("local_cache_has_no_network_consent")
                        page.locator("#workflow-cache-mode").select_option("revalidate")
                        expect(
                            page.locator("#workflow-cache-explanation")
                        ).to_contain_text("Comprueba si las fuentes cambiaron")
                        checked("revalidation_mode_is_explicit")
                        # Persisted telemetry belongs to its recorded session, including
                        # after reload; an unfinished snapshot is not proof of activity.
                        session_fixture = {
                            "id": "synthetic-recorded-session",
                            "started_at": "2025-01-02T03:04:05+00:00",
                            "finished_at": "2025-01-02T03:05:05+00:00",
                            "cache_mode": "local",
                            "bank_revision": "synthetic-previous-bank",
                            "network_requests": 0,
                            "http_cache_hits": 3,
                            "metadata_cache_hits": 4,
                            "bodies_new": 0,
                            "bodies_reused": 2,
                            "duplicate_candidates": 7,
                            "excluded_records": 8,
                        }
                        with engine.store.connection() as con:
                            con.execute(
                                "INSERT OR REPLACE INTO meta VALUES('news_last_session', ?)",
                                (json.dumps(session_fixture),),
                            )
                        page.reload(wait_until="networkidle")
                        expect(page.locator("#workflow-session-scope")).to_contain_text(
                            "Última sesión:"
                        )
                        expect(page.locator("#workflow-session-scope")).to_contain_text(
                            "finalizada"
                        )
                        checked("persisted_session_has_date_and_finished_status")
                        checked(
                            "recorded_session_counter_values_are_preserved",
                            page.locator(
                                "#workflow-session-metrics dd"
                            ).all_text_contents()
                            == ["0", "3", "4", "0", "2", "7", "8"],
                        )
                        expect(
                            page.locator("#workflow-session-freshness")
                        ).to_contain_text("no se comprobó su vigencia en internet")
                        checked("local_session_does_not_claim_global_freshness")
                        expect(
                            page.locator("#workflow-session-freshness")
                        ).to_contain_text("La sesión corresponde a una lista anterior")
                        checked("previous_bank_session_is_not_current_bank_activity")
                        session_fixture["finished_at"] = None
                        with engine.store.connection() as con:
                            con.execute(
                                "UPDATE meta SET value=? WHERE key='news_last_session'",
                                (json.dumps(session_fixture),),
                            )
                        page.reload(wait_until="networkidle")
                        expect(page.locator("#workflow-session-scope")).to_contain_text(
                            "sin cierre registrado"
                        )
                        checked("unfinished_snapshot_is_not_shown_as_running")
                        for width, height, label in [
                            (1280, 900, "desktop"),
                            (390, 720, "mobile"),
                        ]:
                            page.set_viewport_size({"width": width, "height": height})
                            page.locator("#source-filter").fill("Fuente ficticia")
                            page.locator("#source-filter").scroll_into_view_if_needed()
                            box = page.locator("#source-filter").bounding_box()
                            bar = page.locator(".source-run-footer").bounding_box()
                            checked(
                                f"focused_filter_not_covered_{label}",
                                box["y"] + box["height"] < bar["y"],
                            )
                            page.screenshot(path=str(out / f"pipeline-{label}.png"))
                            report["screenshots"].append(
                                f"runtime/streamlined-pipeline-ui-validation/pipeline-{label}.png"
                            )
                        # Finish the selected synthetic retry without HTTP so
                        # the preparation scenario has a genuinely empty queue.
                        retried_job = engine.store.claim(kind_prefix="news_")
                        assert retried_job and retried_job["id"] == jid
                        engine.store.finish(retried_job)
                        GuidedWorkflow(
                            engine.store, NewsAcquirer(engine).sources
                        ).mark_download(
                            status="paused",
                            details={
                                "preparation_incomplete": True,
                                "queue_complete": False,
                                "pending": 0,
                                "stop_reason": "time_limit",
                            },
                        )
                        page.set_viewport_size({"width": 1280, "height": 900})
                        page.reload(wait_until="networkidle")
                        page.locator("[data-tab=news]").click()
                        expect(page.locator("#news-acquire")).to_have_text(
                            "Completar preparación"
                        )
                        expect(page.locator("#workflow-download-note")).to_contain_text(
                            "La búsqueda aún no está completa"
                        )
                        expect(page.locator("#workflow-download-status")).to_have_text(
                            "Preparación pendiente"
                        )
                        expect(
                            page.locator("#workflow-download-current")
                        ).to_contain_text("antes de completar el índice o el plan")
                        expect(page.locator("#workflow-download-wait")).to_contain_text(
                            "El plan sigue incompleto"
                        )
                        expect(page.locator("#workflow-analyze")).to_be_disabled()
                        expect(
                            page.locator("#workflow-partial-description")
                        ).to_contain_text("El plan de búsqueda quedó incompleto")
                        checked(
                            "incomplete_preparation_is_distinct_from_a_finished_empty_queue"
                        )
                        checked("no_browser_errors", not errors)
                        checked("no_external_requests", not external)
                        browser.close()
                finally:
                    control.release.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
        raise
    finally:
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "checks": len(report["checks"]),
                "report": str(out / "report.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
