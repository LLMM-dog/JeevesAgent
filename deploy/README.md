# Jeeves 部署指南

Jeeves 默认只在本机跑（127.0.0.1:9000）。要**远程访问**，推荐在界面里点几下就完成，
不用翻文件、不用手动装工具。

## 方式一：界面一键远程访问（推荐，零服务器）

打开 `设置 → 部署`，按提示点按钮即可：

1. **开启鉴权**：输入管理员用户名 + 密码，点「开启鉴权并创建管理员」——
   会自动完成建账号 + 开鉴权 + 登录
2. **装 Tailscale**：点「安装便携版 Tailscale」——先检测系统已装的（有就直接用），
   没有才下载到项目目录 `.tailscale/`，随项目走、删除项目即彻底卸载
3. **登录**：点「登录 Tailscale」，把页面弹出的授权链接在浏览器打开完成授权
4. **开启隧道**：点「开启 serve」—— 之后在手机/笔记本（同一 Tailscale 账号）访问
   `https://<设备名>.ts.net`

> 需要公网可访问时改点「开启 funnel」（任何人拿到链接都能访问，务必已开鉴权）。
> Tailscale 自带 TLS 证书，密码与 cookie 全程加密。

## 方式二：Linux 服务器一键部署

前置：一台 Linux 服务器（Ubuntu/Debian 系），本机可免密 SSH 登录它。

```bash
ssh-copy-id user@server            # 一次性
./deploy/deploy.sh user@server --domain your-domain.com
```

脚本自动完成：rsync 代码 → 装 uv/node → 装依赖+构建前端 → 生成 .env（新加密密钥，
强制开鉴权 + 绑 0.0.0.0）→ systemd 服务 → Caddy 反代自动 HTTPS。

部署完成后访问 `https://your-domain.com`。首次登录密码在服务日志里：

```bash
ssh user@server 'journalctl -u jeeves -n 100 | grep 初始密码'
```

## 方式三：Docker 部署

```bash
cp .env.example .env      # 填好 ENCRYPTION_KEY，开鉴权
docker compose up -d --build
```

有域名时把 `deploy/Caddyfile` 里的 `your-domain.com` 换成真实域名（A 记录指向服务器），
compose 里的 Caddy 会自动签发 https 证书。

## Tailscale 便携化

- **优先用系统已装的** Tailscale（`which tailscale` + 常见安装路径）
- 没有则从 `pkgs.tailscale.com` 下载便携二进制到项目目录 `.tailscale/`（已 gitignore），
  socket / 登录状态也都在项目目录里 —— 删除整个项目目录 = 彻底干净
- 平台差异：Linux 静态 tarball 无 root；Windows MSI 提取二进制、TUN 驱动是内核级
  （首次启动 tailscaled 弹一次 UAC）；macOS 建议先用系统安装

## 配置的两种改法

- **界面**（推荐）：`设置 → 部署` 里改鉴权开关、绑定地址、端口、管理员账号密码
- **.env**：服务器脚本与高级用户仍可直接编辑，两套配置在内存里即时同步

## 安全清单（防恶意入侵）

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| 登录鉴权 | ✅ 内置 | 用户名 + 密码，PBKDF2 加盐哈希；全 API 强制登录 |
| 登录限流 | ✅ 内置 | 默认 15 分钟 10 次失败，防暴力破解 |
| 会话管理 | ✅ 内置 | HttpOnly + SameSite=Strict cookie，可吊销，30 天过期 |
| CSRF | ✅ 内置 | 跨站请求被 Origin 校验拦截 |
| 安全响应头 | ✅ 内置 | CSP / X-Frame-Options / nosniff 等 |
| HTTPS | ✅ Tailscale/Caddy | 密码与 cookie 全程加密 |
| 绑定非本机强制鉴权 | ✅ 启动校验 | 未开鉴权直接拒绝启动 |
| 路径白名单 / 拒止锚 | ✅ 原有 | 文件访问边界 |
| 命令审批 / 沙箱 | ✅ 原有 | manual 审批 + 可配 Docker 隔离 |
| 防火墙 | ⚠️ 建议 | 服务器只开 80/443（或 22） |
| 备份 | ⚠️ 建议 | 定期备份 `data/` + `.env` |

## 卸载（一键全面清理）

项目是个人本地定位：所有数据都在项目文件夹里，删文件夹就没了。
但有两类东西删文件夹带不走，所以提供一键卸载脚本：

```bash
# Windows：双击 uninstall.bat
uninstall.bat            # 清理 Tailscale 便携版、Docker 容器、残留进程
                         # 并询问是否删除整个项目文件夹

# Linux / macOS
bash uninstall.sh            # 清理项目外残留，保留源码
bash uninstall.sh --delete   # 之后直接删除整个项目文件夹
```

清理内容：
- **便携版 Tailscale**：`tailscale logout` + 停 tailscaled 进程 + 删 `.tailscale/`
  （便携版用 `--tun=userspace-networking` 纯用户态，不装内核 TUN 驱动，删目录即彻底干净）
- 项目创建的 Docker 容器
- 残留进程检查
- 可选：删除整个项目文件夹（含源码）

## 运维

```bash
ssh user@server 'systemctl restart jeeves'   # 重启（改绑定地址/端口后）
ssh user@server 'journalctl -u jeeves -f'
docker compose logs -f jeeves
git pull && ./deploy/deploy.sh user@server   # 升级
```

## 常见问题

- **启动报错“未开启鉴权”**：绑定非本机地址但鉴权是关的。先在界面开启鉴权，或
  把 `app.host` 改回 127.0.0.1。
- **登录后马上被踢**：走了 http 而非 https，请走 Tailscale/Caddy 的 https 链接。
- **忘记管理员密码**：删除 `data/jeeves.db` 里的 `auth_user` 表行（或整个库），
  重启后在 `设置 → 部署` 重新「开启鉴权并创建管理员」。
- **改了端口不生效**：需要重启服务。
