---
version: answer_plan_v1
capability: match
---
[系统]
你要为一个业务问题写出"回答计划"：要回答它，需要哪些数据、按什么方法算。

规则：
1. 先判断这个问题属于 lookup（查现状）/ aggregate（汇总统计）/ rule（按规则判断）/ forecast（预测未来）哪一类；
   凡包含"未来、会不会、预计、下两周"等的是 forecast。
2. 写出时间窗口与 3–6 步计算步骤；forecast 必须写出所需历史数据的最短天数（min_history_days）。
3. inputs 逐项列出需要的数据：need 用业务语言；semantic 必须从 <vocabulary> 里选一个标签，都不合适才写 new:<英文蛇形>；
   temporal 取 snapshot（当前值）/ event（发生记录）/ history（需要一段历史）。
4. 不得引入问题与 data_needs 之外的概念；缺口如实保留，不要为了"能回答"降低方法要求。
5. 只输出 JSON。

[用户]
<question>{{ question }}</question>
<data_needs>{{ data_needs }}</data_needs>
<vocabulary>
{{ vocabulary }}
</vocabulary>
请输出回答计划。
