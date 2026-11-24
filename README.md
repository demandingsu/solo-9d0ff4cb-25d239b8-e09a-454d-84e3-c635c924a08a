# 标本同件索引交接与盘点索引 API

两馆合库后，向合作馆移交标本“同件索引”的交接服务。合作馆只接收
**能独立复核** 的配对：每个交接对必须同时给出两条同件证据链，两条链
除首尾登记号外 **登记号不共用、证据也不共用**。在交接数据之上另发
**盘点索引**：给登记号分配永久键并按供数配对编组，供盘点明细长期引用。

- 框架：FastAPI（交互文档 `GET /docs`）
- 存储：SQLite（WAL 模式，数据落在 `/data` 卷）
- 部署：Docker Compose，一条命令起服务

## 业务规则

1. 证据表每条证据含 **证据号** 与判为同件的 **两个登记号**。
2. 馆员用已有的同件配对申请交接；系统在当前 **有效（未撤回）** 证据中
   查找两条独立证据链：
   - 两条链都从申请对的一端到另一端；
   - 除首尾两个登记号外，两条链不得共用登记号；
   - 两条链不得共用证据。
3. 两条链齐备 → 状态 `supplied`（供数中），响应与落库结论都附上两条链的
   登记号序列和证据号序列；凑不齐 → 状态 `pending`（待补）。
4. 撤回证据时，**只重核用到它的交接对**（通过 `handover_evidence` 索引
   定位）：
   - 还能凑齐两条有效链 → 更新依据，继续 `supplied`（即“换链”）；
   - 凑不齐 → 状态置为 `paused`（暂停供数），清空链依据并记录原因。
5. 撤回与重核在 **同一个数据库事务** 内一起生效；任一步失败整体回滚。
6. 查询接口 **直接读取已保存的结论**，不临时重算；写入失败时上次结论
   仍可正常查询。

## 算法

把登记号看作无向图顶点、证据看作带编号的无向边。用 **点拆点 + 容量
约束** 建网络：

- 每个登记号拆成入点/出点，中间登记号点容量为 1（两条链不能共用），
  首尾点容量为 2；
- 每条无向证据边在两个方向各建容量 1 的弧（证据不能共用，也不能在
  链内折返）；
- Edmonds-Karp 求最大流，流量达到 2 即存在两条合法链，两次增广路
  直接给出两条链，并再做一次防御性互斥校验。

## 盘点索引

交接数据之外再提供一层“盘点索引”，供合作馆在盘点明细里长期引用：

1. **取数**：发布时在一个只读事务里，从当前 **已提交数据** 取出所有交接
   申请的两端登记号，以及所有 `supplied` 交接对的已保存链结论。
2. **编组**：登记号作顶点，“正在供数”的配对作无向边；按连通关系连成组，
   没连上的号各自成组。待补、暂停的交接申请只提供端点，不连边。
3. **永久键**：登记号首次入索引时分配永久键 `SK-00000001` 样式；同一批
   新键 **按登记号升序** 分配。键一旦分配永不改派，因此旧盘点明细里的
   键在合组、拆组后仍可用。
4. **组键**：每组取 **最早分配** 的成员键作组键（按分配顺序，不按字典序）。
   合组后组键取两组中更早的那个；拆组后各组重新按成员最早键取组键。
5. **核对**：编组不持写锁（期间交接照常办理、已发布索引照常查询）；写入
   前在写事务内重新取数并核对登记号与供数配对是否仍与取数时一致。不一致
   → `409`，整次发布取消，**沿用上期** 索引；一致 → 新键、成员、批次在
   同一事务里一起提交，任一步写入失败整体回滚，上期索引原样可查。

查询按 **登记号、永久键或组键** 任一命中，只读已发布内容（不重算），返回
对应登记号的永久键、当前组键与该组全体成员。详见表 `inventory_key`（键表，
只追加）、`inventory_member`（当前索引，整表替换）、`inventory_batch`
（发布批次与统计）。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 试办交接
```

不使用 Docker 时的本地运行方式：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
HANDOVER_DB=./handover.db uvicorn app.main:app --reload
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/evidence` | 录入证据（证据号唯一，重复返回 409） |
| GET | `/evidence` | 列出有效证据，`?include_withdrawn=true` 含已撤回 |
| GET | `/evidence/{id}` | 查询单条证据 |
| POST | `/evidence/{id}/withdraw` | 撤回证据，同事务只重核相关交接对，返回受影响对象及重核结论 |
| POST | `/handovers` | 凭同件配对申请交接，当场判定 `supplied`/`pending` 并落库 |
| GET | `/handovers` | 列出全部交接对及已保存结论（不重算） |
| GET | `/handovers/{id}` | 读取单个交接对已保存的结论 |
| POST | `/handovers/{id}/reverify` | 手工重核（补证后可让待补/暂停对象重新判定） |
| POST | `/inventory/publish` | 发布一期盘点索引；核对到数据变化返回 409 并沿用上期 |
| GET | `/inventory` | 列出当前已发布索引（按组、组内按分配顺序），含批次统计 |
| GET | `/inventory/{query}` | 按登记号、永久键或组键查当前归属与全组成员 |
| GET | `/health` | 健康检查 |

状态取值：`supplied`（供数中）、`pending`（待补）、`paused`（暂停供数）。

## 试办示例（/docs 可直接照填）

```bash
# 录入三条互不相交的 A->B 候选链
for body in \
  '{"evidence_id":"E1","reg_a":"A","reg_b":"M1"}' \
  '{"evidence_id":"E2","reg_a":"M1","reg_b":"N1"}' \
  '{"evidence_id":"E3","reg_a":"N1","reg_b":"B"}' \
  '{"evidence_id":"E4","reg_a":"A","reg_b":"M2"}' \
  '{"evidence_id":"E5","reg_a":"M2","reg_b":"N2"}' \
  '{"evidence_id":"E6","reg_a":"N2","reg_b":"B"}' \
  '{"evidence_id":"E7","reg_a":"A","reg_b":"M3"}' \
  '{"evidence_id":"E8","reg_a":"M3","reg_b":"N3"}' \
  '{"evidence_id":"E9","reg_a":"N3","reg_b":"B"}'; do
  curl -s -X POST localhost:8000/evidence -H 'Content-Type: application/json' -d "$body" >/dev/null
done

# 申请交接 -> supplied，返回两条链的登记号与证据号
curl -s -X POST localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"reg_a":"A","reg_b":"B"}'

# 再录一组 C-D 与一对尚未供数的 E-F，然后发布盘点索引
for body in \
  '{"evidence_id":"F1","reg_a":"C","reg_b":"K1"}' \
  '{"evidence_id":"F2","reg_a":"K1","reg_b":"D"}' \
  '{"evidence_id":"F3","reg_a":"C","reg_b":"K2"}' \
  '{"evidence_id":"F4","reg_a":"K2","reg_b":"D"}'; do
  curl -s -X POST localhost:8000/evidence -H 'Content-Type: application/json' -d "$body" >/dev/null
done
curl -s -X POST localhost:8000/handovers -H 'Content-Type: application/json' \
  -d '{"reg_a":"C","reg_b":"D"}' >/dev/null
curl -s -X POST localhost:8000/handovers -H 'Content-Type: application/json' \
  -d '{"reg_a":"E","reg_b":"F"}' >/dev/null   # 待补：两端各自成组

curl -s -X POST localhost:8000/inventory/publish
# 按登记号、永久键、组键均可查当前归属与全组成员
curl -s localhost:8000/inventory/A
curl -s localhost:8000/inventory/SK-00000001

# 撤回 E1 -> 自动换到 E7/E8/E9 那条链，继续供数
curl -s -X POST localhost:8000/evidence/E1/withdraw
```

## 测试

```bash
python -I tests/test_graph.py      # 算法单元测试（8 项）
python -I tests/test_api.py        # API 端到端测试（5 项，含事务回滚）
python -I tests/test_inventory.py  # 盘点索引测试（8 项：发布/合组/拆组/核对/回滚/编组期间并发）
python -I tests/demo_http.py       # 真实 HTTP 试办：供数→换链→暂停→恢复
python -I tests/demo_inventory_http.py  # 真实 HTTP 试办：发布→合组→拆组
```

## 数据表

- `evidence`：证据（`withdrawn` 软撤回标记，行保留可追溯）。
- `handover`：交接申请与已保存结论（状态、两条链的登记号/证据号 JSON、
  原因、时间戳），`(reg_a, reg_b)` 唯一且规范化排序。
- `handover_evidence`：交接对当前依据所用证据索引，支撑“只重核用到它
  的交接对”的精准定位。
- `inventory_key`：登记号 -> 永久标本键 + 分配序号，只追加、永不改派。
- `inventory_member`：当前已发布索引（登记号、永久键、当前组键），每次
  成功发布整表替换。
- `inventory_batch`：每次成功发布的批次、时间与统计（交接申请数、供数
  配对数、成员数、组数、新键数）。
