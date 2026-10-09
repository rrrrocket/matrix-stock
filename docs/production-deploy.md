# Stock 生产部署：stock.matrix-one.tech

以下命令在本机 Stock 仓库执行；服务器命令标注为远端。服务器应已安装 Docker Compose，并运行 Nginx Proxy Manager（NPM）。`stock.matrix-one.tech` 已解析到服务器，但还没有 Stock 的 HTTPS 代理主机。

## 1. 在服务器拉取代码

在服务器运行：

```bash
git clone https://github.com/rrrrocket/matrix-stock.git /home/ubuntu/matrix-stock
```

后续更新时在服务器运行 `git -C /home/ubuntu/matrix-stock pull --ff-only origin main`，然后重新运行 `./start.sh`。生产密钥和数据库不会进入 Git。

## 2. 上传生产配置

本机已经生成未纳入 Git 的 `.env.production`，其中包含随机的管理员密码、数据库密码和接口密钥，并已配置 `erp.matrix-one.tech`。将它单独上传：

```bash
scp .env.production ubuntu@stock.matrix-one.tech:/home/ubuntu/matrix-stock/.env.production
ssh ubuntu@stock.matrix-one.tech 'chmod 600 /home/ubuntu/matrix-stock/.env.production'
```

Stock 生产配置已将 `STOCK_ERP_CALLBACK_URL` 设为 `https://erp.matrix-one.tech/api/orders/stock-outbound`，`STOCK_ERP_RETURN_ORIGINS` 设为 `https://erp.matrix-one.tech`。需要登录密码时，在本机查看 `.env.production` 的 `STOCK_ADMIN_PASSWORD`；不要把它发到聊天或提交到 Git。

ERP 生产环境还需设置：`STOCK_SERVICE_URL=https://stock.matrix-one.tech`、`STOCK_SERVICE_PUBLIC_URL=https://stock.matrix-one.tech`，以及与 Stock `.env.production` 的 `STOCK_ERP_API_KEY` 完全一致的 `STOCK_SERVICE_API_KEY`。无需填写 ERP 主账号 ID；ERP 用户在「授权中心 → 库存系统授权」登录并绑定已审核的 Stock 账号后，才会推送其已映射的订单。ERP 和 Stock 两端代码都要更新后再启用订单推送，并按各自的 `./start.sh` 部署流程重启。

部署前确认服务器端口 `8020` 未被占用。若已占用，在服务器的 `.env.production` 增加 `STOCK_HOST_PORT=其他端口`；NPM 的 Docker 上游仍是 `matrix-stock-web:8020`。

## 3. 启动数据库和 Web

在服务器运行：

```bash
cd /home/ubuntu/matrix-stock
./start.sh
```

`./start.sh` 在生产服务器启动 PostgreSQL、Web 和重试进程，自动执行迁移与管理员同步、连接 NPM 网络，并在结束时显示容器状态。只需执行这一个启动命令。本机测试使用 `./start.sh test`，测试重试进程使用 `./start.sh test worker`。数据库使用 Docker 卷 `matrix-stock_stock_db`，重新构建容器不会删除数据。**不要运行 `docker compose down -v`。**

## 4. 在 NPM 配置域名和 HTTPS

打开生产环境 Nginx Proxy Manager 管理页 [npm.matrix-one.tech](https://npm.matrix-one.tech/) 并登录。进入 **Hosts → Proxy Hosts → Add Proxy Host**，在 **Details** 页签填写：

| 项目 | 值 |
| --- | --- |
| Domain Names | `stock.matrix-one.tech` |
| Scheme | `http` |
| Forward Hostname / IP | `matrix-stock-web` |
| Forward Port | `8020` |
| Access List | `Publicly Accessible`，让 ERP API 能调用 Stock |
| Block Common Exploits | 开启 |

切换到 **SSL** 页签，选 **Request a new SSL Certificate**，开启 **Force SSL** 和 **HTTP/2 Support**，同意 Let's Encrypt 条款后点 **Save**。域名 DNS 已指向服务器；NPM 现有站点也已对外提供 80/443。Stock 无需单独开放公网 8020 端口。[Nginx Proxy Manager 官方指南](https://nginxproxymanager.com/guide)说明了代理主机与 Let's Encrypt 证书功能。

如果 NPM 保存后显示 `502 Bad Gateway`，先在服务器确认 NPM 容器已加入 Stock 网络：

```bash
docker ps --format '{{.Names}} {{.Image}}' | grep nginx-proxy-manager
docker exec <NPM容器名> getent hosts matrix-stock-web
```

若无法解析 `matrix-stock-web`，运行 `NPM_CONTAINER=<NPM容器名> ./start.sh`。如证书申请失败，核对 NPM 的错误提示和域名 DNS；此域名目前尚无 HTTPS 证书。

## 5. 验收

打开 `https://stock.matrix-one.tech/login/`，再访问 `https://stock.matrix-one.tech/api/health/`，确认页面和接口都正常。若启动或访问失败，再在服务器运行 `docker compose --env-file .env.production -f compose.production.yml logs --tail=50 web` 查看错误。

生产数据库从空库开始，不导入本机仓库或订单。验收时确认登录页为 HTTPS、静态样式正常、管理员可登录；在 ERP 登录生产 Stock 并重新绑定账号。之后用测试订单验证 ERP 订单同步、Stock 出库、国内快递单号回填。不要用真实订单做首次联调。
