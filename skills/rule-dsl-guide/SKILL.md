---
name: rule-dsl-guide
description: 风控规则 DSL 的结构、语法与安全变更规范，用于生成可回滚的规则配置。
version: "1.0.0"
triggers: ["规则配置", "DSL", "表达式", "条件", "动作", "优先级", "名单"]
tags: ["规则", "DSL", "配置"]
tools: ["rule_get", "rule_validate", "rule_apply"]
---

# 规则 DSL 规范

## 结构

```yaml
rule_id: R_RISK_001
name: 高风险设备登录拦截
version: 3
priority: 100
enabled: true
scope:
  channels: ["APP", "H5"]
  products: ["credit_pay"]
when:
  all:
    - feature: device_risk_score
      op: gte
      value: 0.85
    - feature: login_fail_count_1h
      op: gte
      value: 5
then:
  action: reject            # reject | review | pass | challenge
  reason_code: DEVICE_HIGH_RISK
  tag: device_fraud
gray:
  percent: 5
  whitelist: []
```

## 语法约束

- `when` 支持 `all` / `any` / `not`，最多嵌套 3 层；
- 操作符：`eq, neq, gt, gte, lt, lte, in, not_in, between, is_null, regex`；
- 阈值必须是常量或已注册的特征，禁止内联脚本；
- `action` 只允许枚举值，禁止自定义动作；
- 每条规则必须有唯一 `rule_id` 与递增 `version`。

## 安全变更规范

1. 任何发布前先 `rule_validate` 做结构与特征存在性校验；
2. 灰度从 1%~5% 起步，观察期不少于 24 小时；
3. 必须准备回滚版本号；
4. 发布 / 灰度属于高风险动作，必须人工审批。

## 常见错误

- 使用未注册特征 → 校验失败；
- 浮点阈值精度问题 → 用 `between` 并明确边界；
- 条件之间用 `any` 导致误杀扩大；
- 忘记设置 `reason_code`，导致下游无法归因。
