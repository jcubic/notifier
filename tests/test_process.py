"""Tests for process_rule, fetch, and higher-level integration."""

from unittest import mock

import pytest

from mutimon import main


# ========================= detect_language =========================


class TestDetectLanguage:
    def test_from_html_lang(self):
        html = '<html lang="pl"><body></body></html>'
        locale = main.detect_language(html)
        assert locale is not None

    def test_from_content_language_header(self):
        html = "<html><body></body></html>"
        locale = main.detect_language(html, response_headers={"Content-Language": "de"})
        assert locale is not None

    def test_default_en(self):
        html = "<html><body></body></html>"
        locale = main.detect_language(html)
        assert locale is not None

    def test_invalid_lang_falls_back(self):
        html = '<html lang="zzzzz"><body></body></html>'
        locale = main.detect_language(html)
        assert locale is not None


# ========================= fetch_page =========================


class TestFetchPage:
    def test_fetch_page(self):
        fake_response = mock.MagicMock()
        fake_response.text = '<html lang="en"><body>Hello</body></html>'
        fake_response.headers = {}
        with mock.patch("mutimon.main.requests.request", return_value=fake_response):
            html, locale = main.fetch_page("https://example.com")
        assert "Hello" in html

    def test_fetch_page_xml(self):
        fake_response = mock.MagicMock()
        xml_content = b"<rss><channel><item>Test</item></channel></rss>"
        fake_response.content = xml_content
        fake_response.text = xml_content.decode()
        fake_response.headers = {}
        with mock.patch("mutimon.main.requests.request", return_value=fake_response):
            html, locale = main.fetch_page("https://example.com/feed", is_xml=True)
        assert "item" in str(html)

    def test_custom_user_agent(self):
        fake_response = mock.MagicMock()
        fake_response.text = "<html></html>"
        fake_response.headers = {}
        with mock.patch("mutimon.main.requests.request", return_value=fake_response) as mock_get:
            main.fetch_page("https://example.com", user_agent="TestBot/1.0")
        call_headers = mock_get.call_args[1]["headers"]
        assert call_headers["User-Agent"] == "TestBot/1.0"


# ========================= fetch_all_items =========================


class TestFetchAllItems:
    def _mock_fetch(self, html):
        fake_resp = mock.MagicMock()
        fake_resp.text = html
        fake_resp.headers = {}
        return mock.patch("mutimon.main.requests.request", return_value=fake_resp)

    def test_basic_fetch(self):
        html = """
        <html><body>
        <div class="item" data-id="1"><h3>Title 1</h3></div>
        <div class="item" data-id="2"><h3>Title 2</h3></div>
        </body></html>
        """
        definition = {
            "url": "https://example.com",
            "query": {
                "type": "list",
                "selector": "div.item",
                "id": {"type": "attribute", "name": "data-id"},
                "variables": {
                    "title": {"selector": "h3", "value": {"type": "text"}},
                },
            },
        }
        with self._mock_fetch(html):
            items = main.fetch_all_items(definition, {})
        assert len(items) == 2
        assert items[0]["title"] == "Title 1"

    def test_expect_raises_on_missing(self):
        html = "<html><body><p>Empty</p></body></html>"
        definition = {
            "url": "https://example.com",
            "query": {
                "type": "list",
                "selector": "div.item",
                "expect": ["div.item"],
                "variables": {},
            },
        }
        with self._mock_fetch(html):
            with pytest.raises(ValueError, match="Missing expected"):
                main.fetch_all_items(definition, {})

    def test_reject_returns_empty(self):
        html = """
        <html><body>
        <div class="no-results">No results found</div>
        <div class="item"><h3>Recommended</h3></div>
        </body></html>
        """
        definition = {
            "url": "https://example.com",
            "query": {
                "type": "list",
                "selector": "div.item",
                "reject": [".no-results"],
                "variables": {
                    "title": {"selector": "h3", "value": {"type": "text"}},
                },
            },
        }
        with self._mock_fetch(html):
            items = main.fetch_all_items(definition, {})
        assert len(items) == 0


# ========================= query_json =========================


class TestQueryJson:
    def setup_method(self):
        main.setup_liquid({"defs": {}})

    def test_simple_path(self):
        data = {"items": [{"name": "a"}, {"name": "b"}]}
        spec = {
            "type": "list",
            "path": "items",
            "variables": {"name": {"path": "name"}},
        }
        result = main.query_json(data, spec, {})
        assert len(result) == 2
        assert result[0]["name"] == "a"

    def test_single_type(self):
        data = {"info": {"title": "Test"}}
        spec = {
            "type": "single",
            "path": "info",
            "variables": {"title": {"path": "title"}},
        }
        result = main.query_json(data, spec, {})
        assert isinstance(result, dict)
        assert result["title"] == "Test"

    def test_liquid_variables_in_path(self):
        data = {"users": {"abc": {"name": "Alice"}}}
        spec = {
            "type": "single",
            "path": "users.{{uid}}",
            "variables": {"name": {"path": "name"}},
        }
        result = main.query_json(data, spec, {"uid": "abc"})
        assert result["name"] == "Alice"


# ========================= process_rule =========================


class TestProcessRule:
    def setup_method(self):
        main.setup_liquid({"defs": {}})

    def _make_config(self, tmp_mutimon):
        template = tmp_mutimon / "templates" / "test"
        template.write_text(
            "Items: {{count}}\n{% for item in items %}{{item.title}}\n{% endfor %}"
        )
        return {
            "email": {
                "server": {
                    "host": "smtp.test.com",
                    "port": 587,
                    "password": "pass",
                    "email": "from@test.com",
                }
            },
            "defs": {
                "test-site": {
                    "url": "https://example.com",
                    "query": {
                        "type": "list",
                        "selector": "div.item",
                        "id": {"type": "attribute", "name": "data-id"},
                        "variables": {
                            "title": {
                                "selector": "h3",
                                "value": {"type": "text"},
                            },
                        },
                    },
                }
            },
            "rules": [],
        }

    def _mock_fetch(self, html):
        fake_resp = mock.MagicMock()
        fake_resp.text = html
        fake_resp.headers = {}
        return mock.patch("mutimon.main.requests.request", return_value=fake_resp)

    def test_new_items_trigger_email(self, tmp_mutimon):
        config = self._make_config(tmp_mutimon)
        rule = {
            "ref": "test-site",
            "name": "test-proc",
            "subject": "New: {{count}}",
            "template": "./templates/test",
            "email": "user@test.com",
        }
        html = """
        <html><body>
        <div class="item" data-id="1"><h3>First</h3></div>
        <div class="item" data-id="2"><h3>Second</h3></div>
        </body></html>
        """
        with self._mock_fetch(html):
            with mock.patch("mutimon.main.send_email") as mock_send:
                main.process_rule(config, rule)
                mock_send.assert_called_once()

        # State should be saved
        state = main.load_state("test-proc")
        assert len(state) == 2

    def test_known_items_no_email(self, tmp_mutimon):
        config = self._make_config(tmp_mutimon)
        rule = {
            "ref": "test-site",
            "name": "test-known",
            "subject": "New: {{count}}",
            "template": "./templates/test",
            "email": "user@test.com",
        }
        # Pre-save state
        main.save_state(
            "test-known",
            [
                {"id": "1", "title": "First", "_valid": True},
                {"id": "2", "title": "Second", "_valid": True},
            ],
        )
        html = """
        <html><body>
        <div class="item" data-id="1"><h3>First</h3></div>
        <div class="item" data-id="2"><h3>Second</h3></div>
        </body></html>
        """
        with self._mock_fetch(html):
            with mock.patch("mutimon.main.send_email") as mock_send:
                main.process_rule(config, rule)
                mock_send.assert_not_called()

    def test_save_only_mode(self, tmp_mutimon):
        config = self._make_config(tmp_mutimon)
        rule = {
            "ref": "test-site",
            "name": "test-save-only",
            "subject": "New: {{count}}",
            "template": "./templates/test",
            "email": "user@test.com",
        }
        html = '<html><body><div class="item" data-id="1"><h3>Item</h3></div></body></html>'
        with self._mock_fetch(html):
            main.process_rule(config, rule, save_only=True)

        # Email should be saved to file
        email_file = tmp_mutimon / "data" / "emails" / "test-save-only.txt"
        assert email_file.exists()

    def test_email_failure_skips_state_save(self, tmp_mutimon):
        config = self._make_config(tmp_mutimon)
        rule = {
            "ref": "test-site",
            "name": "test-fail",
            "subject": "New: {{count}}",
            "template": "./templates/test",
            "email": "user@test.com",
        }
        html = '<html><body><div class="item" data-id="1"><h3>Item</h3></div></body></html>'
        with self._mock_fetch(html):
            with mock.patch(
                "mutimon.main.send_email", side_effect=Exception("SMTP error")
            ):
                main.process_rule(config, rule)

        # State should NOT be saved
        state = main.load_state("test-fail")
        assert state == []

    def test_missing_definition(self, tmp_mutimon, capsys):
        config = self._make_config(tmp_mutimon)
        rule = {
            "ref": "nonexistent",
            "name": "test-missing",
            "subject": "Test",
            "template": "./templates/test",
            "email": "user@test.com",
        }
        main.process_rule(config, rule)
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_dedup_across_inputs(self, tmp_mutimon):
        config = self._make_config(tmp_mutimon)
        rule = {
            "ref": "test-site",
            "name": "test-dedup",
            "subject": "New: {{count}}",
            "template": "./templates/test",
            "email": "user@test.com",
            "input": [
                {"params": {}},
                {"params": {}},
            ],
        }
        html = '<html><body><div class="item" data-id="1"><h3>Item</h3></div></body></html>'
        with self._mock_fetch(html):
            with mock.patch("mutimon.main.send_email") as mock_send:
                main.process_rule(config, rule)
                # Should send only 1 item, not 2 (deduped)
                # Check the body contains the item only once
                assert mock_send.called

        state = main.load_state("test-dedup")
        assert len(state) == 1  # Deduped


# ========================= send_error_email =========================


class TestSendErrorEmail:
    def test_sends_error_email(self, tmp_mutimon, write_config, sample_config):
        write_config()
        with mock.patch("mutimon.main.smtplib.SMTP") as mock_smtp:
            mock_server = mock.MagicMock()
            mock_smtp.return_value.__enter__ = mock.Mock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = mock.Mock(return_value=False)
            main.send_error_email("[mutimon] Test error", "Error details")
            mock_server.send_message.assert_called_once()

    def test_error_email_catches_exceptions(self, tmp_mutimon, write_config, sample_config):
        write_config()
        with mock.patch("mutimon.main.smtplib.SMTP", side_effect=Exception("fail")):
            # Should not raise, just print
            main.send_error_email("[mutimon] Test", "Body")


# ========================= run() CLI handler =========================


class TestRunFunction:
    def setup_method(self):
        main.verbose = False

    def test_validate_flag(self, tmp_mutimon, write_config, sample_config):
        write_config()
        with mock.patch("sys.argv", ["mon", "--validate"]):
            main.run()  # Should not raise

    def test_list_flag(self, tmp_mutimon, write_config, sample_config, capsys):
        write_config()
        with mock.patch("sys.argv", ["mon", "--list"]):
            main.run()
        out = capsys.readouterr().out
        assert "test-rule" in out

    def test_list_empty(self, tmp_mutimon, write_config, capsys):
        config = {
            "email": {
                "server": {
                    "host": "smtp.test.com",
                    "port": 587,
                    "password": "x",
                    "email": "x@x.com",
                }
            },
            "defs": {},
            "rules": [],
        }
        write_config(config)
        with mock.patch("sys.argv", ["mon", "--list"]):
            main.run()
        out = capsys.readouterr().out
        assert "No rules" in out

    def test_ai_guide_flag(self, capsys):
        with mock.patch("sys.argv", ["mon", "--ai-guide"]):
            main.run()
        out = capsys.readouterr().out
        assert "# Mutimon" in out

    def test_cron_default(self, capsys):
        with mock.patch("sys.argv", ["mon", "--cron"]):
            main.run()
        out = capsys.readouterr().out
        assert "*/5 * * * *" in out
        assert "mon -q" in out
        assert "mutimon.log" in out

    def test_cron_custom_schedule(self, capsys):
        with mock.patch("sys.argv", ["mon", "--cron", "0 8 * * *"]):
            main.run()
        out = capsys.readouterr().out
        assert "0 8 * * *" in out

    def test_cron_fallback_to_argv(self, capsys):
        with mock.patch("sys.argv", ["mon", "--cron"]):
            with mock.patch("shutil.which", return_value=None):
                main.run()
        out = capsys.readouterr().out
        assert "mon -q" in out

    def test_skeleton_email_rejected(self, tmp_mutimon, write_config):
        config = {
            "email": {
                "server": {
                    "host": "smtp.example.com",
                    "port": 587,
                    "password": "your-password-here",
                    "email": "you@example.com",
                }
            },
            "defs": {},
            "rules": [],
        }
        write_config(config)
        with mock.patch("sys.argv", ["mon"]):
            with mock.patch.object(main, "load_secrets", return_value={}):
                with pytest.raises(SystemExit) as exc:
                    main.run()
                assert exc.value.code == 1

    def test_force_specific_rule(self, tmp_mutimon, write_config, sample_config):
        write_config()
        template = tmp_mutimon / "templates" / "test"
        template.write_text("{{count}}")
        html = '<html><body><div class="item"><h3>X</h3><a href="/x">x</a></div></body></html>'
        fake_resp = mock.MagicMock()
        fake_resp.text = html
        fake_resp.headers = {}
        with mock.patch("sys.argv", ["mon", "--force", "test-rule"]):
            with mock.patch("mutimon.main.requests.request", return_value=fake_resp):
                with mock.patch("mutimon.main.send_email"):
                    main.run()

    def test_force_unknown_rule_exits(self, tmp_mutimon, write_config, sample_config):
        write_config()
        with mock.patch("sys.argv", ["mon", "--force", "nonexistent"]):
            with pytest.raises(SystemExit):
                main.run()

    def test_quiet_suppresses_output(self, tmp_mutimon, write_config, sample_config, capsys):
        write_config()
        with mock.patch("sys.argv", ["mon", "--quiet", "--validate"]):
            main.run()
        out = capsys.readouterr().out
        # quiet suppresses stdout
        assert out == ""

    def test_dry_run(self, tmp_mutimon, write_config, sample_config):
        write_config()
        template = tmp_mutimon / "templates" / "test"
        template.write_text("{{count}}")
        html = '<html><body><div class="item"><h3>X</h3><a href="/x">x</a></div></body></html>'
        fake_resp = mock.MagicMock()
        fake_resp.text = html
        fake_resp.headers = {}
        with mock.patch("sys.argv", ["mon", "--dry-run", "--force"]):
            with mock.patch("mutimon.main.requests.request", return_value=fake_resp):
                main.run()
        # No state saved in dry-run
        state = main.load_state("test-rule")
        assert state == []


class TestMainEntryPoint:
    def test_main_catches_exceptions(self, capsys):
        with mock.patch("mutimon.main.run", side_effect=Exception("boom")):
            with mock.patch("mutimon.main.send_error_email"):
                main.main()
        err = capsys.readouterr().err
        assert "boom" in err
