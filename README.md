# Matrix Stock

独立部署的自有仓库存系统。库存由人工入库、盘点和订单出库维护；不读取或改写 ERP 的平台库存池。一个实例服务一个公司，员工账号由管理员创建。多个自有仓分别记账，同一订单商品可分多次、从不同仓库出库。

## 本地启动

需要 Python 3.12+ 和 `uv`。复制 `.env.example` 为 `.env`，为 `STOCK_SECRET_KEY`、`STOCK_ERP_API_KEY` 和 `STOCK_ADMIN_PASSWORD` 设置独立随机值。`STOCK_ADMIN_USERNAME` 默认为示例中的 `admin`，可自行修改。然后：

```bash
./start.sh
```

启动时会根据 `.env` 创建管理员；修改其中的管理员密码后重启服务即可更新登录密码。打开 http://127.0.0.1:8020/ 登录。先建立仓库，再人工入库。生产环境应配置 PostgreSQL、HTTPS、`STOCK_DEBUG=0` 和安全密钥；SQLite 仅供本地开发。

期初库存支持上传 `.xlsx` 或 `.csv`。表头必须包含 `仓库编码`、`货号`、`数量`，可选 `商品名称`；先创建仓库再导入。同一仓库货号只允许导入一次期初库存，之后使用“人工入库”和“盘点调整”。

## 货号标准

库存系统沿用 ERP 的货号：品牌和型号之间使用空格或 `-`，例如 `品牌 型号` 或 `品牌-型号`。ERP 中的 `LocalProduct.sku` 是准确信息来源。期初库存导入、人工入库、盘点和订单同步均使用 ERP 中**相同的完整货号**；库存系统仅去除首尾空白，不转换分隔符，也不把两种写法自动视为同一货号。已有 ERP 货号无需修改。

## ERP 订单接口

ERP 对 `POST /api/orders/` 发送完整订单快照，HTTP 头 `X-Stock-Key` 使用 `STOCK_ERP_API_KEY`。请求只包含库存业务需要的字段，不传客户个人信息：

```json
{
  "platform": "ozon",
  "store_id": "18",
  "store_name": "示例店铺",
  "order_id": "posting-1001",
  "status": "open",
  "version": 1,
  "ordered_at": "2026-10-01T10:30:00+08:00",
  "items": [
    {"sku": "示例品牌-100", "name": "示例商品", "quantity": 2}
  ]
}
```

`status` 为 `open`、`cancelled` 或 `closed`。相同平台、店铺 ID 和订单号的重复快照只更新订单，不会重复扣库存。订单商品按货号汇总。取消订单阻止后续出库，但已实际出库的数量不会自动回滚，退货需要实物入库。当前接口可先用于联调。

ERP 推送时应发送递增的 `version`；相同版本的重复请求不会改写订单，低版本请求会被拒绝。旧的无版本调用仍可用于尚未接入版本控制的订单。

如需飞书群提醒，在 `.env` 配置 `STOCK_FEISHU_WEBHOOK`，机器人启用了签名校验时再填 `STOCK_FEISHU_SECRET`，然后另开终端运行 `./start.sh worker`。订单与自有仓库存匹配后，每张订单生成一条通知；失败会重试，订单取消或库存耗尽时不再发送。ERP 推送适配器和补拉核对仍待下一阶段接入。

## 测试

```bash
.venv/bin/python manage.py test stock
.venv/bin/python manage.py check
```
