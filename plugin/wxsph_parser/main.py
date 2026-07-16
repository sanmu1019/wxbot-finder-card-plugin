# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import requests

from config.config import Config
from core.context import ContextType
from core.plugin_system import Event, EventAction, EventContext, Plugin, register
from core.wechat_api import WechatAPIClient
from utils.download_helper import download_video
from utils.logger import get_logger

logger = get_logger(__name__)


@register(
    name="WxSphParser",
    desire_priority=95,
    hidden=False,
    desc="解析微信视频号分享链接，返回摘要并可转发视频",
    version="1.4.0",
    author="codex",
)
class WxSphParser(Plugin):
    U64_MASK = (1 << 64) - 1
    SHARE_URL_RE = re.compile(
        r"https?://(?:weixin\.qq\.com|mp\.weixin\.qq\.com)/sph/[^\s<>\"]+",
        re.IGNORECASE,
    )
    ANY_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
    SPH_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,48}$")
    SPH_PATH_RE = re.compile(r"(?:^|/|%2[fF])sph(?:/|%2[fF])([A-Za-z0-9_-]{6,48})", re.IGNORECASE)
    XML_FIELD_RE = re.compile(
        r"<(?P<tag>[A-Za-z_][A-Za-z0-9_.:-]*)\b[^>/]*>\s*(?P<value>[^<>]*?)\s*</(?P=tag)>",
        re.DOTALL,
    )
    XML_ATTR_RE = re.compile(
        r"<(?P<tag>[A-Za-z_][A-Za-z0-9_.:-]*)\b(?P<attrs>[^>]*)>",
        re.DOTALL,
    )
    XML_ATTR_VALUE_RE = re.compile(
        r"(?P<name>[A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*(['\"])(?P<value>.*?)\2",
        re.DOTALL,
    )
    FINDER_MARKERS = (
        "<finder",
        "finderliveproductshare",
        "findernamecard",
        "finderfeed",
        "finderusername",
        "objectid",
        "objectnonceid",
    )
    FINDER_DEBUG_FIELD_RE = re.compile(
        r"(sph|short|share|finder|object|nonce|feed|export|url|username|nickname|title|desc|token|sign|decode)",
        re.IGNORECASE,
    )
    FINDER_MEDIA_HOSTS = {
        "wxapp.tc.qq.com",
        "finder.video.qq.com",
    }
    FINDER_VIDEO_PATH_MARKERS = (
        "/20302/stodownload",
        "/251/20302/stodownload",
    )

    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context

        self.config = self.load_config() or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.allow_group = bool(self.config.get("allow_group", True))
        self.allow_private = bool(self.config.get("allow_private", True))
        self.auto_detect = bool(self.config.get("auto_detect", True))
        self.send_summary = bool(self.config.get("send_summary", True))
        self.send_video = bool(self.config.get("send_video", True))
        self.send_fallback_text = bool(self.config.get("send_fallback_text", True))
        self.max_retries = int(self.config.get("max_retries", 2))
        self.timeout = max(5, int(self.config.get("timeout", 18)))
        self.video_max_size_mb = max(10, int(self.config.get("video_max_size_mb", 100)))
        self.try_compact_cdn = bool(self.config.get("try_compact_cdn", True))
        self.try_full_cdn = bool(self.config.get("try_full_cdn", True))
        self.finder_protocol_enabled = bool(
            self.config.get("finder_protocol_enabled", True)
        )
        self.finder_protocol_base = str(
            self.config.get("finder_protocol_base")
            or Config.WECHAT_API_BASE_URL
            or "http://127.0.0.1:9000/api"
        ).strip().rstrip("/")
        self.finder_protocol_timeout = max(
            3,
            int(self.config.get("finder_protocol_timeout", 15)),
        )
        self.finder_protocol_cgi = max(
            1,
            int(self.config.get("finder_protocol_cgi", 3906)),
        )
        self.finder_bridge_enabled = bool(
            self.config.get("finder_bridge_enabled", False)
        )
        self.finder_bridge_base = str(
            self.config.get("finder_bridge_base", "http://127.0.0.1:8790")
        ).strip().rstrip("/")
        self.finder_bridge_api_key = str(
            self.config.get("finder_bridge_api_key", "")
        ).strip()
        self.finder_bridge_timeout = max(
            2,
            int(self.config.get("finder_bridge_timeout", 30)),
        )

        self.api_base = str(self.config.get("api_base", "http://127.0.0.1:8787")).strip().rstrip("/")
        self.api_key = str(self.config.get("api_key", "")).strip()
        self.manual_triggers = [
            str(x).strip()
            for x in self.config.get("triggers", ["解析", "解析链接", "视频号解析"])
            if str(x).strip()
        ]

        self.cache_dir = os.path.join(str(Config.BASE_DIR), "tmp", "wxsph_parser")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.wechat_api = WechatAPIClient()

        logger.info(
            f"[WxSphParser] init enabled={self.enabled}, base={self.api_base}, "
            f"auto_detect={self.auto_detect}, send_video={self.send_video}, "
            f"try_full_cdn={self.try_full_cdn}, try_compact_cdn={self.try_compact_cdn}, "
            f"finder_protocol_enabled={self.finder_protocol_enabled}, "
            f"finder_protocol_base={self.finder_protocol_base or '-'}, "
            f"finder_bridge_enabled={self.finder_bridge_enabled}, "
            f"finder_bridge_base={self.finder_bridge_base or '-'}"
        )

    def on_handle_context(self, e_context: EventContext):
        if not self.enabled:
            return

        context = e_context.get("context")
        if not context:
            return

        is_group = bool(e_context.get("is_group", False))
        if is_group and not self.allow_group:
            return
        if (not is_group) and not self.allow_private:
            return

        content = str(e_context.get("content", "") or "")
        original_content = str(e_context.get("original_content", content) or "")
        outer_content = str(e_context.get("outer_content", "") or "")
        msg_type = getattr(context, "type", None)
        target_wxid = str(e_context.get("from_wxid", "") or "").strip() or str(e_context.get("to_wxid", "") or "").strip()
        if not target_wxid:
            return

        raw_text = self._normalize_text(content)
        original_text = self._normalize_text("\n".join(x for x in (original_content, outer_content) if x))
        source_blob = "\n".join(x for x in (original_content, outer_content, content) if x)

        if msg_type == ContextType.SHARING and not bool(e_context.get("quoted_link", False)):
            logger.debug("[WxSphParser] ignored non-quoted sharing link")
            return

        share_url, explicit = self._resolve_share_url(msg_type, raw_text, original_text)
        card_meta = self._extract_finder_card_meta(source_blob)
        media_candidates = self._build_media_candidates(source_blob)

        if card_meta.get("object_id") or media_candidates:
            logger.info(
                "[WxSphParser] card meta: "
                f"object_id={card_meta.get('object_id') or '-'}, "
                f"object_nonce_length={len(card_meta.get('object_nonce_id') or '')}, "
                f"nickname={self._shorten(card_meta.get('nickname') or '-', 30)}, "
                f"media_candidates={len(media_candidates)}, "
                f"share_url={bool(share_url)}"
            )

        if (
            not share_url
            and card_meta.get("object_id")
            and card_meta.get("object_nonce_id")
            and self.finder_bridge_enabled
        ):
            share_url = self._resolve_card_share_url(card_meta)
            if share_url:
                logger.info(
                    "[WxSphParser] resolved card objectId to sph URL via bridge: "
                    f"object_id={card_meta.get('object_id')}, url={share_url[:160]}"
                )

        protocol_error = ""
        if (
            not share_url
            and card_meta.get("object_id")
            and card_meta.get("object_nonce_id")
            and self.finder_protocol_enabled
        ):
            try:
                if self._handle_card_via_protocol(target_wxid, card_meta):
                    e_context.action = EventAction.BREAK_PASS
                    return
            except Exception as exc:
                protocol_error = str(exc)
                logger.warning(
                    "[WxSphParser] finder protocol refresh failed: "
                    f"object_id={card_meta.get('object_id')}, error={exc}"
                )

        if media_candidates and self.send_video and not share_url:
            try:
                if self._send_first_video(
                    target_wxid,
                    media_candidates,
                    share_url or "https://channels.weixin.qq.com/",
                ):
                    e_context.action = EventAction.BREAK_PASS
                    return
                raise RuntimeError(self._media_failure_message(card_meta, media_candidates))
            except Exception as exc:
                logger.error(f"[WxSphParser] direct media failed: {exc}", exc_info=True)
                if not share_url:
                    self._send_text(target_wxid, str(exc)[:180])
                    e_context.action = EventAction.BREAK_PASS
                    return

        if not share_url:
            if explicit:
                if protocol_error:
                    self._send_text(
                        target_wxid,
                        f"视频号详情刷新失败：{self._shorten(protocol_error, 120)}",
                    )
                elif self._looks_like_finder_card(original_text or raw_text) or card_meta.get("object_id"):
                    self._send_text(target_wxid, self._no_share_url_message(card_meta, media_candidates))
                else:
                    self._send_text(target_wxid, "请发送：解析 <微信视频号链接>")
                e_context.action = EventAction.BREAK_PASS
            return

        try:
            self._handle_share(target_wxid, share_url)
        except Exception as exc:
            logger.error(f"[WxSphParser] handle failed: {exc}", exc_info=True)
            self._send_text(target_wxid, f"视频号解析失败：{str(exc)[:120]}")
        finally:
            e_context.action = EventAction.BREAK_PASS

    def _resolve_share_url(self, msg_type: Any, text: str, original_text: str = "") -> tuple[str, bool]:
        if msg_type == ContextType.SHARING:
            url = self._extract_share_url(text) or self._extract_share_url(original_text)
            if not url:
                url = self._extract_finder_card_url(text, original_text)
                if url:
                    logger.info(f"[WxSphParser] resolved finder card url: {url[:160]}")
            return url, bool(url) or self._looks_like_finder_card(original_text or text)

        manual_remainder = self._match_trigger(text)
        if manual_remainder is not None:
            url = (
                self._extract_share_url(manual_remainder)
                or self._extract_share_url(text)
                or self._extract_share_url(original_text)
                or self._extract_finder_card_url(manual_remainder, original_text)
                or self._extract_finder_card_url(text, original_text)
            )
            return url, True

        if self.auto_detect:
            url = (
                self._extract_share_url(text)
                or self._extract_share_url(original_text)
                or self._extract_finder_card_url(text, original_text)
            )
            return url, bool(url)

        return "", False

    def _match_trigger(self, text: str) -> Optional[str]:
        for trigger in sorted(self.manual_triggers, key=len, reverse=True):
            if text == trigger:
                return ""
            if text.startswith(trigger + " "):
                return text[len(trigger):].strip()
            if text.startswith(trigger + "\n"):
                return text[len(trigger):].strip()
        return None

    def _extract_share_url(self, text: str) -> str:
        if not text:
            return ""
        for source in self._iter_decoded_sources(text):
            match = self.SHARE_URL_RE.search(source)
            if not match:
                continue
            url = self._clean_url(match.group(0))
            validated = self._validate_share_url(url)
            if validated:
                return validated
        return ""

    def _extract_finder_card_url(self, text: str, original_text: str = "") -> str:
        """Extract a public /sph/ URL only when the card actually contains one."""
        source_text = original_text or text
        if not self._looks_like_finder_card(source_text):
            return ""

        for source in self._iter_decoded_sources(text, original_text):
            url = self._extract_share_url(source)
            if url:
                return url

            code = self._extract_sph_code(source)
            if code:
                return self._build_sph_url(code)

            for match in self.ANY_URL_RE.finditer(source or ""):
                url = self._clean_url(match.group(0))
                nested_url = self._extract_share_url(self._decode_repeated(url))
                if nested_url:
                    return nested_url

                parsed = urlparse(url)
                query = parse_qs(parsed.query or "")
                for key, values in query.items():
                    key_norm = self._normalize_field_name(key)
                    for value in values:
                        decoded = self._decode_repeated(value)
                        nested_url = self._extract_share_url(decoded)
                        if nested_url:
                            return nested_url
                        # objectId is not a sph short code.
                        if key_norm in {"sph", "sphurl", "shorturl", "shortlink", "shareurl"} and self._is_sph_code(decoded):
                            return self._build_sph_url(decoded)

            for tag, value in self._iter_xml_fields(source):
                tag_norm = self._normalize_field_name(tag)
                value = self._decode_repeated(value)
                nested_url = self._extract_share_url(value)
                if nested_url:
                    return nested_url
                code = self._extract_sph_code(value)
                if code:
                    return self._build_sph_url(code)
                if tag_norm in {"sph", "sphurl", "shorturl", "shortlink", "shareurl"} and self._is_sph_code(value):
                    return self._build_sph_url(value)

        self._log_finder_card_fields(source_text)
        return ""

    def _extract_finder_card_meta(self, text: str) -> dict[str, str]:
        meta: dict[str, str] = {}
        if not text or not self._looks_like_finder_card(text):
            return meta

        for source in self._iter_decoded_sources(text):
            for tag, value in self._iter_xml_fields(source):
                key = self._normalize_field_name(tag)
                clean = self._clean_text(value)
                if not clean:
                    continue
                if key in {"objectid", "object_id"} and not meta.get("object_id"):
                    meta["object_id"] = clean
                elif key in {"objectnonceid", "object_nonce_id", "nonceid", "nonce_id"} and not meta.get("object_nonce_id"):
                    meta["object_nonce_id"] = clean
                elif key in {"finderusername", "username"} and not meta.get("username"):
                    meta["username"] = clean
                elif key == "nickname" and not meta.get("nickname"):
                    meta["nickname"] = clean
                elif key in {"desc", "description"} and not meta.get("desc"):
                    meta["desc"] = clean
                elif key == "feedtype" and not meta.get("feed_type"):
                    meta["feed_type"] = clean
            if meta.get("object_id") and meta.get("object_nonce_id"):
                break
        return meta

    def _build_media_candidates(self, text: str) -> list[str]:
        raw_urls = self._extract_finder_media_urls(text)
        candidates: list[str] = []
        seen: set[str] = set()

        def add(url: str) -> None:
            value = self._clean_url(url)
            if not value or value in seen:
                return
            seen.add(value)
            candidates.append(value)

        for raw_url in raw_urls:
            variants = self._normalize_finder_media_variants(raw_url)
            for item in variants:
                add(item)

        if candidates:
            logger.info(
                "[WxSphParser] media candidates prepared: "
                f"count={len(candidates)}, first_keys={self._url_query_keys(candidates[0])}"
            )
        return candidates

    def _extract_finder_media_urls(self, text: str) -> list[str]:
        if not text or not self._looks_like_finder_card(text):
            return []

        found: list[str] = []
        seen: set[str] = set()
        source_seen: set[str] = set()

        for raw_source in self._iter_decoded_sources(text):
            for candidate_source in (raw_source, html.unescape(raw_source)):
                if not candidate_source or candidate_source in source_seen:
                    continue
                source_seen.add(candidate_source)

                for match in re.finditer(
                    r"<url\b[^>]*>(.*?)</url>",
                    candidate_source,
                    re.IGNORECASE | re.DOTALL,
                ):
                    media_url = self._validate_finder_media_url(match.group(1), require_token=False)
                    if media_url and media_url not in seen:
                        seen.add(media_url)
                        found.append(media_url)

                for match in self.ANY_URL_RE.finditer(candidate_source):
                    media_url = self._validate_finder_media_url(match.group(0), require_token=False)
                    if media_url and media_url not in seen:
                        seen.add(media_url)
                        found.append(media_url)
        return found

    def _normalize_finder_media_variants(self, value: str) -> list[str]:
        validated = self._validate_finder_media_url(value, require_token=True)
        if not validated:
            incomplete = self._validate_finder_media_url(value, require_token=False)
            if incomplete:
                logger.warning(
                    f"[WxSphParser] skip incomplete media url: "
                    f"length={len(incomplete)}, keys={self._url_query_keys(incomplete)}"
                )
            return []

        variants: list[str] = []
        compact = self._compact_finder_media_url(validated) if self.try_compact_cdn else ""
        if compact:
            variants.append(compact)
        if self.try_full_cdn and validated not in variants:
            variants.append(validated)
        if not variants and validated:
            variants.append(validated)
        return variants

    def _compact_finder_media_url(self, url: str) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query or "", keep_blank_values=True)
        encfilekey = self._first_query_value(query, "encfilekey")
        token = self._first_query_value(query, "token")
        if not encfilekey or not token:
            return ""
        compact_query = urlencode({"encfilekey": encfilekey, "token": token}, safe="*")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", compact_query, ""))

    def _validate_finder_media_url(self, value: str, require_token: bool = True) -> str:
        url = html.unescape(str(value or "")).strip()
        url = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", url, flags=re.DOTALL).strip()
        url = self._clean_url(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return ""
        host = (parsed.hostname or "").lower()
        if host not in self.FINDER_MEDIA_HOSTS:
            return ""

        path = (parsed.path or "").lower()
        if not any(marker in path for marker in self.FINDER_VIDEO_PATH_MARKERS):
            return ""

        query = parse_qs(parsed.query or "", keep_blank_values=True)
        encfilekey = self._first_query_value(query, "encfilekey")
        token = self._first_query_value(query, "token")
        if not encfilekey:
            return ""
        if require_token and not token:
            logger.warning(
                f"[WxSphParser] incomplete finder media url: "
                f"length={len(url)}, missing=['token'], keys={sorted(query)}"
            )
            return ""
        return url

    def _first_query_value(self, query: dict[str, list[str]], key: str) -> str:
        values = query.get(key) or []
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _url_query_keys(self, url: str) -> list[str]:
        try:
            return sorted(parse_qs(urlparse(url).query or "", keep_blank_values=True))
        except Exception:
            return []

    def _media_failure_message(self, card_meta: dict[str, str], media_candidates: list[str]) -> str:
        object_id = card_meta.get("object_id") or "-"
        nonce_length = len(card_meta.get("object_nonce_id") or "")
        if media_candidates:
            return (
                "视频号直链下载失败（链接可能已过期或内容加密）。"
                f" objectId={object_id}, nonceLength={nonce_length}。"
                "当前卡片没有可用的 sph 短链，需要详情刷新接口才能继续。"
            )
        return (
            "视频号卡片未提取到可下载直链。"
            f" objectId={object_id}, nonceLength={nonce_length}。"
            "请改发 weixin.qq.com/sph 链接，或等待详情刷新能力接入。"
        )

    def _no_share_url_message(self, card_meta: dict[str, str], media_candidates: list[str]) -> str:
        object_id = card_meta.get("object_id") or "-"
        nonce_length = len(card_meta.get("object_nonce_id") or "")
        if media_candidates:
            return (
                "已提取到视频 CDN，但下载失败或文件不可用（常见原因：链接过期/缺 decodeKey）。"
                f" objectId={object_id}, nonceLength={nonce_length}。"
            )
        return (
            "视频号卡片里没有 sph 短链，也没有带 token 的可下载视频直链。"
            f" objectId={object_id}, nonceLength={nonce_length}。"
            "请重新引用新卡片，或发送 weixin.qq.com/sph 链接。"
        )

    def _iter_decoded_sources(self, *values: str):
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            stack = [str(value)]
            while stack:
                item = stack.pop()
                if not item or item in seen:
                    continue
                seen.add(item)
                yield item
                decoded = self._decode_repeated(item)
                if decoded and decoded not in seen:
                    stack.append(decoded)

    def _decode_repeated(self, value: str) -> str:
        text = html.unescape(str(value or "")).strip()
        text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
        for _ in range(3):
            decoded = unquote(text)
            decoded = html.unescape(decoded)
            if decoded == text:
                break
            text = decoded
        return text.strip()

    def _extract_sph_code(self, text: str) -> str:
        if not text:
            return ""
        decoded = self._decode_repeated(text)
        match = self.SPH_PATH_RE.search(decoded)
        if match:
            return match.group(1)
        return ""

    def _build_sph_url(self, code: str) -> str:
        return f"https://weixin.qq.com/sph/{code.strip()}"

    def _is_sph_code(self, value: str) -> bool:
        code = str(value or "").strip()
        return bool(self.SPH_CODE_RE.fullmatch(code) and re.search(r"[A-Za-z]", code))

    def _iter_xml_fields(self, text: str):
        for xml_text in self._iter_xml_documents(text):
            try:
                root = ET.fromstring(xml_text)
                for elem in root.iter():
                    tag = str(elem.tag or "").split("}", 1)[-1]
                    for attr_name, attr_value in elem.attrib.items():
                        value = self._decode_repeated(attr_value)
                        if tag and attr_name and value:
                            yield f"{tag}.{attr_name}", value
                    value = self._decode_repeated("".join(elem.itertext()) if elem.text is None else elem.text)
                    if tag and value:
                        yield tag, value
                return
            except ET.ParseError:
                pass

        for match in self.XML_FIELD_RE.finditer(text or ""):
            tag = match.group("tag") or ""
            value = self._decode_repeated(match.group("value") or "")
            if tag and value:
                yield tag, value
        for match in self.XML_ATTR_RE.finditer(text or ""):
            tag = match.group("tag") or ""
            attrs = match.group("attrs") or ""
            for attr in self.XML_ATTR_VALUE_RE.finditer(attrs):
                attr_name = attr.group("name") or ""
                value = self._decode_repeated(attr.group("value") or "")
                if tag and attr_name and value:
                    yield f"{tag}.{attr_name}", value

    def _iter_xml_documents(self, text: str):
        raw = str(text or "").strip()
        candidates = [raw]
        unescaped = html.unescape(raw)
        if unescaped != raw:
            candidates.append(unescaped)
        unquoted = unquote(raw)
        if unquoted not in candidates:
            candidates.append(unquoted)

        seen: set[str] = set()
        for value in candidates:
            if not value or value in seen:
                continue
            seen.add(value)
            xml_doc = self._extract_xml_document(value)
            if xml_doc:
                yield xml_doc

    def _extract_xml_document(self, value: str) -> str:
        if "<?xml" in value:
            value = value[value.find("<?xml") :]
        elif "<msg" in value:
            value = value[value.find("<msg") :]
        elif "<appmsg" in value:
            value = value[value.find("<appmsg") :]
        else:
            return ""
        for end_tag in ("</msg>", "</appmsg>"):
            end_index = value.rfind(end_tag)
            if end_index >= 0:
                return value[: end_index + len(end_tag)]
        return value

    def _normalize_field_name(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    def _log_finder_card_fields(self, text: str) -> None:
        fields: list[str] = []
        for tag, value in self._iter_xml_fields(text):
            if not self.FINDER_DEBUG_FIELD_RE.search(tag):
                continue
            clean_value = self._clean_text(value)
            if not clean_value:
                continue
            normalized_tag = self._normalize_field_name(tag)
            if normalized_tag in {
                "nonceid",
                "objectnonce",
                "objectnonceid",
            }:
                fields.append(f"{tag}=<redacted:{len(clean_value)}>")
            else:
                fields.append(f"{tag}={self._shorten(clean_value, 80)}")
            if len(fields) >= 24:
                break
        if fields:
            logger.info(f"[WxSphParser] finder card fields: {'; '.join(fields)}")
        else:
            logger.info("[WxSphParser] finder card detected, no useful sph fields found")

    def _looks_like_finder_card(self, text: str) -> bool:
        compact = (text or "").lower()
        return any(marker in compact for marker in self.FINDER_MARKERS)

    def _clean_url(self, url: str) -> str:
        return str(url or "").strip().rstrip("】）)】>。，,;；\"'`")

    def _validate_share_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return ""
        host = (parsed.netloc or "").lower()
        if host not in {"weixin.qq.com", "mp.weixin.qq.com"}:
            return ""
        if "/sph/" not in parsed.path:
            return ""
        return url

    def _handle_share(self, target_wxid: str, share_url: str) -> None:
        payload = self._fetch_share_payload(share_url)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data:
            raise RuntimeError(str(payload.get("msg") or "empty response"))

        summary = self._build_summary(data, share_url)
        if self.send_summary and summary:
            try:
                self._send_text(target_wxid, summary)
            except Exception as exc:
                logger.warning(f"[WxSphParser] summary send failed: {exc}")

        video_urls = self._collect_video_urls(data)
        if self.send_video:
            sent = self._send_first_video(target_wxid, video_urls, share_url)
            if sent:
                return

        if self.send_fallback_text:
            try:
                self._send_text(target_wxid, self._build_fallback_text(data, share_url, video_urls))
            except Exception as exc:
                logger.warning(f"[WxSphParser] fallback text send failed: {exc}")

    def _handle_card_via_protocol(
        self,
        target_wxid: str,
        card_meta: dict[str, str],
    ) -> bool:
        detail = self._fetch_finder_card_detail(card_meta)
        video_urls = self._collect_finder_detail_video_urls(detail)
        if self.send_video:
            if not video_urls:
                raise RuntimeError("protocol response has no downloadable video URL")
            if not self._send_first_video(
                target_wxid,
                video_urls,
                "https://channels.weixin.qq.com/",
            ):
                raise RuntimeError("refreshed video download or send failed")

        if self.send_summary:
            summary = self._build_finder_detail_summary(detail, card_meta)
            if summary:
                try:
                    self._send_text(target_wxid, summary)
                except Exception as exc:
                    logger.warning(f"[WxSphParser] card summary send failed: {exc}")

        logger.info(
            "[WxSphParser] finder card handled via protocol: "
            f"object_id={card_meta.get('object_id')}, media_count={len(video_urls)}"
        )
        return bool(video_urls or self.send_summary)

    def _fetch_finder_card_detail(
        self,
        card_meta: dict[str, str],
    ) -> dict[str, Any]:
        object_id = self._clean_text(card_meta.get("object_id"))
        object_nonce_id = self._clean_text(card_meta.get("object_nonce_id"))
        if not object_id or not object_nonce_id:
            raise RuntimeError("missing objectId or objectNonceId")

        bot_wxid = self._clean_text(
            Config.CURRENT_BOT_WXID or Config.EXPECTED_BOT_WXID
        )
        if not bot_wxid:
            bot_wxid = self._clean_text(
                self.wechat_api.get_current_login_wxid() or ""
            )
        if not bot_wxid:
            raise RuntimeError("current bot wxid is unavailable")

        endpoint = f"{self.finder_protocol_base}/Finder/GetCommentDetail"
        resp = requests.post(
            endpoint,
            data={
                "wxid": bot_wxid,
                "objectId": object_id,
                "objectNonceId": object_nonce_id,
                "cgi": str(self.finder_protocol_cgi),
            },
            headers={"Accept": "application/json"},
            timeout=self.finder_protocol_timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"protocol HTTP {resp.status_code}: {(resp.text or '')[:180]}"
            )

        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("invalid protocol JSON payload")
        code = payload.get("Code", payload.get("code"))
        success = payload.get("Success", payload.get("success"))
        if success is False or code not in (None, 0, 200, "0", "200"):
            message = self._clean_text(
                payload.get("Message")
                or payload.get("message")
                or "finder detail request failed"
            )
            raise RuntimeError(message)

        detail = payload.get("Data", payload.get("data"))
        if not isinstance(detail, dict):
            raise RuntimeError("protocol response has no detail data")

        urls = self._collect_finder_detail_video_urls(detail)
        logger.info(
            "[WxSphParser] finder protocol refreshed media: "
            f"object_id={object_id}, count={len(urls)}, "
            f"first_keys={self._url_query_keys(self._video_candidate_url(urls[0])) if urls else []}"
        )
        return detail

    def _collect_finder_detail_video_urls(
        self,
        detail: dict[str, Any],
    ) -> list[dict[str, str]]:
        media_items = detail.get("media")
        if not isinstance(media_items, list):
            return []

        urls: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in media_items:
            if not isinstance(item, dict):
                continue
            full_url = self._clean_text(
                item.get("full_url")
                or item.get("fullUrl")
                or item.get("FullURL")
            )
            if not full_url:
                base_url = self._clean_text(
                    item.get("url")
                    or item.get("URL")
                )
                url_token = self._clean_text(
                    item.get("url_token")
                    or item.get("urlToken")
                    or item.get("URLToken")
                )
                if base_url and url_token:
                    full_url = base_url + url_token

            validated = self._validate_finder_media_url(
                full_url,
                require_token=True,
            )
            if not validated or validated in seen:
                continue
            seen.add(validated)
            decode_key = self._clean_text(
                item.get("decode_key")
                or item.get("decodeKey")
                or item.get("decrypt_key")
                or item.get("decryptKey")
            )
            urls.append({"url": validated, "decode_key": decode_key})
        return urls

    def _build_finder_detail_summary(
        self,
        detail: dict[str, Any],
        card_meta: dict[str, str],
    ) -> str:
        title = self._clean_text(
            detail.get("title")
            or card_meta.get("desc")
            or "视频号内容"
        )
        nickname = self._clean_text(
            detail.get("nickname")
            or card_meta.get("nickname")
        )
        lines = [f"标题：{self._shorten(title, 100)}"]
        if nickname:
            lines.append(f"作者：{self._shorten(nickname, 60)}")
        return "\n".join(lines)

    def _resolve_card_share_url(self, card_meta: dict[str, str]) -> str:
        object_id = self._clean_text(card_meta.get("object_id"))
        object_nonce_id = self._clean_text(
            card_meta.get("object_nonce_id")
        )
        if (
            not object_id
            or not object_nonce_id
            or not self.finder_bridge_base
        ):
            return ""

        endpoint = f"{self.finder_bridge_base}/api/v1/finder/share-url"
        headers = {"Accept": "application/json"}
        if self.finder_bridge_api_key:
            headers["X-API-Key"] = self.finder_bridge_api_key

        try:
            resp = requests.post(
                endpoint,
                json={
                    "object_id": object_id,
                    "object_nonce_id": object_nonce_id,
                    "scene": 40,
                },
                headers=headers,
                timeout=self.finder_bridge_timeout,
            )
            if resp.status_code != 200:
                detail = (resp.text or "")[:240]
                try:
                    error_payload = resp.json()
                    if isinstance(error_payload, dict):
                        detail = self._clean_text(
                            error_payload.get("detail")
                            or error_payload.get("msg")
                            or detail
                        )
                except ValueError:
                    pass
                raise RuntimeError(f"HTTP {resp.status_code}: {detail}")

            payload = resp.json()
            if not isinstance(payload, dict):
                raise RuntimeError("invalid JSON payload")
            if payload.get("code") not in (None, 0, 200, "0", "200"):
                raise RuntimeError(
                    self._clean_text(payload.get("msg")) or "bridge resolve failed"
                )

            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("bridge response has no data")
            share_url = self._validate_share_url(
                self._clean_text(
                    data.get("sph_url")
                    or data.get("feedH5Url")
                    or data.get("share_url")
                )
            )
            if not share_url:
                raise RuntimeError("bridge response has no valid sph URL")
            return share_url
        except Exception as exc:
            logger.warning(
                "[WxSphParser] finder bridge share-url failed: "
                f"object_id={object_id}, error={exc}"
            )
            return ""

    def _fetch_share_payload(self, share_url: str) -> dict[str, Any]:
        endpoint = f"{self.api_base}/api/wxsph"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.get(
                    endpoint,
                    params={"url": share_url},
                    headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:160]}")
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("invalid json payload")
                code = payload.get("code")
                if code not in (0, 200, "0", "200"):
                    raise RuntimeError(str(payload.get("msg") or "parse failed"))
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                break
        raise RuntimeError(str(last_error) if last_error else "parse failed")

    def _build_summary(self, data: dict[str, Any], share_url: str) -> str:
        title = self._first_str(data, "title", "desc") or "视频号内容"
        author = ""
        author_info = data.get("author")
        if isinstance(author_info, dict):
            author = self._first_str(author_info, "name", "nickname", "username")
        elif author_info is not None:
            author = self._clean_text(author_info)

        desc = self._first_str(data, "desc", "title")
        quality = self._clean_text(data.get("quality"))

        lines = [f"标题：{self._shorten(title, 80)}"]
        if author:
            lines.append(f"作者：{self._shorten(author, 60)}")
        if desc and desc != title:
            lines.append(f"简介：{self._shorten(desc, 120)}")
        if quality:
            lines.append(f"清晰度：{quality}")
        lines.append(f"链接：{share_url}")
        return "\n".join(lines)

    def _build_fallback_text(self, data: dict[str, Any], share_url: str, video_urls: list[str]) -> str:
        title = self._first_str(data, "title", "desc") or "视频号内容"
        lines = [f"标题：{self._shorten(title, 80)}", f"链接：{share_url}"]
        if video_urls:
            lines.append(f"播放：{video_urls[0]}")
        return "\n".join(lines)

    def _collect_video_urls(self, data: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            url = self._clean_text(value)
            if not url.startswith("http"):
                return
            if url in seen:
                return
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return
            seen.add(url)
            candidates.append(url)

        for key in ("url", "play_url", "video_url", "origin_url", "download_url"):
            add(data.get(key))

        backups = data.get("video_backup")
        if isinstance(backups, list):
            for item in backups:
                if not isinstance(item, dict):
                    continue
                for key in ("url", "play_url", "video_url", "download_url"):
                    add(item.get(key))

        expanded: list[str] = []
        expanded_seen: set[str] = set()
        for url in candidates:
            variants = self._normalize_finder_media_variants(url)
            for item in variants or [url]:
                if item in expanded_seen:
                    continue
                expanded_seen.add(item)
                expanded.append(item)
        return expanded or candidates

    def _send_first_video(self, target_wxid: str, video_urls: list[Any], share_url: str) -> bool:
        for index, candidate in enumerate(video_urls):
            video_url, decode_key = self._video_candidate_parts(candidate)
            if not video_url:
                continue
            video_path = ""
            try:
                logger.info(
                    f"[WxSphParser] try media candidate #{index + 1}/{len(video_urls)}: "
                    f"length={len(video_url)}, keys={self._url_query_keys(video_url)}, "
                    f"decode_key={bool(decode_key)}"
                )
                video_path = download_video(
                    video_url,
                    self.cache_dir,
                    timeout=max(self.timeout, 30),
                    max_size_mb=self.video_max_size_mb,
                    prefix="wxsph_",
                    referer=share_url,
                )
                if not video_path:
                    continue
                if decode_key and not self._is_probable_video_file(video_path):
                    self._decrypt_finder_video_file(video_path, decode_key)
                if not self._is_probable_video_file(video_path):
                    logger.warning(
                        f"[WxSphParser] downloaded file is not a recognized video: "
                        f"{video_url[:120]}"
                    )
                    continue
                logger.info(
                    f"[WxSphParser] video download verified: "
                    f"path={video_path}, size={os.path.getsize(video_path)}"
                )
                try:
                    self.wechat_api.send_video_msg(
                        wxid=target_wxid,
                        video_path=video_path,
                        http_port=Config.WECHAT_HTTP_PORT,
                    )
                    logger.info(f"[WxSphParser] send video ok: {video_url[:120]}")
                    return True
                except Exception as video_err:
                    logger.warning(f"[WxSphParser] send video failed, fallback to file: {video_err}")
                    self.wechat_api.send_file_msg(
                        wxid=target_wxid,
                        file_path=video_path,
                        http_port=Config.WECHAT_HTTP_PORT,
                    )
                    return True
            except Exception as exc:
                logger.warning(f"[WxSphParser] download/send failed: {exc}")
            finally:
                self._remove_file(video_path)
        return False

    def _video_candidate_url(self, candidate: Any) -> str:
        return self._video_candidate_parts(candidate)[0]

    def _video_candidate_parts(self, candidate: Any) -> tuple[str, str]:
        if isinstance(candidate, dict):
            return (
                self._clean_text(
                    candidate.get("url")
                    or candidate.get("full_url")
                    or candidate.get("video_url")
                ),
                self._clean_text(
                    candidate.get("decode_key")
                    or candidate.get("decodeKey")
                    or candidate.get("decrypt_key")
                    or candidate.get("decryptKey")
                    or candidate.get("key")
                ),
            )
        return self._clean_text(candidate), ""

    def _decrypt_finder_video_file(self, file_path: str, decode_key: str) -> None:
        key_text = self._clean_text(decode_key)
        if not key_text:
            return
        try:
            key = int(key_text, 10)
        except ValueError as exc:
            raise RuntimeError(f"invalid decodeKey: {key_text}") from exc
        if key <= 0:
            return

        with open(file_path, "r+b") as file_obj:
            data = bytearray(file_obj.read(131072))
            if not data:
                return
            self._finder_isaac64_xor(data, key)
            file_obj.seek(0)
            file_obj.write(data)
        logger.info(
            f"[WxSphParser] decrypted finder video prefix: "
            f"path={file_path}, bytes={len(data)}"
        )

    def _finder_isaac64_xor(self, data: bytearray, key: int) -> None:
        seed, mm = [0] * 256, [0] * 256
        seed[0] = self._u64(key)
        aa = bb = cc = 0
        golden = 0x9E3779B97F4A7C13
        a = b = c = d = e = f = g = h = golden

        for _ in range(4):
            a, b, c, d, e, f, g, h = self._isaac64_mix(a, b, c, d, e, f, g, h)

        for i in range(0, 256, 8):
            a, b, c, d, e, f, g, h = (
                self._u64(a + seed[i]),
                self._u64(b + seed[i + 1]),
                self._u64(c + seed[i + 2]),
                self._u64(d + seed[i + 3]),
                self._u64(e + seed[i + 4]),
                self._u64(f + seed[i + 5]),
                self._u64(g + seed[i + 6]),
                self._u64(h + seed[i + 7]),
            )
            a, b, c, d, e, f, g, h = self._isaac64_mix(a, b, c, d, e, f, g, h)
            mm[i : i + 8] = [a, b, c, d, e, f, g, h]

        for i in range(0, 256, 8):
            a, b, c, d, e, f, g, h = (
                self._u64(a + mm[i]),
                self._u64(b + mm[i + 1]),
                self._u64(c + mm[i + 2]),
                self._u64(d + mm[i + 3]),
                self._u64(e + mm[i + 4]),
                self._u64(f + mm[i + 5]),
                self._u64(g + mm[i + 6]),
                self._u64(h + mm[i + 7]),
            )
            a, b, c, d, e, f, g, h = self._isaac64_mix(a, b, c, d, e, f, g, h)
            mm[i : i + 8] = [a, b, c, d, e, f, g, h]

        seed, aa, bb, cc = self._isaac64(seed, mm, aa, bb, cc)
        randcnt = 255

        for offset in range(0, len(data), 8):
            rand_number = seed[randcnt]
            if randcnt == 0:
                seed, aa, bb, cc = self._isaac64(seed, mm, aa, bb, cc)
                randcnt = 255
            else:
                randcnt -= 1
            block = rand_number.to_bytes(8, "big")
            for pos, value in enumerate(block):
                index = offset + pos
                if index >= len(data):
                    return
                data[index] ^= value

    def _isaac64(self, seed: list[int], mm: list[int], aa: int, bb: int, cc: int) -> tuple[list[int], int, int, int]:
        cc = self._u64(cc + 1)
        bb = self._u64(bb + cc)
        for i in range(256):
            x = mm[i]
            if i % 4 == 0:
                aa = self._u64(~(aa ^ self._u64(aa << 21)))
            elif i % 4 == 1:
                aa = self._u64(aa ^ (aa >> 5))
            elif i % 4 == 2:
                aa = self._u64(aa ^ self._u64(aa << 12))
            else:
                aa = self._u64(aa ^ (aa >> 33))
            aa = self._u64(aa + mm[(i + 128) % 256])
            y = self._u64(mm[(x >> 3) % 256] + aa + bb)
            mm[i] = y
            bb = self._u64(mm[(y >> 11) % 256] + x)
            seed[i] = bb
        return seed, aa, bb, cc

    def _isaac64_mix(
        self,
        a: int,
        b: int,
        c: int,
        d: int,
        e: int,
        f: int,
        g: int,
        h: int,
    ) -> tuple[int, int, int, int, int, int, int, int]:
        a = self._u64(a - e)
        f = self._u64(f ^ (h >> 9))
        h = self._u64(h + a)
        b = self._u64(b - f)
        g = self._u64(g ^ self._u64(a << 9))
        a = self._u64(a + b)
        c = self._u64(c - g)
        h = self._u64(h ^ (b >> 23))
        b = self._u64(b + c)
        d = self._u64(d - h)
        a = self._u64(a ^ self._u64(c << 15))
        c = self._u64(c + d)
        e = self._u64(e - a)
        b = self._u64(b ^ (d >> 14))
        d = self._u64(d + e)
        f = self._u64(f - b)
        c = self._u64(c ^ self._u64(e << 20))
        e = self._u64(e + f)
        g = self._u64(g - c)
        d = self._u64(d ^ (f >> 17))
        f = self._u64(f + g)
        h = self._u64(h - d)
        e = self._u64(e ^ self._u64(g << 14))
        g = self._u64(g + h)
        return a, b, c, d, e, f, g, h

    def _u64(self, value: int) -> int:
        return value & self.U64_MASK

    def _is_probable_video_file(self, file_path: str) -> bool:
        try:
            if not file_path or os.path.getsize(file_path) < 1024:
                return False
            with open(file_path, "rb") as file_obj:
                header = file_obj.read(64)
            return (
                b"ftyp" in header
                or header.startswith(b"\x1a\x45\xdf\xa3")
                or header.startswith(b"RIFF")
                or header.startswith(b"FLV")
            )
        except OSError:
            return False

    def _send_text(self, target_wxid: str, content: str) -> None:
        text = self._clean_text(content)
        if not target_wxid or not text:
            return
        self.wechat_api.send_text_msg(
            wxid=target_wxid,
            content=text,
            http_port=Config.WECHAT_HTTP_PORT,
        )

    def _remove_file(self, file_path: str) -> None:
        if not file_path:
            return
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

    def _first_str(self, data: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = data.get(key)
            text = self._clean_text(value)
            if text:
                return text
        return ""

    def _shorten(self, text: str, limit: int) -> str:
        value = self._clean_text(text)
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 3)].rstrip() + "..."

    def _clean_text(self, value: Any) -> str:
        text = html.unescape("" if value is None else str(value))
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _normalize_text(self, value: Any) -> str:
        text = self._clean_text(value)
        text = re.sub(r"^@\S+[\u2005\u00a0\s]*", "", text).strip()
        return text
