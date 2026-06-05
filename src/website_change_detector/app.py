"""网页监控脚本，发现内容变动->邮件提醒"""

import hashlib
import json
import logging
import signal
import smtplib
import sys
import time as _time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qsl, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from apscheduler.schedulers.blocking import BlockingScheduler

from . import __version__

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
]

# Simple TTL cache for WBI signing key (valid ~30 min)
_wbi_key_cache = {"key": None, "expires": 0}


# ── config & state ───────────────────────────────────────────────────────

def load_config():
    search_paths = [
        Path("config.json"),
        Path.home() / ".config" / "website-change-detector" / "config.json",
    ]
    for p in search_paths:
        if p.exists():
            config = json.loads(p.read_text(encoding="utf-8"))
            config["_config_dir"] = str(p.parent)
            return config
    raise FileNotFoundError(
        "未找到 config.json，已查找:\n"
        + "\n".join(f"  {p}" for p in search_paths)
    )


def load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("状态文件损坏，将重新建立基线")
        return {}


def save_state(state, path):
    Path(path).write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def state_file_path(config):
    custom = config.get("state_file")
    if custom:
        return custom
    return str(Path(config["_config_dir"]) / "state.json")


# ── network ───────────────────────────────────────────────────────────────

def fetch_page(url, timeout=30):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _get_wbi_key(cookies=None):
    now = _time.time()
    if _wbi_key_cache["key"] and now < _wbi_key_cache["expires"]:
        return _wbi_key_cache["key"]
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
    if cookies:
        headers["Cookie"] = cookies
    resp = httpx.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"Bilibili 鉴权失败 (code={data.get('code')}): {data.get('message')}。"
            f"请检查 config.json 中该 target 的 cookies 是否有效。"
        )
    img_key = data["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub_key = data["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    raw = img_key + sub_key
    mixed = "".join(raw[i] for i in WBI_MIXIN_TABLE)[:32]
    _wbi_key_cache["key"] = mixed
    _wbi_key_cache["expires"] = now + 1800
    return mixed


def _sign_bilibili(url, cookies=None):
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query))
    params["wts"] = str(int(_time.time()))
    sorted_items = sorted(params.items())
    query_string = "&".join(f"{k}={v}" for k, v in sorted_items)
    mixin_key = _get_wbi_key(cookies)
    w_rid = hashlib.md5((query_string + mixin_key).encode()).hexdigest()
    sorted_items.append(("w_rid", w_rid))
    new_query = "&".join(f"{k}={v}" for k, v in sorted_items)
    return urlunparse(parsed._replace(query=new_query))


def fetch_json(url, timeout=30, cookies=None):
    is_bilibili = "api.bilibili.com" in url
    original_url = url

    def _request(signed_url):
        parsed = urlparse(signed_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": origin + "/",
        }
        if cookies:
            headers["Cookie"] = cookies
        resp = httpx.get(
            signed_url, headers=headers, timeout=timeout, follow_redirects=True
        )
        resp.raise_for_status()
        return resp.json()

    if is_bilibili:
        url = _sign_bilibili(url, cookies)

    data = _request(url)

    if is_bilibili and isinstance(data, dict) and data.get("code") == -352:
        logger.warning("Bilibili 风控拦截 (-352)，刷新 WBI 密钥后重试...")
        _wbi_key_cache["expires"] = 0
        url = _sign_bilibili(original_url, cookies)
        _time.sleep(5)
        data = _request(url)

    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(
            f"API 返回错误 (code={data.get('code')}): {data.get('message', '未知')}"
        )
    return data


def extract_json_value(data, path):
    keys = path.split(".")
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list):
            idx = int(key)
            if idx < len(data):
                data = data[idx]
            else:
                return None
        else:
            return None
    return data


# ── parsing ───────────────────────────────────────────────────────────────

def extract_visible_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()
    body = soup.find("body")
    source = body if body else soup
    return source.get_text(separator="\n", strip=True)


def compute_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── email ─────────────────────────────────────────────────────────────────

def send_email(smtp_cfg, target_name, url, strategy, old_text, new_text):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if strategy == "fulltext":
        old = old_text[:600] + "..." if len(old_text) > 600 else old_text
        new = new_text[:600] + "..." if len(new_text) > 600 else new_text
        detail = (
            f"<p>页面 <strong>{target_name}</strong> 内容发生变化。</p>"
            f"<p>URL: <a href='{url}'>{url}</a></p>"
            f"<p>检测时间: {now}&emsp;策略: 全文对比</p>"
            f"<hr><h3>旧内容</h3><pre>{old}</pre>"
            f"<h3>新内容</h3><pre>{new}</pre>"
        )
    else:
        detail = (
            f"<p>页面 <strong>{target_name}</strong> 内容发生变化。</p>"
            f"<p>URL: <a href='{url}'>{url}</a></p>"
            f"<p>检测时间: {now}&emsp;策略: 哈希对比</p>"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[网站监控] {target_name} 发生变化"
    msg["From"] = smtp_cfg["user"]
    msg["To"] = smtp_cfg["receiver"]
    msg.attach(MIMEText(detail, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg["port"], timeout=15) as srv:
            srv.login(smtp_cfg["user"], smtp_cfg["password"])
            srv.sendmail(smtp_cfg["user"], smtp_cfg["receiver"], msg.as_string())
        logger.info("邮件已发送 → %s", smtp_cfg["receiver"])
    except Exception as exc:
        logger.error("邮件发送失败: %s", exc)


# ── detection ─────────────────────────────────────────────────────────────

def check_target(target, state):
    """Fetch page, compare with previous state. Returns (changed, old_text, new_text)."""
    url = target["url"]
    name = target["name"]
    content_type = target.get("content_type", "html")

    logger.info("检查: %s (%s)", name, url)

    try:
        if content_type == "json":
            data = fetch_json(
                url,
                timeout=target.get("timeout", 30),
                cookies=target.get("cookies"),
            )
            json_path = target.get("json_path")
            if json_path:
                data = extract_json_value(data, json_path)
            text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        else:
            html = fetch_page(url, timeout=target.get("timeout", 30))
            text = extract_visible_text(html)
    except Exception as exc:
        logger.error("抓取失败 [%s]: %s", name, exc)
        return False, "", ""

    current_hash = compute_hash(text)
    previous = state.get(url)

    state[url] = {
        "hash": current_hash,
        "text": text,
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }

    if previous is None:
        logger.info("首次抓取，基线已建立: %s", name)
        return False, "", ""

    if current_hash != previous["hash"]:
        logger.info("★ 检测到变化: %s", name)
        return True, previous["text"], text

    logger.info("无变化: %s", name)
    return False, "", ""


def monitor_single_target(config, state, state_path, target):
    changed, old_text, new_text = check_target(target, state)
    if changed:
        strategy = target.get("strategy", "hash")
        send_email(
            config["smtp"],
            target["name"],
            target["url"],
            strategy,
            old_text,
            new_text,
        )
    save_state(state, state_path)


def monitor_all_targets(config, state, state_path):
    for target in config.get("targets", []):
        monitor_single_target(config, state, state_path, target)


# ── entry point ───────────────────────────────────────────────────────────

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main():
    setup_logging()
    logger.info("网站变化监控器 v%s 启动中...", __version__)

    config = load_config()
    targets = config.get("targets", [])
    if not targets:
        logger.warning("未配置任何监控目标，请在 config.json 的 targets 中添加")
        return

    default_interval = config.get("interval_minutes", 30)
    state_path = state_file_path(config)
    state = load_state(state_path)

    logger.info("已加载 %d 个监控目标", len(targets))

    # 启动时立即扫描一次
    logger.info("正在执行启动扫描...")
    monitor_all_targets(config, state, state_path)
    logger.info("启动扫描完成")

    scheduler = BlockingScheduler()
    for i, target in enumerate(targets):
        minutes = target.get("interval_minutes", default_interval)
        scheduler.add_job(
            monitor_single_target,
            "interval",
            minutes=minutes,
            args=[config, state, state_path, target],
            id=f"monitor_{i}",
        )
        logger.info(
            "  已注册: %s — 每 %d 分钟检查一次", target["name"], minutes
        )

    def shutdown(signum, frame):
        logger.info("收到退出信号，正在关闭...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("调度器已启动，按 Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    logger.info("监控已停止")


if __name__ == "__main__":
    main()
