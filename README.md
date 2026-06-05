# website-change-detector

定时监控网页变化，发现变动后通过邮件通知你。

> 定时抓取网页 → 提取纯文本 → SHA256 哈希对比 → 变化时发送邮件

## 依赖

- Python >= 3.12
- [APScheduler](https://github.com/agronholm/apscheduler) — 定时任务调度
- [httpx](https://github.com/encode/httpx) — HTTP 请求
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML 解析

## 快速开始

### 配置环境

```bash
cd website-change-detector
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 运行

```bash
detect
```

### 退出

按 `Ctrl+C` 退出

## 配置

复制模板并填入你的信息：

```bash
cp config_example.json config.json
```

编辑 `config.json`：

```json
{
  "smtp": {
    "host": "smtp.qq.com",
    "port": 465,
    "user": "你的QQ号@qq.com",
    "password": "QQ邮箱授权码",
    "receiver": "接收通知的邮箱"
  },
  "interval_minutes": 30,
  "targets": [
    {
      "name": "新闻首页",
      "url": "要追踪的url",
      "strategy": "hash",
      "interval_minutes": 10
    },
    {
      "name": "博客",
      "url": "要追踪的url",
      "strategy": "fulltext",
      "interval_minutes": 10
    }
  ]
}
```

### 检测策略

| 策略 | 说明 |
|------|------|
| `hash` | 哈希对比 |
| `fulltext` | 同上，但邮件里会附上变化前后的文本预览 |

### 可选字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `strategy` | `hash` | 检测策略 |
| `interval_minutes` | 继承全局值 | 单独设置检查间隔 |
| `timeout` | 30 | 请求超时（秒） |
| `state_file` | `config.json` 同目录 | 自定义状态文件路径 |
| `content_type` | `html` | 可选 `json` |
| `json_path` | | | 
| `cookies` | | |

