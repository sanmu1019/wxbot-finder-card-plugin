from __future__ import annotations

import html
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = ROOT / "plugin" / "wxsph_parser" / "main.py"


class DummyLogger:
    def debug(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


def install_framework_stubs() -> None:
    config_package = types.ModuleType("config")
    config_module = types.ModuleType("config.config")

    class Config:
        BASE_DIR = ROOT
        CURRENT_BOT_WXID = ""
        EXPECTED_BOT_WXID = ""
        WECHAT_API_BASE_URL = "http://127.0.0.1:9000/api"
        WECHAT_HTTP_PORT = 9011

    config_module.Config = Config
    sys.modules["config"] = config_package
    sys.modules["config.config"] = config_module

    core_package = types.ModuleType("core")
    context_module = types.ModuleType("core.context")

    class ContextType:
        SHARING = "sharing"

    context_module.ContextType = ContextType

    plugin_module = types.ModuleType("core.plugin_system")

    class Event:
        ON_HANDLE_CONTEXT = "on_handle_context"

    class EventAction:
        BREAK_PASS = "break_pass"

    class EventContext:
        pass

    class Plugin:
        def __init__(self) -> None:
            self.handlers = {}

        def load_config(self) -> dict:
            return {}

    def register(**_kwargs):
        def decorator(cls):
            return cls

        return decorator

    plugin_module.Event = Event
    plugin_module.EventAction = EventAction
    plugin_module.EventContext = EventContext
    plugin_module.Plugin = Plugin
    plugin_module.register = register

    wechat_api_module = types.ModuleType("core.wechat_api")

    class WechatAPIClient:
        pass

    wechat_api_module.WechatAPIClient = WechatAPIClient

    sys.modules["core"] = core_package
    sys.modules["core.context"] = context_module
    sys.modules["core.plugin_system"] = plugin_module
    sys.modules["core.wechat_api"] = wechat_api_module

    utils_package = types.ModuleType("utils")
    download_module = types.ModuleType("utils.download_helper")
    logger_module = types.ModuleType("utils.logger")
    download_module.download_video = lambda *_args, **_kwargs: ""
    logger_module.get_logger = lambda _name: DummyLogger()

    sys.modules["utils"] = utils_package
    sys.modules["utils.download_helper"] = download_module
    sys.modules["utils.logger"] = logger_module


@pytest.fixture(scope="module")
def plugin_module():
    install_framework_stubs()
    spec = importlib.util.spec_from_file_location(
        "wxsph_parser_test_module",
        PLUGIN_FILE,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def parser(plugin_module):
    return plugin_module.WxSphParser.__new__(
        plugin_module.WxSphParser
    )


def test_extracts_card_ids_from_escaped_xml(parser) -> None:
    raw_xml = """
    <msg>
      <appmsg>
        <finderFeed>
          <objectId>12345678901234567890</objectId>
          <objectNonceId>nonce_example_123456</objectNonceId>
          <nickname>示例作者</nickname>
        </finderFeed>
      </appmsg>
    </msg>
    """

    result = parser._extract_finder_card_meta(
        html.escape(raw_xml)
    )

    assert result["object_id"] == "12345678901234567890"
    assert result["object_nonce_id"] == "nonce_example_123456"
    assert result["nickname"] == "示例作者"


def test_extracts_existing_sph_link(parser) -> None:
    text = (
        "解析 https%3A%2F%2Fweixin.qq.com%2Fsph%2FExample_123"
    )

    assert parser._extract_share_url(
        text
    ) == "https://weixin.qq.com/sph/Example_123"


def test_object_id_is_not_treated_as_sph_code(parser) -> None:
    raw_xml = """
    <msg>
      <appmsg>
        <finderFeed>
          <objectId>12345678901234567890</objectId>
        </finderFeed>
      </appmsg>
    </msg>
    """

    assert parser._extract_finder_card_url(
        raw_xml,
        raw_xml,
    ) == ""


def test_bridge_uses_post_json(plugin_module, parser, monkeypatch) -> None:
    captured = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "code": 0,
                "data": {
                    "sph_url": (
                        "https://weixin.qq.com/sph/Example_123"
                    )
                },
            }

    def fake_post(url, json, headers, timeout):
        captured.update(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr(plugin_module.requests, "post", fake_post)
    parser.finder_bridge_base = "http://127.0.0.1:8790"
    parser.finder_bridge_api_key = "example-key"
    parser.finder_bridge_timeout = 30

    result = parser._resolve_card_share_url(
        {
            "object_id": "12345678901234567890",
            "object_nonce_id": "nonce_example_123456",
        }
    )

    assert result == "https://weixin.qq.com/sph/Example_123"
    assert captured["url"].endswith(
        "/api/v1/finder/share-url"
    )
    assert captured["json"] == {
        "object_id": "12345678901234567890",
        "object_nonce_id": "nonce_example_123456",
        "scene": 40,
    }
    assert captured["headers"]["X-API-Key"] == "example-key"
