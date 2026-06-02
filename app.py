#!/usr/bin/env python3
"""
Feishu webhook gateway for Jenkins and Prometheus Alertmanager notifications.

The service accepts a simple Jenkins-friendly JSON payload or a Prometheus
Alertmanager webhook payload and forwards a formatted Feishu custom bot message
to a Feishu group webhook.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


LOG = logging.getLogger("feishu-webhook-gateway")


SUCCESS_STATES = {"success", "succeeded", "ok", "pass", "passed", "stable"}
FAILED_STATES = {"failure", "failed", "error", "unstable", "aborted"}
ALERTMANAGER_FIRING_STATES = {"firing"}
ALERTMANAGER_RESOLVED_STATES = {"resolved"}


@dataclass(frozen=True)
class Target:
    name: str
    webhook_url: str
    secret: str | None = None


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    request_token: str | None
    target: Target
    timeout_seconds: float


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def load_config(path: str | None = None) -> AppConfig:
    file_config: dict[str, Any] = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            file_config = json.load(f)

    host = str(file_config.get("host") or _env("HOST", "0.0.0.0"))
    port = int(file_config.get("port") or _env("PORT", "8008"))
    request_token = file_config.get("request_token") or _env("GATEWAY_TOKEN")
    timeout_seconds = float(file_config.get("timeout_seconds") or _env("TIMEOUT_SECONDS", "8"))

    webhook_url = file_config.get("webhook_url") or _env("FEISHU_WEBHOOK_URL")
    secret = file_config.get("secret") or _env("FEISHU_SECRET")
    if not webhook_url:
        raise ValueError(
            "No Feishu webhook configured. Set webhook_url in config JSON or FEISHU_WEBHOOK_URL."
        )

    return AppConfig(
        host=host,
        port=port,
        request_token=request_token,
        target=Target(name="default", webhook_url=str(webhook_url), secret=secret),
        timeout_seconds=timeout_seconds,
    )


def normalize_status(value: Any) -> str:
    status = str(value or "unknown").strip()
    return status or "unknown"


def status_color(status: str) -> str:
    lowered = status.lower()
    if lowered in SUCCESS_STATES:
        return "green"
    if lowered in FAILED_STATES:
        return "red"
    if "progress" in lowered or "running" in lowered:
        return "blue"
    return "orange"


def status_icon(status: str) -> str:
    lowered = status.lower()
    if lowered in SUCCESS_STATES:
        return "✅"
    if lowered in FAILED_STATES:
        return "❌"
    if "progress" in lowered or "running" in lowered:
        return "🚀"
    return "⚠️"


def alert_status_template(status: str) -> str:
    lowered = status.lower()
    if lowered in ALERTMANAGER_FIRING_STATES:
        return "red"
    if lowered in ALERTMANAGER_RESOLVED_STATES:
        return "green"
    return "orange"


def alert_status_icon(status: str) -> str:
    lowered = status.lower()
    if lowered in ALERTMANAGER_FIRING_STATES:
        return "🚨"
    if lowered in ALERTMANAGER_RESOLVED_STATES:
        return "✅"
    return "⚠️"


def field(payload: dict[str, Any], *names: str, default: str = "-") -> str:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def div_text_element(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def note_element(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def hr_element() -> dict[str, Any]:
    return {"tag": "hr"}


def feishu_text(value: Any, default: str = "-") -> str:
    text = str(value if value not in (None, "") else default)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def labels_to_text(labels: dict[str, Any], skip: set[str] | None = None) -> str:
    skip = skip or set()
    parts = []
    for key in sorted(labels):
        if key in skip:
            continue
        value = labels.get(key)
        if value not in (None, ""):
            parts.append(f"`{key}`={feishu_text(value)}")
    return "  ".join(parts) if parts else "-"


def first_present(*values: Any, default: str = "-") -> str:
    for value in values:
        if value not in (None, ""):
            return feishu_text(value)
    return default


def is_alertmanager_payload(payload: dict[str, Any]) -> bool:
    alerts = payload.get("alerts")
    return isinstance(alerts, list) and (
        "groupLabels" in payload
        or "commonLabels" in payload
        or any(isinstance(alert, dict) and "labels" in alert for alert in alerts)
    )


def build_feishu_message(payload: dict[str, Any]) -> dict[str, Any]:
    if is_alertmanager_payload(payload):
        return build_alertmanager_feishu_message(payload)
    return build_jenkins_feishu_message(payload)


def build_jenkins_feishu_message(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "发版通知")
    project = str(payload.get("project") or "Jenkins")
    card_title = f"{title} - {project}"
    template = "blue"
    if "完成" in title:
        template = "green"
    elif "失败" in title:
        template = "red"
    elif "停止" in title:
        template = "orange"
    elif "开始" in title:
        template = "blue"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": card_title},
            },
            "elements": [],
        },
    }


def build_alertmanager_feishu_message(payload: dict[str, Any]) -> dict[str, Any]:
    alerts = [alert for alert in payload.get("alerts", []) if isinstance(alert, dict)]
    status = normalize_status(payload.get("status") or first_present(*(alert.get("status") for alert in alerts)))
    common_labels = payload.get("commonLabels") if isinstance(payload.get("commonLabels"), dict) else {}
    group_labels = payload.get("groupLabels") if isinstance(payload.get("groupLabels"), dict) else {}
    common_annotations = (
        payload.get("commonAnnotations") if isinstance(payload.get("commonAnnotations"), dict) else {}
    )

    alert_name = first_present(
        common_labels.get("alertname"),
        group_labels.get("alertname"),
        default="Prometheus 告警",
    )
    severity = first_present(common_labels.get("severity"), default="-")
    instance = first_present(common_labels.get("instance"), common_labels.get("job"), default="-")
    summary = first_present(
        common_annotations.get("summary"),
        common_annotations.get("description"),
        default="",
    )

    title = f"{alert_status_icon(status)} [{status.upper()}] {alert_name}"
    if len(alerts) > 1:
        title = f"{title} x{len(alerts)}"

    elements: list[dict[str, Any]] = [
        div_text_element(
            "\n".join(
                [
                    f"**告警名称：** {alert_name}",
                    f"**状态：** {status}",
                    f"**级别：** {severity}",
                    f"**实例/任务：** {instance}",
                ]
            )
        )
    ]

    if summary:
        elements.append(div_text_element(f"**摘要：** {summary}"))

    if group_labels:
        elements.append(div_text_element(f"**分组标签：** {labels_to_text(group_labels)}"))

    max_alerts = 8
    for index, alert in enumerate(alerts[:max_alerts], start=1):
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
        alert_status = normalize_status(alert.get("status") or status)
        alert_summary = first_present(
            annotations.get("summary"),
            annotations.get("description"),
            labels.get("alertname"),
            default=f"告警 {index}",
        )

        detail_lines = [
            f"**#{index} {alert_summary}**",
            f"状态：{alert_status}",
            f"开始：{first_present(alert.get('startsAt'), default='-')}",
        ]
        if alert_status.lower() in ALERTMANAGER_RESOLVED_STATES or alert.get("endsAt"):
            detail_lines.append(f"结束：{first_present(alert.get('endsAt'), default='-')}")
        detail_lines.append(f"标签：{labels_to_text(labels, skip={'alertname'})}")

        generator_url = alert.get("generatorURL")
        if generator_url:
            detail_lines.append(f"[查看 Prometheus 详情]({feishu_text(generator_url)})")

        elements.append(hr_element())
        elements.append(div_text_element("\n".join(detail_lines)))

    remaining = len(alerts) - max_alerts
    if remaining > 0:
        elements.append(note_element(f"还有 {remaining} 条告警未展示，请在 Alertmanager 中查看完整列表。"))

    external_url = payload.get("externalURL")
    if external_url:
        elements.append(hr_element())
        elements.append(div_text_element(f"[打开 Alertmanager]({feishu_text(external_url)})"))

    if payload.get("truncatedAlerts"):
        elements.append(note_element(f"Alertmanager 已截断 {payload.get('truncatedAlerts')} 条告警。"))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": alert_status_template(status),
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def sign_feishu_payload(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def post_to_feishu(target: Target, message: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = dict(message)
    if target.secret:
        timestamp = int(time.time())
        body["timestamp"] = str(timestamp)
        body["sign"] = sign_feishu_payload(target.secret, timestamp)

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        target.webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            parsed = json.loads(response_body) if response_body else {}
            return {"status_code": response.status, "body": parsed}
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu webhook returned HTTP {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call Feishu webhook: {exc.reason}") from exc


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "FeishuWebhookGateway/1.0"
    config: AppConfig

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.write_json({"ok": True, "service": "feishu-webhook-gateway"})
            return
        self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/notify", "/webhook", "/"}:
            self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        if not self.is_authorized():
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        try:
            payload = self.read_json_body()
            feishu_message = build_feishu_message(payload)
            post_to_feishu(self.config.target, feishu_message, self.config.timeout_seconds)
            self.write_json({"ok": True})
        except ValueError as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.exception("request failed")
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def is_authorized(self) -> bool:
        if not self.config.request_token:
            return True
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {self.config.request_token}"
        return hmac.compare_digest(authorization, expected)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("empty request body")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GatewayServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], config: AppConfig):
        super().__init__(server_address, GatewayHandler)
        self.config = config
        GatewayHandler.config = config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jenkins/Alertmanager to Feishu webhook gateway")
    parser.add_argument("-c", "--config", help="Path to JSON config file")
    parser.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config(args.config)
    server = GatewayServer((config.host, config.port), config)
    LOG.info("listening on http://%s:%s", config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
