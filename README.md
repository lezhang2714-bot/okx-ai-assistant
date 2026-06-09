# OKX AI短线助手部署目录

本目录是独立交付包，拷走整个目录即可运行，包含：

- `monitor.py`：OKX AI短线助手主程序
- `config.json`：业务配置
- `.env`：密钥和环境配置
- `run.sh`：读取配置并启动监控
- `install.sh`：安装依赖、创建虚拟环境、可选安装systemd用户服务
- `okx-ai-assistant.service`：systemd用户服务模板
- `view_logs.sh`：查看运行日志
- `test_push.py`：测试Telegram/企业微信/微信机器人推送
- `test_ai.py`：测试AI接口连通性
- `stability_24h.sh`：24小时稳定性测试

## 1. 安装

```bash
cd app/lpm-process/source/okx_ai_assistant_deploy
./install.sh
```

如果要安装为systemd用户服务：

```bash
./install.sh --user-systemd
```

## 2. 修改配置

编辑 `.env`：

```bash
OPENAI_API_KEY="你的OpenAI Key"
WECOM_WEBHOOK_URL="企业微信机器人Webhook"
TELEGRAM_BOT_TOKEN="Telegram Bot Token"
TELEGRAM_CHAT_ID="Telegram Chat ID"
```

编辑 `config.json`：

```json
{
  "runtime": 0,
  "interval": 5,
  "ai_enabled": true,
  "push_enabled": true
}
```

## 3. 前台运行

```bash
./run.sh
```

## 4. 测试推送

```bash
./test_push.py
```

## 5. 测试AI

```bash
./test_ai.py
```

## 6. 查看日志

```bash
./view_logs.sh
```

## 7. systemd用户服务

启动：

```bash
systemctl --user start okx-ai-assistant
```

查看状态：

```bash
systemctl --user status okx-ai-assistant
```

停止：

```bash
systemctl --user stop okx-ai-assistant
```

设置开机自启：

```bash
systemctl --user enable okx-ai-assistant
loginctl enable-linger "$USER"
```

## 8. 24小时稳定性测试

```bash
./stability_24h.sh
```

测试日志输出到：

```text
logs/stability_24h.log
logs/stability_24h.summary.json
```
