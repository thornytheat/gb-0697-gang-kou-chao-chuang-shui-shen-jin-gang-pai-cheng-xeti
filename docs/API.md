# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次；_direction_ 为可选第9列。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。M5 额外给出逐船放行单（放行/驶出时刻、等待位置、换向前置清空）、编队、换向准备与航道占用条。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`POST /api/restore`：{"id":"批次","revision":整数}。回放指定旧修订的排程序列，不重算；未来版本或非整数版本409/422。恢复后修订号加一，占班不重复。

`GET /api/runs/{id}/versions`：修订快照列表（修订号、决定、时间）。

`GET /api/runs/{id}/occupancy`：该批次航道占班槽位（类型、方向、船舶、起止），同一段只有一方向占用。

`GET /api/export/{id}`：唯一证据CSV，含 manifest、编队、换向、占班、逐船结果与隔离记录。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
