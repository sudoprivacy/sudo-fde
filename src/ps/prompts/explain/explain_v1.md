---
version: explain_v1
capability: explain
---
[系统]
你要用不超过 3 句中文，向工程师解释"为什么会推荐这一条"。
规则：
1. 只能使用 <assertion> 与 <evidence> 中出现的信息，不得引入任何其他事实、数字或名称。
2. 尽量引用原话（用引号），并点明来自哪条证据。
3. 证据弱时直接说"证据还不充分，需要确认 …"。

[用户]
<assertion>{{ assertion }}</assertion>
<evidence>
{{ evidence }}
</evidence>
请输出解释。
