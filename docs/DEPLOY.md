# 部署 TaskHub 到服务器

本地已验证通过，本文只讲怎么搬到公网服务器并让多台电脑连上。

## 前置

- 一台能被其他电脑访问的机器（云主机 / 内网服务器均可）
- Python 3.10+
- 如果要走 HTTPS，需要一个域名（或用内网 IP 直连）

## 一、部署服务端

### 拷贝代码

```bash
# 在服务器上
sudo mkdir -p /opt/taskhub && cd /opt/taskhub
# 上传 taskhub 目录下的 server/ requirements.txt
```

### 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 用 systemd 守护

创建 `/etc/systemd/system/taskhub.service`：

```ini
[Unit]
Description=TaskHub Server
After=network.target

[Service]
Type=simple
User=taskhub
WorkingDirectory=/opt/taskhub
Environment="TASKHUB_ADMIN_TOKEN=换成你自己的长口令"
Environment="TASKHUB_DATA=/var/lib/taskhub"
Environment="TASKHUB_PUBLIC_URL=https://taskhub.example.com"
ExecStart=/opt/taskhub/.venv/bin/python -m uvicorn server.app:app --host 0.0.0.0 --port 8787
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/lib/taskhub
sudo systemctl daemon-reload
sudo systemctl enable --now taskhub
sudo systemctl status taskhub
```

> **数据目录说明**：数据库和附件都在 `TASKHUB_DATA` 下（默认是代码目录里的 `data/`）。
> 部署时把它指到 `/var/lib/taskhub` 这样升级代码不会丢数据。
> 备份就是备份这一个目录。

### 可选：Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
ENV TASKHUB_DATA=/data
EXPOSE 8787
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8787"]
```

```yaml
# docker-compose.yml
services:
  taskhub:
    build: .
    ports: ["8787:8787"]
    volumes:
      - taskhub-data:/data
    environment:
      TASKHUB_ADMIN_TOKEN: 换成你自己的长口令
      TASKHUB_PUBLIC_URL: https://taskhub.example.com
    restart: unless-stopped
volumes:
  taskhub-data:
```

## 二、HTTPS 反代（推荐）

裸 HTTP 暴露在公网上，token 会明文传输。用 Nginx 反代：

```nginx
server {
    listen 443 ssl;
    server_name taskhub.example.com;

    ssl_certificate     /etc/letsencrypt/live/taskhub.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/taskhub.example.com/privkey.pem;

    # 任务附件可能较大（截图、构建日志）
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**注意**：如果前面挂了 Nginx，FastAPI 需要知道自己在代理后面才能生成正确的链接。
`--proxy-headers` 是 uvicorn 默认开启的，通常无需额外配置。

## 三、防火墙

```bash
sudo ufw allow 8787/tcp     # 如果不用反代
# 用反代的话只开 443
```

## 四、各电脑接入

在每台执行电脑上：

1. `pip install requests`
2. 复制 `agent/agent.py` 到该电脑
3. 复制 `agent/agent.example.json` 为 `agent.json`，填入：
   - `server`：服务器地址，如 `https://taskhub.example.com`
   - `token`：控制台注册节点时下发的
   - `labels`：这台电脑的能力标签
   - `workspace`：本机项目根目录
4. 运行 `python agent.py --config agent.json`

### 让 agent 常驻

**Linux（systemd）** — 创建 `~/.config/systemd/user/taskhub-agent.service`：

```ini
[Unit]
Description=TaskHub Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 %h/taskhub/agent.py --config %h/taskhub/agent.json
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now taskhub-agent
```

**Windows** — 用任务计划程序，触发器选「计算机启动时」，
操作为启动程序，填 `python.exe` 路径和 `agent.py --config agent.json`。

## 五、WorkBuddy 定期任务（如果用智能模式）

在每台电脑的 WorkBuddy 里建一个定期任务（建议 5-10 分钟一次），
把 `workbuddy-schedule-prompt.md` 的内容粘进去，替换其中的 `{server}` 和 `{token}`。

## 六、数据备份

```bash
# SQLite 在线备份（不锁库）
sqlite3 /var/lib/taskhub/taskhub.db ".backup '/var/backups/taskhub-$(date +%F).db'"

# 附件目录
tar czf /var/backups/taskhub-uploads-$(date +%F).tar.gz -C /var/lib/taskhub uploads
```

建议加个 cron 每天跑一次。

## 七、升级

```bash
cd /opt/taskhub
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart taskhub
```

数据库用 `CREATE TABLE IF NOT EXISTS`，重启时自动补新表。

## 排查清单

| 现象 | 检查 |
|---|---|
| agent 报 401 | token 错了。去控制台「重置口令」，同步更新 agent.json |
| agent 报 409 | 该节点已有任务在跑。检查是不是 WorkBuddy 崩了留下了 running 任务 |
| 任务一直不被领取 | 任务 `required_labels` 和所有节点 `labels` 没有交集 |
| agent 收不到附件 | `client_max_body_size` 太小，或节点标签不匹配 |
| 连不上服务器 | 防火墙、防火墙安全组、反代配置 |
| 心跳正常但任务不动 | 项目配了 `command`，agent 跑的是命令而不是等 AI；或反之 |