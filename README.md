# Basketball Plus-Minus Recalculation API

篮球技术台补正换人/得分记录后，重算球员正负值的复算服务。
FastAPI + SQLite，事件按序编号、带修订号，更正整批原子生效，统计按版本物化。

## 启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（不需要 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行自测
```

数据文件路径由环境变量 `BASKETBALL_DB` 决定（compose 中为卷 `/data/basketball.db`，
本地默认 `./basketball.db`）。

## 规则

- 两队（A/B）各一份名单和 5 名首发；任何时刻每队场上保持 5 人。
- 换人：只能用**本队替补**换下**本队在场**球员。
- 得分：分值 1/2/3，**得分球员必须在场**。
- 每次得分：本队场上 5 人正负值各 **+分值**，对方场上 5 人各 **−分值**。
- 导入和更正都对**整条全场记录**按序模拟验算，任何一条不合法即整批拒绝。

## API 一览（均可在 /docs 中直接操作）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/games` | 导入两队名单、首发、按序编号的换人/得分记录 → 版本 1 |
| GET | `/games/{id}/events` | 当前生效的事件及各自**修订号**（记录员更正前查询用） |
| POST | `/games/{id}/corrections` | 按所见修订号**整批**更正事件 → 新版本 |
| GET | `/games/{id}/stats` | 版号、比分、各球员正负值；`?version=N` 查历史版本 |
| GET | `/games/{id}/versions` | 版本历史（导入/各次更正批次） |
| GET | `/games/{id}` / `/games` | 比赛详情 / 列表 |

### 导入示例

```json
POST /games
{
  "game_id": "finals-g1",
  "team_a": {"name": "Tigers", "players": ["A1","A2","A3","A4","A5","A6","A7"],
             "starters": ["A1","A2","A3","A4","A5"]},
  "team_b": {"name": "Sharks", "players": ["B1","B2","B3","B4","B5","B6","B7"],
             "starters": ["B1","B2","B3","B4","B5"]},
  "events": [
    {"type": "score", "team": "A", "player": "A1", "points": 2},
    {"type": "substitution", "team": "A", "out": "A5", "in": "A6"},
    {"type": "score", "team": "B", "player": "B1", "points": 3}
  ]
}
```

事件按数组顺序编号为 `seq` 1..N，初始修订号均为 0。

### 更正示例

```json
POST /games/finals-g1/corrections
{"corrections": [
  {"seq": 2, "expected_revision": 0,
   "event": {"type": "substitution", "team": "A", "out": "A5", "in": "A7"}},
  {"seq": 4, "expected_revision": 0,
   "event": {"type": "score", "team": "A", "player": "A7", "points": 3}}
]}
```

## 更正语义（与需求逐条对应）

- **按所见修订号整批更正**：批内每条都带 `expected_revision`；任一事件的当前
  修订号与之不符 → 整批拒绝，`409` 并列出冲突事件的 `seq`、期望与实际修订号，
  原事件和统计照旧。
- **基于最新已生效记录验算**：其他事件此前已有更正不影响本批——只要本批所改
  事件的修订号仍相符，就在包含既往全部更正的最新全场记录上套用本批并整体
  验算（五人制、本队替补换人、得分者在场）。验算失败 → `422` 并指出相关事件
  的 `seq` 与原因，原事件和统计照旧。
- **复算期间可查上一版**：新版本的统计完全在内存中算好后，才用一个短事务把
  「新事件修订 + 版本记录 + 新统计」一起提交；查询接口不加锁，复算期间读到的
  仍是上一版（WAL 模式，读写不互斥）。测试用一个提交前钩子实际验证了这一点。
- **更正成功随新版生效**：提交后 `current_version` 指向新版本，事件与统计同时
  切换；历史版本的统计仍可通过 `?version=N` 查询（每版物化，永不回改）。

## 并发模型

- 更正（写）按比赛加进程内互斥锁串行化；两个并发批次一个先生效，另一个若
  修订号仍相符则在其之上继续生效，否则收到 409。
- 查询（读）不加锁，读最近已提交版本。
- 部署形态为**单进程单 worker**（compose 默认）。多进程/多机部署需要把序列化
  下沉到数据库（如 `BEGIN IMMEDIATE` + 事务内复核），当前未实现。

## 自测

`pytest` 共 16 个用例，覆盖：导入验算与正负值逐分手算核对、非法导入/换人/
得分拒绝、更正成功重算、修订冲突 409、基于最新记录的交错更正、验算失败 422
并指出事件、批次原子性、历史版本查询、**复算期间并发查询仍见上一版**、
**并发更正不同事件均生效**、**并发更正同一事件一胜一负**。

## 已知边界

- 更正只支持**修改**已编号事件，不支持插入/删除事件（需求范围为"更正原事件"）。
- 分值限定 1/2/3。
- 多 worker / 多实例部署的写序列化未实现（见上）。
